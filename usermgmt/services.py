# File: django-usermgmt/usermgmt/services.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-11-17 04:19:48 SE Asia Standard Time
# Description:
# License: MIT

"""
Service layer for ``usermgmt``.

All business logic lives here (no printing, no argparse) so it can be reused
from management commands, scripts, Celery tasks, tests, etc.

Design notes
------------
* Works with custom user models (``AUTH_USER_MODEL``): the login field is read
  from ``USERNAME_FIELD`` and optional fields are only touched if they exist.
* Every mutating operation is atomic and writes one line to the
  ``usermgmt.audit`` logger. Passwords are NEVER logged.
* Safety guards: the last active superuser cannot be deleted / deactivated /
  demoted unless ``force=True``.
"""
from __future__ import annotations

import getpass
import logging
import re
from datetime import timedelta
from typing import Any, Iterable, Mapping, Sequence

from django.apps import apps
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

audit = logging.getLogger("usermgmt.audit")

TRUE_VALUES = {"1", "true", "t", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "f", "no", "n", "off"}


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class UserMgmtError(Exception):
    """Base class for all expected (user-facing) errors."""


class NotFoundError(UserMgmtError):
    pass


class AlreadyExistsError(UserMgmtError):
    pass


class InvalidInputError(UserMgmtError):
    pass


class SafetyError(UserMgmtError):
    """Operation blocked by a safety guard (can be overridden with force)."""


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def user_model():
    return get_user_model()


def _manager():
    return user_model()._default_manager


def _has_field(model, name: str) -> bool:
    try:
        model._meta.get_field(name)
        return True
    except FieldDoesNotExist:
        return False


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - exotic environments
        return "unknown"


def _audit(action: str, **details: Any) -> None:
    extra = " ".join(f"{k}={v!r}" for k, v in details.items())
    audit.info("action=%s actor=%s %s", action, _actor(), extra)


def _clean_identifier(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        raise InvalidInputError("Username/identifier must not be empty.")
    return value


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(exc.messages)


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise InvalidInputError(f"Cannot interpret {value!r} as a boolean.")


def split_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in re.split(r"[;,|]", str(value)) if p.strip()]


def _perm_str(perm: Permission) -> str:
    return f"{perm.content_type.app_label}.{perm.codename}"


def _active_superuser_count(exclude_pk=None) -> int:
    qs = _manager().filter(is_superuser=True, is_active=True)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.count()


def _guard_last_superuser(user, action: str, force: bool) -> None:
    if force:
        return
    if getattr(user, "is_superuser", False) and getattr(user, "is_active", False):
        if _active_superuser_count(exclude_pk=user.pk) == 0:
            raise SafetyError(
                f"Refusing to {action} '{user.get_username()}': it is the last "
                "active superuser. Use force to override."
            )


# --------------------------------------------------------------------------- #
# Users: lookup / query / serialize
# --------------------------------------------------------------------------- #
def get_user(identifier: str):
    """Find a user by USERNAME_FIELD, then by e-mail (if it contains '@'), then by pk."""
    User = user_model()
    ident = _clean_identifier(identifier)
    field = User.USERNAME_FIELD
    manager = _manager()

    found = manager.filter(**{field: ident}).first()
    if found:
        return found
    if "@" in ident and _has_field(User, "email"):
        matches = list(manager.filter(email__iexact=ident)[:2])
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise InvalidInputError(f"E-mail '{ident}' matches several users; use the username.")
    if ident.isdigit():
        found = manager.filter(pk=int(ident)).first()
        if found:
            return found
    raise NotFoundError(f"User '{ident}' not found.")


