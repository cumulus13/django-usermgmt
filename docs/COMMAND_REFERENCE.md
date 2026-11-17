# Command Reference

```
python manage.py usermgr ACTION [arguments] [options]
python manage.py usermgr -h                # list all actions
python manage.py usermgr ACTION -h         # help for one action
```

Conventions used below:

- `USER` is a username, e-mail address or numeric id (see [resolution order](USER_GUIDE.md#2-identifying-a-user)).
- `PERM` is `app_label.codename`, or `app_label.*` for every permission of an app.
- `[opt ...]` means the option may be repeated.
- Boolean toggles such as `--active/--no-active` leave the value untouched when neither is given.

## Contents

- [Global behaviour](#global-behaviour)
- [Shared option groups](#shared-option-groups)
- [User actions](#user-actions)
- [Permission actions](#permission-actions)
- [Group actions](#group-actions)
- [Bulk actions](#bulk-actions)
- [Exit codes](#exit-codes)
- [Audit log format](#audit-log-format)

---

## Global behaviour

Django's standard management command options (`--settings`, `--pythonpath`, `--traceback`, `-v`, `--no-color`,
`--force-color`, `--skip-checks`) work as usual and go **before** the action name:

```bash
python manage.py usermgr --settings=config.settings.prod list-users
```

## Shared option groups

### Password options

Used by `create-user`, `set-password` and (partly) `check-user`.

| Option | Description |
|---|---|
| `--password-stdin` | Read the password from the first line of standard input. |
| `--password-env VAR` | Read the password from environment variable `VAR`. |
| `--password VALUE` | Pass the password on the command line. **Unsafe**: prints a warning. |
| `--no-password` | `create-user` only. Create the account with an unusable password. |
| `--skip-password-validation` | Do not run `AUTH_PASSWORD_VALIDATORS`. Not available on `check-user`. |

With no source and an interactive terminal, you are prompted twice (no echo). With no source and no terminal the
command fails.

### Output format

| Option | Values | Default |
|---|---|---|
| `--format` | `table`, `json`, `csv` | `table` |

`export-users` supports `json` and `csv` only, default `json`.

---

## User actions

### `create-user USERNAME`

Create a regular, staff or super user. User creation, group membership and direct permissions happen in one
transaction.

| Option | Description |
|---|---|
| `--email EMAIL` | E-mail address. |
| `--first-name NAME`, `--last-name NAME` | Profile fields (only if the model has them). |
| `--staff` | Grant `is_staff` (admin site access). |
| `--superuser` | Grant `is_superuser`; implies `--staff`. |
| `--inactive` | Create the account with `is_active=False`. |
| `--group NAME` `[...]` | Add to a group. Repeatable. |
| `--perm PERM` `[...]` | Grant a direct permission. Repeatable. |
| `--create-missing-groups` | Create groups given with `--group` if they do not exist. |
| `--field KEY=VALUE` `[...]` | Set any other model field (for custom user models). Values are passed as strings. |
| `--unique-email` | Fail if the e-mail is already used by another user (case-insensitive). |
| password options | See above, including `--no-password`. |

Validation performed before anything is written: non-empty username, not already existing, all fields exist on
the model, `full_clean()` (field validators, `REQUIRED_FIELDS`), then password validators.

```bash
python manage.py usermgr create-user budi --email budi@example.com --group editors --create-missing-groups
```

### `list-users`

| Option | Description |
|---|---|
| `--search TEXT` | Case-insensitive substring of username, e-mail, first name or last name. |
| `--active` / `--no-active` | Filter on `is_active`. |
| `--staff` / `--no-staff` | Filter on `is_staff`. |
| `--superuser` / `--no-superuser` | Filter on `is_superuser`. |
| `--group NAME` | Members of a group. Errors if the group does not exist. |
| `--stale-days N` | No login for `N` days, or never logged in and joined more than `N` days ago. |
| `--never-logged-in` | `last_login` is empty. |
| `--order-by FIELD` | Any model field, optionally prefixed with `-`. Default: the username field. |
| `--limit N` | Return at most `N` rows. |
| `--format` | See above. |

Columns: `id`, `username`, `email`, `active`, `staff`, `superuser`, `last_login`, `groups`.

### `show-user USER`

Full detail for one user: profile, flags, dates, whether the password is usable, groups, direct permissions and
the number of effective permissions.

| Option | Description |
|---|---|
| `--all-perms` | Also list every effective permission. |
| `--format` | See above. |

### `check-user USER`

Runs health checks and prints one row per check. Exits with code `3` if any check fails.

| Check | Passes when |
|---|---|
| `active` | `is_active` is true. |
| `usable_password` | The account has a password that can be used to log in. |
| `admin_access` | The user is active **and** staff. |
| `password_matches` | Only with a password source. The supplied password is correct. |
| `perm:APP.CODENAME` | Only with `--perm`. `user.has_perm()` returns true. |

| Option | Description |
|---|---|
| `--perm PERM` `[...]` | Permissions to test. An unknown permission is an error, not a failed check. |
| `--check-password` | Prompt for a password to verify. |
| `--password-stdin`, `--password-env VAR` | Non-interactive alternatives. |
| `--format` | See above. |

### `update-user USER`

Update profile fields. At least one option is required. The result is validated with `full_clean()`
(including uniqueness) before saving.

| Option | Description |
|---|---|
| `--email`, `--first-name`, `--last-name` | Profile fields. |
| `--new-username NAME` | Rename (sets the model's `USERNAME_FIELD`). |
| `--field KEY=VALUE` `[...]` | Any other model field. |

Passwords and the `is_*` flags are rejected here; use `set-password` and `set-flags`.

### `set-password USER`

| Option | Description |
|---|---|
| `--revoke-sessions` | Delete all active database sessions of the user after changing the password. |
| password options | Except `--no-password`. |

### `set-flags USER`

| Option | Description |
|---|---|
| `--active` / `--no-active` | Set `is_active`. |
| `--staff` / `--no-staff` | Set `is_staff`. |
| `--superuser` / `--no-superuser` | Set `is_superuser`. `--superuser` also sets `is_staff` unless `--no-staff` is given. |
| `--force` | Bypass the [last-superuser guard](ARCHITECTURE.md#last-superuser-guard). |

At least one flag is required.

### `activate USER`

Set `is_active=True`.

### `deactivate USER`

| Option | Description |
|---|---|
| `--revoke-sessions` | Also delete the user's active database sessions. |
| `--force` | Bypass the last-superuser guard. |

### `delete-user USER`

| Option | Description |
|---|---|
| `--yes` | Skip the `[y/N]` confirmation (required when stdin is not a terminal). |
| `--force` | Bypass the last-superuser guard. |

Deleting follows normal Django `on_delete` rules, so related rows may be removed.

### `revoke-sessions USER`

Delete every unexpired database session belonging to the user. Prints the number removed.
Requires `django.contrib.sessions` in `INSTALLED_APPS` and the database session backend.

---

## Permission actions

### `grant-perm USER PERM [PERM ...]` / `revoke-perm USER PERM [PERM ...]`

Add or remove **direct** permissions. Already-present (or already-absent) permissions are reported as
`No change`. Group-inherited permissions are not affected by `revoke-perm`.

### `list-perms`

| Option | Description |
|---|---|
| `--app LABEL` | Filter by app label. |
| `--model NAME` | Filter by model name (lower-case). |
| `--search TEXT` | Substring of codename or human-readable name. |
| `--format` | See above. |

Columns: `permission`, `name`, `model`.

### `who-can PERM`

Active users that hold `PERM` as a superuser, directly or through a group. Same columns as `list-users`.

---

## Group actions

### `create-group NAME`

| Option | Description |
|---|---|
| `--perm PERM` `[...]` | Permissions to attach. |
| `--exist-ok` | Do not fail if the group already exists (permissions are still added). |

### `delete-group NAME`

| Option | Description |
|---|---|
| `--yes` | Skip confirmation. |
| `--force` | Delete even if the group has members. |

### `rename-group OLD NEW`

Fails if `NEW` already exists.

### `list-groups`

| Option | Description |
|---|---|
| `--search TEXT` | Substring of the group name. |
| `--format` | See above. |

Columns: `id`, `name`, `users`, `permissions`.

### `show-group NAME`

Members and permissions of a group. Supports `--format`.

### `group-grant NAME PERM [PERM ...]` / `group-revoke NAME PERM [PERM ...]`

Add or remove permissions on a group. Idempotent.

### `add-to-group USER GROUP [GROUP ...]`

| Option | Description |
|---|---|
| `--create-missing` | Create groups that do not exist instead of failing. |

### `remove-from-group USER GROUP [GROUP ...]`

All named groups must exist; groups the user does not belong to are ignored.

---

## Bulk actions

### `import-users FILE`

`FILE` is `.csv` (UTF-8, header row required, BOM tolerated) or `.json` (a list of objects, or
`{"users": [...]}`).

| Option | Description |
|---|---|
| `--update` | Update users that already exist (additive for groups and permissions). |
| `--dry-run` | Process everything, then roll back. |
| `--create-missing-groups` | Create groups referenced by the file. |
| `--skip-password-validation` | Do not run password validators. |
| `--format` | Format of the per-row report. |

Row statuses: `created`, `updated`, `skipped` (exists, no `--update`), `error` (see the message column).
A summary line such as `created=2, updated=0, skipped=1, error=0` is written to stderr.

Columns: see the [user guide](USER_GUIDE.md#513-bulk-import).

### `export-users`

| Option | Description |
|---|---|
| `--format json\|csv` | Default `json`. |
| `--output FILE` | Write to a file instead of stdout. |
| `--group NAME` | Only members of the group. |
| `--active` / `--no-active` | Filter on `is_active`. |

Columns: `username`, `email`, `first_name`, `last_name`, `is_active`, `is_staff`, `is_superuser`, `groups`,
`permissions`. Password hashes are never exported.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Expected error: not found, already exists, invalid input, safety guard, aborted confirmation |
| `2` | Command line usage error |
| `3` | `check-user` failed a check, or `import-users` reported row errors |

## Audit log format

Each mutation emits one `INFO` record on the `usermgmt.audit` logger:

```
action=<name> actor=<os user> key='value' key='value' ...
```

| Action | Fields |
|---|---|
| `create_user` | `user`, `staff`, `superuser`, `active`, `groups` |
| `update_user` | `user`, `fields` |
| `set_password` | `user`, `revoked_sessions` |
| `set_flags` | `user`, changed flags |
| `delete_user` | `user` |
| `revoke_sessions` | `user`, `count` |
| `grant_permissions`, `revoke_permissions` | `user`, `perms` |
| `create_group`, `delete_group`, `rename_group` | `group` (or `old`, `new`), `perms` / `members` |
| `grant_group_permissions`, `revoke_group_permissions` | `group`, `perms` |
| `add_to_groups`, `remove_from_groups` | `user`, `groups` |
| `import_users` | `total`, `dry_run`, `errors` |

`actor` is the operating-system user running the command. Passwords and hashes are never logged.

## 👤 Author
        
[Hadi Cahyadi](mailto:cumulus13@gmail.com)
    

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/cumulus13)

[![Donate via Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/cumulus13)
 
[Support me on Patreon](https://www.patreon.com/cumulus13)