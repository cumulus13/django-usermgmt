# File: django-usermgmt/usermgmt/management/commands/usermgr.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-11-17 04:19:48 SE Asia Standard Time
# Description:
# License: MIT

"""
``python manage.py usermgr <action> ...``

Thin CLI layer over ``usermgmt.services``. Run ``python manage.py usermgr -h``
or ``python manage.py usermgr <action> -h`` for details.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from getpass import getpass
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from usermgmt import services as svc

FORMATS = ("table", "json", "csv")
USER_COLUMNS = ["id", "username", "email", "active", "staff", "superuser", "last_login", "groups"]


class Command(BaseCommand):
    help = "Manage users, groups and permissions (create, list, check, grant, import/export, ...)."

    # ------------------------------------------------------------------ #
    # Argument definitions
    # ------------------------------------------------------------------ #
    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", metavar="ACTION", required=True)

        def add(name, help_text, fmt=False):
            p = sub.add_parser(name, help=help_text, description=help_text)
            if fmt:
                p.add_argument("--format", choices=FORMATS, default="table")
            return p

        def password_args(p, allow_none=False):
            g = p.add_argument_group("password source (default: interactive prompt)")
            g.add_argument("--password-stdin", action="store_true", help="read password from stdin (first line)")
            g.add_argument("--password-env", metavar="VAR", help="read password from environment variable VAR")
            g.add_argument("--password", help="UNSAFE: visible in process list / shell history")
            if allow_none:
                g.add_argument("--no-password", action="store_true", help="create user with an unusable password")
            p.add_argument("--skip-password-validation", action="store_true", help="bypass AUTH_PASSWORD_VALIDATORS")

        # ---- users --------------------------------------------------- #
        p = add("create-user", "Create a regular, staff or super user.")
        p.add_argument("username")
        p.add_argument("--email")
        p.add_argument("--first-name")
        p.add_argument("--last-name")
        p.add_argument("--staff", action="store_true", help="grant access to the admin site")
        p.add_argument("--superuser", action="store_true", help="all permissions (implies --staff)")
        p.add_argument("--inactive", action="store_true", help="create as inactive")
        p.add_argument("--group", action="append", default=[], metavar="NAME", help="repeatable")
        p.add_argument("--perm", action="append", default=[], metavar="APP.CODENAME", help="repeatable")
        p.add_argument("--create-missing-groups", action="store_true")
        p.add_argument("--field", action="append", default=[], metavar="KEY=VALUE", help="custom model field")
        p.add_argument("--unique-email", action="store_true", help="fail if the e-mail is already used")
        password_args(p, allow_none=True)

        p = add("list-users", "List users with filters.", fmt=True)
        p.add_argument("--search", help="substring of username / e-mail / name")
        p.add_argument("--active", action=argparse.BooleanOptionalAction, default=None)
        p.add_argument("--staff", action=argparse.BooleanOptionalAction, default=None)
        p.add_argument("--superuser", action=argparse.BooleanOptionalAction, default=None)
        p.add_argument("--group")
        p.add_argument("--stale-days", type=int, metavar="N", help="no login for N days")
        p.add_argument("--never-logged-in", action="store_true")
        p.add_argument("--order-by", metavar="FIELD", help="e.g. -date_joined")
        p.add_argument("--limit", type=int)

        p = add("show-user", "Show full details of one user.", fmt=True)
        p.add_argument("user", help="username, e-mail or numeric id")
        p.add_argument("--all-perms", action="store_true", help="list every effective permission")

        p = add("check-user", "Run health / permission / password checks (exit code 3 if any fails).", fmt=True)
        p.add_argument("user")
        p.add_argument("--perm", action="append", default=[], metavar="APP.CODENAME")
        p.add_argument("--check-password", action="store_true", help="prompt for a password to verify")
        p.add_argument("--password-stdin", action="store_true")
        p.add_argument("--password-env", metavar="VAR")

        p = add("update-user", "Update profile fields.")
        p.add_argument("user")
        p.add_argument("--email")
        p.add_argument("--first-name")
        p.add_argument("--last-name")
        p.add_argument("--new-username")
        p.add_argument("--field", action="append", default=[], metavar="KEY=VALUE")

        p = add("set-password", "Change a user's password.")
        p.add_argument("user")
        p.add_argument("--revoke-sessions", action="store_true", help="log the user out everywhere")
        password_args(p)

        p = add("set-flags", "Set is_active / is_staff / is_superuser.")
        p.add_argument("user")
        p.add_argument("--active", action=argparse.BooleanOptionalAction, default=None)
        p.add_argument("--staff", action=argparse.BooleanOptionalAction, default=None)
        p.add_argument("--superuser", action=argparse.BooleanOptionalAction, default=None)
        p.add_argument("--force", action="store_true", help="bypass last-superuser guard")

        p = add("activate", "Activate a user.")
        p.add_argument("user")

        p = add("deactivate", "Deactivate a user (optionally kill sessions).")
        p.add_argument("user")
        p.add_argument("--revoke-sessions", action="store_true")
        p.add_argument("--force", action="store_true")

        p = add("delete-user", "Delete a user permanently.")
        p.add_argument("user")
        p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
        p.add_argument("--force", action="store_true", help="bypass last-superuser guard")

        p = add("revoke-sessions", "Force logout: delete all DB sessions of a user.")
        p.add_argument("user")

        # ---- permissions ---------------------------------------------- #
        p = add("grant-perm", "Grant direct permissions to a user.")
        p.add_argument("user")
        p.add_argument("perms", nargs="+", metavar="APP.CODENAME", help="or APP.* for every permission of an app")

        p = add("revoke-perm", "Revoke direct permissions from a user.")
        p.add_argument("user")
        p.add_argument("perms", nargs="+", metavar="APP.CODENAME")

        p = add("list-perms", "List available permissions.", fmt=True)
        p.add_argument("--app")
        p.add_argument("--model")
        p.add_argument("--search")

        p = add("who-can", "List active users that effectively hold a permission.", fmt=True)
        p.add_argument("perm", metavar="APP.CODENAME")

        # ---- groups --------------------------------------------------- #
        p = add("create-group", "Create a group.")
        p.add_argument("name")
        p.add_argument("--perm", action="append", default=[], metavar="APP.CODENAME")
        p.add_argument("--exist-ok", action="store_true")

        p = add("delete-group", "Delete a group.")
        p.add_argument("name")
        p.add_argument("--yes", action="store_true")
        p.add_argument("--force", action="store_true", help="delete even if it has members")

        p = add("rename-group", "Rename a group.")
        p.add_argument("old")
        p.add_argument("new")

        p = add("list-groups", "List groups with member / permission counts.", fmt=True)
        p.add_argument("--search")

        p = add("show-group", "Show members and permissions of a group.", fmt=True)
        p.add_argument("name")

        p = add("group-grant", "Grant permissions to a group.")
        p.add_argument("name")
        p.add_argument("perms", nargs="+", metavar="APP.CODENAME")

        p = add("group-revoke", "Revoke permissions from a group.")
        p.add_argument("name")
        p.add_argument("perms", nargs="+", metavar="APP.CODENAME")

        p = add("add-to-group", "Add a user to one or more groups.")
        p.add_argument("user")
        p.add_argument("groups", nargs="+", metavar="GROUP")
        p.add_argument("--create-missing", action="store_true", help="create groups that do not exist")

        p = add("remove-from-group", "Remove a user from one or more groups.")
        p.add_argument("user")
        p.add_argument("groups", nargs="+", metavar="GROUP")

        # ---- bulk ------------------------------------------------------ #
        p = add("import-users", "Bulk import from CSV or JSON (columns: username,email,password,first_name,"
                                "last_name,is_staff,is_superuser,is_active,groups,permissions).", fmt=True)
        p.add_argument("file", type=Path)
        p.add_argument("--update", action="store_true", help="update users that already exist")
        p.add_argument("--dry-run", action="store_true", help="validate everything, then roll back")
        p.add_argument("--create-missing-groups", action="store_true")
        p.add_argument("--skip-password-validation", action="store_true")

        p = add("export-users", "Export users (never includes password hashes).")
        p.add_argument("--format", choices=("json", "csv"), default="json")
        p.add_argument("--output", type=Path, help="write to file instead of stdout")
        p.add_argument("--group")
        p.add_argument("--active", action=argparse.BooleanOptionalAction, default=None)

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def handle(self, *args, **options):
        handler = getattr(self, "do_" + options["action"].replace("-", "_"))
        try:
            handler(options)
        except svc.UserMgmtError as exc:
            raise CommandError(str(exc)) from exc

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _ok(self, message):
        self.stdout.write(self.style.SUCCESS(message))

    def _note(self, message):
        self.stdout.write(self.style.WARNING(message))

    @staticmethod
    def _cell(value):
        if value is None or value == "" or value == []:
            return "-"
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, (list, tuple)):
            return ",".join(str(v) for v in value)
        return str(value)

    def _render(self, rows, columns, fmt):
        if fmt == "json":
            self.stdout.write(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
            return
        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            writer.writerow(columns)
            for r in rows:
                writer.writerow([";".join(map(str, r[c])) if isinstance(r[c], list) else r[c] for c in columns])
            self.stdout.write(buf.getvalue().rstrip("\n"))
            return
        if not rows:
            self.stdout.write("(no results)")
            return
        table = [[self._cell(r.get(c)) for c in columns] for r in rows]
        widths = [max(len(c), *(len(row[i]) for row in table)) for i, c in enumerate(columns)]
        line = "  ".join(c.upper().ljust(w) for c, w in zip(columns, widths))
        self.stdout.write(self.style.MIGRATE_LABEL(line))
        self.stdout.write("  ".join("-" * w for w in widths))
        for row in table:
            self.stdout.write("  ".join(v.ljust(w) for v, w in zip(row, widths)))
        self.stdout.write(f"\n{len(rows)} row(s)")

    def _render_kv(self, data, fmt):
        if fmt == "json":
            self.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str))
            return
        if fmt == "csv":
            self._render([data], list(data), "csv")
            return
        width = max(len(k) for k in data)
        for key, value in data.items():
            if isinstance(value, list) and len(value) > 8:
                value = f"{', '.join(map(str, value[:8]))}, ... (+{len(value) - 8})"
            self.stdout.write(f"{key.ljust(width)} : {self._cell(value)}")

    def _confirm(self, question, assume_yes):
        if assume_yes:
            return
        if not sys.stdin.isatty():
            raise CommandError("Confirmation required: re-run with --yes in non-interactive mode.")
        if input(f"{question} [y/N]: ").strip().lower() not in {"y", "yes"}:
            raise CommandError("Aborted.")

    @staticmethod
    def _parse_kv(items):
        result = {}
        for item in items:
            if "=" not in item:
                raise CommandError(f"Invalid --field '{item}', expected KEY=VALUE.")
            key, value = item.split("=", 1)
            result[key.strip()] = value
        return result

    def _read_password(self, o, *, confirm=True, interactive=True):
        if o.get("password_stdin"):
            value = sys.stdin.readline().rstrip("\r\n")
        elif o.get("password_env"):
            value = os.environ.get(o["password_env"])
            if value is None:
                raise CommandError(f"Environment variable {o['password_env']} is not set.")
        elif o.get("password") is not None:
            self.stderr.write(self.style.WARNING("Warning: --password is visible to other users via `ps`."))
            value = o["password"]
        elif o.get("no_password"):
            return None
        elif interactive and sys.stdin.isatty():
            value = getpass("Password: ")
            if confirm and getpass("Password (again): ") != value:
                raise CommandError("Passwords do not match.")
        else:
            raise CommandError("No password source: use --password-stdin, --password-env VAR"
                               + (", or --no-password." if "no_password" in o else "."))
        if not value:
            raise CommandError("Password must not be empty.")
        return value

    # ------------------------------------------------------------------ #
    # Actions: users
    # ------------------------------------------------------------------ #
    def do_create_user(self, o):
        password = self._read_password(o)
        user = svc.create_user(
            o["username"],
            password=password,
            email=o["email"],
            first_name=o["first_name"],
            last_name=o["last_name"],
            is_staff=o["staff"],
            is_superuser=o["superuser"],
            is_active=not o["inactive"],
            groups=o["group"],
            permissions=o["perm"],
            create_missing_groups=o["create_missing_groups"],
            extra_fields=self._parse_kv(o["field"]),
            validate_password=not o["skip_password_validation"],
            unique_email=o["unique_email"],
        )
        kind = "superuser" if user.is_superuser else "staff user" if user.is_staff else "user"
        self._ok(f"Created {kind} '{user.get_username()}' (id={user.pk}).")

    def do_list_users(self, o):
        users = svc.list_users(
            search=o["search"], is_active=o["active"], is_staff=o["staff"], is_superuser=o["superuser"],
            group=o["group"], stale_days=o["stale_days"], never_logged_in=o["never_logged_in"],
            order_by=o["order_by"], limit=o["limit"],
        )
        self._render([svc.serialize_user(u) for u in users], USER_COLUMNS, o["format"])

    def do_show_user(self, o):
        user = svc.get_user(o["user"])
        self._render_kv(svc.serialize_user(user, detail=True, all_perms=o["all_perms"]), o["format"])

    def do_check_user(self, o):
        user = svc.get_user(o["user"])
        password = None
        if o["check_password"] or o["password_stdin"] or o["password_env"]:
            password = self._read_password(o, confirm=False)
        results = svc.check_user(user, password=password, perms=o["perm"])
        self._render(results, ["check", "ok", "detail"], o["format"])
        if not all(r["ok"] for r in results):
            raise CommandError("One or more checks failed.", returncode=3)

    def do_update_user(self, o):
        user = svc.get_user(o["user"])
        fields = self._parse_kv(o["field"])
        for key in ("email", "first_name", "last_name"):
            if o[key] is not None:
                fields[key] = o[key]
        if o["new_username"]:
            fields[svc.user_model().USERNAME_FIELD] = o["new_username"]
        svc.update_user(user, **fields)
        self._ok(f"Updated '{user.get_username()}': {', '.join(sorted(fields))}.")

    def do_set_password(self, o):
        user = svc.get_user(o["user"])
        password = self._read_password(o)
        revoked = svc.set_password(
            user, password, validate=not o["skip_password_validation"], revoke=o["revoke_sessions"]
        )
        self._ok(f"Password changed for '{user.get_username()}'." + (f" {revoked} session(s) revoked." if o["revoke_sessions"] else ""))

    def do_set_flags(self, o):
        user = svc.get_user(o["user"])
        svc.set_flags(user, is_active=o["active"], is_staff=o["staff"], is_superuser=o["superuser"], force=o["force"])
        self._ok(f"Flags updated for '{user.get_username()}'.")

    def do_activate(self, o):
        svc.set_flags(svc.get_user(o["user"]), is_active=True)
        self._ok(f"Activated '{o['user']}'.")

    def do_deactivate(self, o):
        user = svc.get_user(o["user"])
        svc.set_flags(user, is_active=False, force=o["force"])
        if o["revoke_sessions"]:
            svc.revoke_sessions(user)
        self._ok(f"Deactivated '{user.get_username()}'.")

    def do_delete_user(self, o):
        user = svc.get_user(o["user"])
        self._confirm(f"Permanently delete user '{user.get_username()}'?", o["yes"])
        self._ok(f"Deleted '{svc.delete_user(user, force=o['force'])}'.")

    def do_revoke_sessions(self, o):
        count = svc.revoke_sessions(svc.get_user(o["user"]))
        self._ok(f"{count} session(s) revoked.")

    # ------------------------------------------------------------------ #
    # Actions: permissions
    # ------------------------------------------------------------------ #
    def _report_change(self, changed, same, verb):
        if changed:
            self._ok(f"{verb}: {', '.join(changed)}")
        if same:
            self._note(f"No change: {', '.join(same)}")

    def do_grant_perm(self, o):
        self._report_change(*svc.grant_permissions(svc.get_user(o["user"]), o["perms"]), "Granted")

    def do_revoke_perm(self, o):
        self._report_change(*svc.revoke_permissions(svc.get_user(o["user"]), o["perms"]), "Revoked")

    def do_list_perms(self, o):
        qs = svc.list_permissions(app_label=o["app"], model=o["model"], search=o["search"])
        rows = [{"permission": f"{p.content_type.app_label}.{p.codename}", "name": p.name,
                 "model": p.content_type.model} for p in qs]
        self._render(rows, ["permission", "name", "model"], o["format"])

    def do_who_can(self, o):
        rows = [svc.serialize_user(u) for u in svc.who_can(o["perm"])]
        self._render(rows, USER_COLUMNS, o["format"])

    # ------------------------------------------------------------------ #
    # Actions: groups
    # ------------------------------------------------------------------ #
    def do_create_group(self, o):
        group, created = svc.create_group(o["name"], permissions=o["perm"], exist_ok=o["exist_ok"])
        self._ok(f"Group '{group.name}' " + ("created." if created else "already exists."))

    def do_delete_group(self, o):
        self._confirm(f"Delete group '{o['name']}'?", o["yes"])
        members = svc.delete_group(o["name"], force=o["force"])
        self._ok(f"Group '{o['name']}' deleted ({members} member(s) detached).")

    def do_rename_group(self, o):
        svc.rename_group(o["old"], o["new"])
        self._ok(f"Renamed '{o['old']}' -> '{o['new']}'.")

    def do_list_groups(self, o):
        rows = [{"id": g.pk, "name": g.name, "users": g.user_count, "permissions": g.permission_count}
                for g in svc.list_groups(search=o["search"])]
        self._render(rows, ["id", "name", "users", "permissions"], o["format"])

    def do_show_group(self, o):
        self._render_kv(svc.serialize_group(svc.get_group(o["name"])), o["format"])

    def do_group_grant(self, o):
        self._report_change(*svc.grant_group_permissions(o["name"], o["perms"]), "Granted")

    def do_group_revoke(self, o):
        self._report_change(*svc.revoke_group_permissions(o["name"], o["perms"]), "Revoked")

    def do_add_to_group(self, o):
        user = svc.get_user(o["user"])
        added = svc.add_to_groups(user, o["groups"], create_missing=o["create_missing"])
        self._ok(f"'{user.get_username()}' added to: {', '.join(added)}") if added else self._note("Already a member of all given groups.")

    def do_remove_from_group(self, o):
        user = svc.get_user(o["user"])
        removed = svc.remove_from_groups(user, o["groups"])
        self._ok(f"'{user.get_username()}' removed from: {', '.join(removed)}") if removed else self._note("Was not a member of any given group.")

    # ------------------------------------------------------------------ #
    # Actions: bulk
    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_rows(path: Path):
        if not path.is_file():
            raise CommandError(f"File not found: {path}")
        suffix = path.suffix.lower()
        try:
            if suffix == ".json":
                data = json.loads(path.read_text(encoding="utf-8"))
                rows = data.get("users", []) if isinstance(data, dict) else data
                if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                    raise CommandError("JSON must be a list of objects (or {'users': [...]}).")
                return rows
            if suffix == ".csv":
                with path.open(newline="", encoding="utf-8-sig") as fh:
                    return list(csv.DictReader(fh))
        except (json.JSONDecodeError, UnicodeDecodeError, csv.Error) as exc:
            raise CommandError(f"Cannot parse {path}: {exc}") from exc
        raise CommandError("Unsupported file type (use .csv or .json).")

    def do_import_users(self, o):
        results = svc.import_users(
            self._load_rows(o["file"]),
            update_existing=o["update"],
            validate_password=not o["skip_password_validation"],
            create_missing_groups=o["create_missing_groups"],
            dry_run=o["dry_run"],
        )
        self._render(results, ["row", "username", "status", "message"], o["format"])
        summary = {s: sum(r["status"] == s for r in results) for s in ("created", "updated", "skipped", "error")}
        prefix = "[DRY RUN - nothing saved] " if o["dry_run"] else ""
        self.stderr.write(prefix + ", ".join(f"{k}={v}" for k, v in summary.items()))
        if summary["error"]:
            raise CommandError("Import finished with errors.", returncode=3)

    def do_export_users(self, o):
        rows = svc.export_users(group=o["group"], is_active=o["active"])
        columns = ["username", "email", "first_name", "last_name", "is_active", "is_staff",
                   "is_superuser", "groups", "permissions"]
        target = o["output"]
        if target is None:
            self._render(rows, columns, o["format"])
            return
        buf = io.StringIO()
        if o["format"] == "json":
            json.dump(rows, buf, indent=2, ensure_ascii=False)
        else:
            writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        target.write_text(buf.getvalue(), encoding="utf-8")
        self._ok(f"Exported {len(rows)} user(s) to {target}.")