def list_users(
    *,
    search: str | None = None,
    is_active: bool | None = None,
    is_staff: bool | None = None,
    is_superuser: bool | None = None,
    group: str | None = None,
    stale_days: int | None = None,
    never_logged_in: bool = False,
    order_by: str | None = None,
    limit: int | None = None,
):
    User = user_model()
    field = User.USERNAME_FIELD
    qs = _manager().all().prefetch_related("groups")

    if search:
        q = Q(**{f"{field}__icontains": search})
        for extra in ("email", "first_name", "last_name"):
            if extra != field and _has_field(User, extra):
                q |= Q(**{f"{extra}__icontains": search})
        qs = qs.filter(q)

    for name, value in (("is_active", is_active), ("is_staff", is_staff), ("is_superuser", is_superuser)):
        if value is not None:
            if not _has_field(User, name):
                raise InvalidInputError(f"User model has no field '{name}'.")
            qs = qs.filter(**{name: value})

    if group:
        get_group(group)  # raises NotFoundError for typos instead of silently returning nothing
        qs = qs.filter(groups__name=group)

    if never_logged_in:
        qs = qs.filter(last_login__isnull=True)
    if stale_days is not None:
        if stale_days < 0:
            raise InvalidInputError("stale_days must be >= 0.")
        cutoff = timezone.now() - timedelta(days=stale_days)
        stale = Q(last_login__lt=cutoff)
        if _has_field(User, "date_joined"):
            stale |= Q(last_login__isnull=True, date_joined__lt=cutoff)
        qs = qs.filter(stale)

    if order_by:
        bare = order_by.lstrip("-")
        if bare != "pk" and not _has_field(User, bare):
            raise InvalidInputError(f"Cannot order by unknown field '{bare}'.")
        qs = qs.order_by(order_by)
    else:
        qs = qs.order_by(field)

    qs = qs.distinct()
    if limit is not None:
        if limit < 1:
            raise InvalidInputError("limit must be >= 1.")
        qs = qs[:limit]
    return qs


def _iso(value):
    return value.isoformat(timespec="seconds") if value else None


def serialize_user(user, *, detail: bool = False, all_perms: bool = False) -> dict:
    data: dict[str, Any] = {
        "id": user.pk,
        "username": user.get_username(),
        "email": getattr(user, "email", None) or None,
        "active": getattr(user, "is_active", None),
        "staff": getattr(user, "is_staff", None),
        "superuser": getattr(user, "is_superuser", None),
        "last_login": _iso(getattr(user, "last_login", None)),
        "groups": sorted(g.name for g in user.groups.all()),
    }
    if detail:
        data.update(
            {
                "first_name": getattr(user, "first_name", None) or None,
                "last_name": getattr(user, "last_name", None) or None,
                "date_joined": _iso(getattr(user, "date_joined", None)),
                "has_usable_password": user.has_usable_password(),
                "direct_permissions": sorted(
                    _perm_str(p) for p in user.user_permissions.select_related("content_type")
                ),
            }
        )
        effective = sorted(user.get_all_permissions())
        data["effective_permission_count"] = len(effective)
        if all_perms:
            data["effective_permissions"] = effective
    return data


def export_users(**filters) -> list[dict]:
    rows = []
    for user in list_users(**filters):
        d = serialize_user(user, detail=True)
        rows.append(
            {
                "username": d["username"],
                "email": d["email"] or "",
                "first_name": d.get("first_name") or "",
                "last_name": d.get("last_name") or "",
                "is_active": d["active"],
                "is_staff": d["staff"],
                "is_superuser": d["superuser"],
                "groups": ";".join(d["groups"]),
                "permissions": ";".join(d["direct_permissions"]),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Users: mutations
# --------------------------------------------------------------------------- #
def create_user(
    identifier: str,
    *,
    password: str | None = None,
    email: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    is_staff: bool = False,
    is_superuser: bool = False,
    is_active: bool = True,
    groups: Iterable[str] = (),
    permissions: Iterable[str] = (),
    create_missing_groups: bool = False,
    extra_fields: Mapping[str, Any] | None = None,
    validate_password: bool = True,
    unique_email: bool = False,
):
    """Create a regular (or staff / super) user. ``password=None`` => unusable password."""
    User = user_model()
    field = User.USERNAME_FIELD
    identifier = _clean_identifier(identifier)
    groups = list(groups)
    permissions = list(permissions)
    if password is not None and password == "":
        raise InvalidInputError("Password must not be empty (use None for an unusable password).")

    fields: dict[str, Any] = dict(extra_fields or {})
    for key, value in (("email", email), ("first_name", first_name), ("last_name", last_name)):
        if value is not None and key != field:
            fields[key] = value
    if is_superuser:
        is_staff = True
    for key, value in (("is_staff", is_staff), ("is_superuser", is_superuser), ("is_active", is_active)):
        if _has_field(User, key):
            fields[key] = value
    for key in fields:
        if not _has_field(User, key):
            raise InvalidInputError(f"User model has no field '{key}'.")

    if _manager().filter(**{field: identifier}).exists():
        raise AlreadyExistsError(f"User '{identifier}' already exists.")
    if unique_email and fields.get("email") and _manager().filter(email__iexact=fields["email"]).exists():
        raise AlreadyExistsError(f"E-mail '{fields['email']}' is already used.")

    candidate = User(**{field: identifier}, **fields)
    try:
        candidate.full_clean(exclude=["password"], validate_unique=False)
        if password is not None and validate_password:
            password_validation.validate_password(password, candidate)
    except ValidationError as exc:
        raise InvalidInputError(_validation_message(exc)) from exc

    with transaction.atomic():
        try:
            user = _manager().create_user(**{field: identifier}, password=password, **fields)
        except IntegrityError as exc:
            raise AlreadyExistsError(f"User '{identifier}' already exists.") from exc
        if groups:
            add_to_groups(user, groups, create_missing=create_missing_groups)
        if permissions:
            grant_permissions(user, permissions)

    _audit(
        "create_user",
        user=identifier,
        staff=is_staff,
        superuser=is_superuser,
        active=is_active,
        groups=groups,
    )
    return user


def update_user(user, **fields: Any):
    """Update profile fields (email, first_name, last_name, USERNAME_FIELD, custom fields)."""
    User = user_model()
    if not fields:
        raise InvalidInputError("Nothing to update.")
    for key in fields:
        if key in {"password", "is_superuser", "is_staff", "is_active", "id", "pk"}:
            raise InvalidInputError(f"Field '{key}' cannot be changed here; use the dedicated command.")
        if not _has_field(User, key):
            raise InvalidInputError(f"User model has no field '{key}'.")

    old_name = user.get_username()
    for key, value in fields.items():
        setattr(user, key, value)
    try:
        user.full_clean(exclude=["password"], validate_unique=True)
    except ValidationError as exc:
        raise InvalidInputError(_validation_message(exc)) from exc
    with transaction.atomic():
        user.save(update_fields=list(fields))
    _audit("update_user", user=old_name, fields=sorted(fields))
    return user


def set_password(user, password: str, *, validate: bool = True, revoke: bool = False) -> int:
    """Change password. Returns number of revoked sessions (0 if not requested)."""
    if not password:
        raise InvalidInputError("Password must not be empty.")
    if validate:
        try:
            password_validation.validate_password(password, user)
        except ValidationError as exc:
            raise InvalidInputError(_validation_message(exc)) from exc
    with transaction.atomic():
        user.set_password(password)
        user.save(update_fields=["password"])
    revoked = revoke_sessions(user) if revoke else 0
    _audit("set_password", user=user.get_username(), revoked_sessions=revoked)
    return revoked


def set_flags(
    user,
    *,
    is_active: bool | None = None,
    is_staff: bool | None = None,
    is_superuser: bool | None = None,
    force: bool = False,
):
    """
    Toggle is_active / is_staff / is_superuser.
    Promoting to superuser also sets is_staff=True unless is_staff is given explicitly.
    """
    changes: dict[str, bool] = {}
    if is_superuser is True and is_staff is None:
        is_staff = True
    for key, value in (("is_active", is_active), ("is_staff", is_staff), ("is_superuser", is_superuser)):
        if value is None:
            continue
        if not _has_field(type(user), key):
            raise InvalidInputError(f"User model has no field '{key}'.")
        changes[key] = value
    if not changes:
        raise InvalidInputError("No flag specified.")

    if changes.get("is_active") is False or changes.get("is_superuser") is False:
        _guard_last_superuser(user, "deactivate/demote", force)

    for key, value in changes.items():
        setattr(user, key, value)
    with transaction.atomic():
        user.save(update_fields=list(changes))
    _audit("set_flags", user=user.get_username(), **changes)
    return user


def delete_user(user, *, force: bool = False) -> str:
    _guard_last_superuser(user, "delete", force)
    name = user.get_username()
    with transaction.atomic():
        user.delete()
    _audit("delete_user", user=name)
    return name


def revoke_sessions(user) -> int:
    """Delete all active DB-backed sessions of ``user`` (forces logout everywhere)."""
    if not apps.is_installed("django.contrib.sessions"):
        raise InvalidInputError("django.contrib.sessions is not installed.")
    from django.contrib.sessions.models import Session

    target = str(user.pk)
    count = 0
    for session in Session.objects.filter(expire_date__gte=timezone.now()).iterator():
        try:
            data = session.get_decoded()
        except Exception:  # corrupt session
            continue
        if str(data.get("_auth_user_id")) == target:
            session.delete()
            count += 1
    _audit("revoke_sessions", user=user.get_username(), count=count)
    return count


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_user(user, *, password: str | None = None, perms: Sequence[str] = ()) -> list[dict]:
    """Return a list of {check, ok, detail} dicts."""
    results = [
        {"check": "active", "ok": bool(user.is_active), "detail": ""},
        {
            "check": "usable_password",
            "ok": user.has_usable_password(),
            "detail": "" if user.has_usable_password() else "login by password impossible",
        },
        {
            "check": "admin_access",
            "ok": bool(user.is_active and getattr(user, "is_staff", False)),
            "detail": "is_active and is_staff",
        },
    ]
    if password is not None:
        results.append({"check": "password_matches", "ok": user.check_password(password), "detail": ""})
    for spec in perms:
        resolve_permissions([spec])  # validates that the permission exists
        results.append({"check": f"perm:{spec}", "ok": user.has_perm(spec), "detail": ""})
    return results


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #
def resolve_permissions(specs: Iterable[str]) -> list[Permission]:
    """Resolve 'app_label.codename' (or 'app_label.*') into Permission objects."""
    resolved: dict[int, Permission] = {}
    base = Permission.objects.select_related("content_type")
    for raw in specs:
        spec = raw.strip()
        if "." not in spec:
            raise InvalidInputError(f"Invalid permission '{spec}'. Expected 'app_label.codename'.")
        app_label, codename = spec.split(".", 1)
        if codename == "*":
            found = list(base.filter(content_type__app_label=app_label))
            if not found:
                raise NotFoundError(f"No permissions for app '{app_label}'.")
        else:
            found = list(base.filter(content_type__app_label=app_label, codename=codename))
            if not found:
                raise NotFoundError(f"Permission '{spec}' not found (did you run migrate?).")
            if len(found) > 1:
                raise InvalidInputError(f"Permission '{spec}' is ambiguous.")
        for perm in found:
            resolved[perm.pk] = perm
    return list(resolved.values())


def list_permissions(*, app_label: str | None = None, model: str | None = None, search: str | None = None):
    qs = Permission.objects.select_related("content_type").order_by(
        "content_type__app_label", "content_type__model", "codename"
    )
    if app_label:
        qs = qs.filter(content_type__app_label=app_label)
    if model:
        qs = qs.filter(content_type__model=model.lower())
    if search:
        qs = qs.filter(Q(codename__icontains=search) | Q(name__icontains=search))
    return qs


def _change_perms(owner_manager, specs: Iterable[str], *, add: bool) -> tuple[list[str], list[str]]:
    """Return (changed, unchanged) permission strings."""
    perms = resolve_permissions(specs)
    current = set(owner_manager.values_list("pk", flat=True))
    to_change = [p for p in perms if (p.pk not in current) == add]
    unchanged = [_perm_str(p) for p in perms if p not in to_change]
    with transaction.atomic():
        if to_change:
            (owner_manager.add if add else owner_manager.remove)(*to_change)
    return [_perm_str(p) for p in to_change], unchanged


def grant_permissions(user, specs: Iterable[str]) -> tuple[list[str], list[str]]:
    changed, same = _change_perms(user.user_permissions, specs, add=True)
    if changed:
        _audit("grant_permissions", user=user.get_username(), perms=changed)
    return changed, same


def revoke_permissions(user, specs: Iterable[str]) -> tuple[list[str], list[str]]:
    changed, same = _change_perms(user.user_permissions, specs, add=False)
    if changed:
        _audit("revoke_permissions", user=user.get_username(), perms=changed)
    return changed, same


def who_can(spec: str):
    """Active users that effectively hold the permission (direct, via group, or superuser)."""
    (perm,) = resolve_permissions([spec])
    return (
        _manager()
        .filter(is_active=True)
        .filter(Q(is_superuser=True) | Q(user_permissions=perm) | Q(groups__permissions=perm))
        .distinct()
        .order_by(user_model().USERNAME_FIELD)
    )


# --------------------------------------------------------------------------- #
# Groups
# --------------------------------------------------------------------------- #
def get_group(name: str) -> Group:
    try:
        return Group.objects.get(name=name)
    except Group.DoesNotExist:
        raise NotFoundError(f"Group '{name}' not found.") from None


def _clean_group_name(name: str) -> str:
    name = (name or "").strip()
    max_len = Group._meta.get_field("name").max_length
    if not name:
        raise InvalidInputError("Group name must not be empty.")
    if len(name) > max_len:
        raise InvalidInputError(f"Group name too long (max {max_len}).")
    return name


def create_group(name: str, *, permissions: Iterable[str] = (), exist_ok: bool = False) -> tuple[Group, bool]:
    name = _clean_group_name(name)
    with transaction.atomic():
        group, created = Group.objects.get_or_create(name=name)
        if not created and not exist_ok:
            raise AlreadyExistsError(f"Group '{name}' already exists.")
        perm_list = list(permissions)
        if perm_list:
            _change_perms(group.permissions, perm_list, add=True)
    if created:
        _audit("create_group", group=name, perms=list(permissions))
    return group, created


def delete_group(name: str, *, force: bool = False) -> int:
    group = get_group(name)
    members = group.user_set.count()
    if members and not force:
        raise SafetyError(f"Group '{name}' still has {members} member(s). Use force to delete anyway.")
    with transaction.atomic():
        group.delete()
    _audit("delete_group", group=name, members=members)
    return members


def rename_group(old: str, new: str) -> Group:
    group = get_group(old)
    new = _clean_group_name(new)
    if Group.objects.filter(name=new).exists():
        raise AlreadyExistsError(f"Group '{new}' already exists.")
    group.name = new
    group.save(update_fields=["name"])
    _audit("rename_group", old=old, new=new)
    return group


def list_groups(*, search: str | None = None):
    qs = Group.objects.annotate(
        user_count=Count("user", distinct=True),
        permission_count=Count("permissions", distinct=True),
    ).order_by("name")
    if search:
        qs = qs.filter(name__icontains=search)
    return qs


def serialize_group(group: Group) -> dict:
    return {
        "id": group.pk,
        "name": group.name,
        "members": sorted(u.get_username() for u in group.user_set.all()),
        "permissions": sorted(_perm_str(p) for p in group.permissions.select_related("content_type")),
    }


def grant_group_permissions(name: str, specs: Iterable[str]) -> tuple[list[str], list[str]]:
    group = get_group(name)
    changed, same = _change_perms(group.permissions, specs, add=True)
    if changed:
        _audit("grant_group_permissions", group=name, perms=changed)
    return changed, same


def revoke_group_permissions(name: str, specs: Iterable[str]) -> tuple[list[str], list[str]]:
    group = get_group(name)
    changed, same = _change_perms(group.permissions, specs, add=False)
    if changed:
        _audit("revoke_group_permissions", group=name, perms=changed)
    return changed, same


def add_to_groups(user, names: Iterable[str], *, create_missing: bool = False) -> list[str]:
    names = list(dict.fromkeys(_clean_group_name(n) for n in names))
    existing = {g.name: g for g in Group.objects.filter(name__in=names)}
    missing = [n for n in names if n not in existing]
    with transaction.atomic():
        if missing:
            if not create_missing:
                raise NotFoundError(f"Group(s) not found: {', '.join(missing)} (use create-missing).")
            for n in missing:
                existing[n] = Group.objects.create(name=n)
        current = set(user.groups.values_list("name", flat=True))
        new = [existing[n] for n in names if n not in current]
        if new:
            user.groups.add(*new)
    added = [g.name for g in new]
    if added:
        _audit("add_to_groups", user=user.get_username(), groups=added)
    return added


def remove_from_groups(user, names: Iterable[str]) -> list[str]:
    names = list(dict.fromkeys(names))
    groups = [get_group(n) for n in names]
    current = set(user.groups.values_list("name", flat=True))
    removed = [g for g in groups if g.name in current]
    with transaction.atomic():
        if removed:
            user.groups.remove(*removed)
    result = [g.name for g in removed]
    if result:
        _audit("remove_from_groups", user=user.get_username(), groups=result)
    return result


# --------------------------------------------------------------------------- #
# Bulk import
# --------------------------------------------------------------------------- #
def _import_row(row: Mapping[str, Any], *, update_existing, validate_password, create_missing_groups) -> str:
    User = user_model()
    field = User.USERNAME_FIELD
    ident = _clean_identifier(str(row.get(field) or row.get("username") or ""))
    groups = split_list(row.get("groups"))
    perms = split_list(row.get("permissions"))
    password = row.get("password") or None
    flags = {k: to_bool(row[k]) for k in ("is_staff", "is_superuser", "is_active") if row.get(k) not in (None, "")}
    profile = {
        k: row[k] for k in ("email", "first_name", "last_name") if row.get(k) not in (None, "") and k != field
    }

    existing = _manager().filter(**{field: ident}).first()
    if existing is None:
        create_user(
            ident,
            password=password,
            email=profile.get("email"),
            first_name=profile.get("first_name"),
            last_name=profile.get("last_name"),
            is_staff=flags.get("is_staff", False),
            is_superuser=flags.get("is_superuser", False),
            is_active=flags.get("is_active", True),
            groups=groups,
            permissions=perms,
            create_missing_groups=create_missing_groups,
            validate_password=validate_password,
        )
        return "created"

    if not update_existing:
        return "skipped"
    if profile:
        update_user(existing, **profile)
    if flags:
        set_flags(existing, **flags)
    if password:
        set_password(existing, password, validate=validate_password)
    if groups:
        add_to_groups(existing, groups, create_missing=create_missing_groups)
    if perms:
        grant_permissions(existing, perms)
    return "updated"


def import_users(
    rows: Iterable[Mapping[str, Any]],
    *,
    update_existing: bool = False,
    validate_password: bool = True,
    create_missing_groups: bool = False,
    dry_run: bool = False,
) -> list[dict]:
    """
    Import many users. Each row is isolated in its own savepoint: a bad row is
    reported and skipped without affecting the others. ``dry_run`` rolls back all.
    """
    results: list[dict] = []
    with transaction.atomic():
        for number, row in enumerate(rows, start=1):
            ident = str(row.get(user_model().USERNAME_FIELD) or row.get("username") or "")
            try:
                with transaction.atomic():
                    status = _import_row(
                        row,
                        update_existing=update_existing,
                        validate_password=validate_password,
                        create_missing_groups=create_missing_groups,
                    )
                results.append({"row": number, "username": ident, "status": status, "message": ""})
            except Exception as exc:  # noqa: BLE001 - report per row, keep going
                if not isinstance(exc, UserMgmtError):
                    audit.exception("import row %s failed", number)
                results.append({"row": number, "username": ident, "status": "error", "message": str(exc)})
        if dry_run:
            transaction.set_rollback(True)
    _audit(
        "import_users",
        total=len(results),
        dry_run=dry_run,
        errors=sum(r["status"] == "error" for r in results),
    )
    return results
