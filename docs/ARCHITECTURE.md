# Architecture

This document explains how `django-usermgmt` is built and why. It is aimed at maintainers and at reviewers who
need to assess the module before deploying it.

## Contents

1. [Goals](#goals)
2. [Layers](#layers)
3. [Module map](#module-map)
4. [Error model](#error-model)
5. [Key flows](#key-flows)
6. [Last-superuser guard](#last-superuser-guard)
7. [Permission resolution and `who-can`](#permission-resolution-and-who-can)
8. [Transactions and consistency](#transactions-and-consistency)
9. [Custom user model support](#custom-user-model-support)
10. [Design decisions](#design-decisions)

---

## Goals

| Goal | How it is met |
|---|---|
| Robust | Validation before writes, atomic transactions, per-row isolation in bulk import |
| Safe | Last-superuser guard, confirmations, no secrets in argv by default or in logs |
| Reusable | All logic in a framework-level service layer with no I/O |
| Automatable | Stable exit codes, JSON/CSV output, no prompts without a TTY |
| Portable | Only public Django APIs, `USERNAME_FIELD`-aware, no third-party dependencies |

## Layers

```mermaid
flowchart TB
    subgraph Presentation["Presentation layer: management/commands/usermgr.py"]
        ARG["Argument parsing<br/>(argparse sub-commands)"]
        PW["Password input<br/>(stdin / env / prompt)"]
        OUT["Rendering<br/>(table / json / csv)"]
        CONF["Confirmations<br/>(--yes, TTY checks)"]
    end
    subgraph Service["Service layer: services.py"]
        USERS["User operations"]
        PERMS["Permission operations"]
        GROUPS["Group operations"]
        BULK["Bulk import / export"]
        GUARD["Safety guards"]
        AUD["Audit logging"]
    end
    subgraph Django["Django"]
        ORM["ORM and transactions"]
        AUTH["django.contrib.auth<br/>password validators, hashers"]
        SESS["django.contrib.sessions"]
    end
    DB[("Database")]

    Presentation -->|"plain Python calls"| Service
    Service --> ORM
    Service --> AUTH
    Service --> SESS
    ORM --> DB
```

[![Layers](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture3.jpg)](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture3.jpg)

Rules of the layering:

- The presentation layer never touches the ORM directly (other than reading `USERNAME_FIELD` for one rename option).
- The service layer never prints, prompts or reads `sys.stdin`. It receives values and returns data or raises.
- Business rules (guards, validation, idempotency) live **only** in the service layer, so every caller gets them.

## Module map

```mermaid
flowchart LR
    subgraph pkg["usermgmt package"]
        apps["apps.py<br/>AppConfig"]
        services["services.py"]
        cmd["management/commands/usermgr.py"]
        tests["tests/test_usermgr.py"]
    end
    cmd --> services
    tests --> services
    tests --> cmd
    services --> dj["django.contrib.auth<br/>django.db<br/>django.utils"]
    cmd --> dj2["django.core.management.base"]
```

[![Modulee map](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture4.jpg)](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture4.jpg)

Inside `services.py`, functions are grouped by concern:

```mermaid
flowchart TD
    S["services.py"] --> H["Helpers<br/>_has_field, to_bool, split_list,<br/>_audit, _perm_str"]
    S --> U["Users<br/>get_user, list_users, serialize_user,<br/>create_user, update_user, set_password,<br/>set_flags, delete_user, revoke_sessions,<br/>check_user"]
    S --> P["Permissions<br/>resolve_permissions, list_permissions,<br/>grant_permissions, revoke_permissions, who_can"]
    S --> G["Groups<br/>get_group, create_group, delete_group,<br/>rename_group, list_groups, serialize_group,<br/>grant/revoke_group_permissions,<br/>add_to_groups, remove_from_groups"]
    S --> B["Bulk<br/>import_users, export_users"]
```

[![Architecture5](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture5.jpg)](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture5.jpg)

## Error model

All expected failures derive from `UserMgmtError`. The command converts them into `CommandError`
(exit code 1). Unexpected exceptions propagate with a traceback, except inside bulk import where each row is
isolated and reported.

```mermaid
classDiagram
    Exception <|-- UserMgmtError
    UserMgmtError <|-- NotFoundError
    UserMgmtError <|-- AlreadyExistsError
    UserMgmtError <|-- InvalidInputError
    UserMgmtError <|-- SafetyError
    class UserMgmtError {
        base class for user-facing errors
    }
    class NotFoundError {
        user or group or permission missing
    }
    class AlreadyExistsError {
        duplicate user or group or e-mail
    }
    class InvalidInputError {
        bad value or validator failure
    }
    class SafetyError {
        blocked by a guard (override with force)
    }
```

[![Error model](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture6.jpg)](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture6.jpg)

## Key flows

### `create-user`

```mermaid
sequenceDiagram
    actor Op as Operator
    participant CLI as usermgr command
    participant SVC as services.create_user
    participant PV as Password validators
    participant DB as Database
    participant LOG as Audit logger

    Op->>CLI: create-user budi --group editors
    CLI->>CLI: resolve password (stdin / env / prompt)
    CLI->>SVC: create_user(budi, password, groups, ...)
    SVC->>DB: does the username already exist?
    DB-->>SVC: no
    SVC->>SVC: build unsaved candidate, full_clean()
    SVC->>PV: validate_password(password, candidate)
    PV-->>SVC: ok
    rect rgb(235, 245, 255)
        Note over SVC,DB: single atomic transaction
        SVC->>DB: manager.create_user(...)
        SVC->>DB: add to groups (create if allowed)
        SVC->>DB: grant direct permissions
    end
    SVC->>LOG: action=create_user ...
    SVC-->>CLI: user
    CLI-->>Op: Created user 'budi' (id=7)
```

[![create-user](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture7.jpg)](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture7.jpg)

Validation happens **before** the transaction so that obvious mistakes never open one. The transaction still
protects against races: an `IntegrityError` from a concurrent creation is translated to `AlreadyExistsError`.

### `import-users`

```mermaid
sequenceDiagram
    participant CLI as usermgr command
    participant SVC as services.import_users
    participant DB as Database

    CLI->>CLI: load CSV or JSON rows
    CLI->>SVC: import_users(rows, dry_run, ...)
    rect rgb(235, 245, 255)
        Note over SVC,DB: outer transaction
        loop each row
            rect rgb(245, 245, 235)
                Note over SVC,DB: savepoint per row
                SVC->>DB: create or update user, groups, permissions
                alt row ok
                    SVC->>SVC: record created / updated / skipped
                else any exception
                    SVC->>DB: rollback to savepoint
                    SVC->>SVC: record error and message
                end
            end
        end
        opt dry_run
            SVC->>DB: mark outer transaction for rollback
        end
    end
    SVC-->>CLI: per-row results
    CLI->>CLI: print report, exit 3 if any error
```

[![import-user](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture8.jpg)](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture8.jpg)


### Group membership change

```mermaid
flowchart TD
    A["add-to-group USER G1 G2"] --> B["Normalise and de-duplicate names"]
    B --> C{"All groups exist?"}
    C -- no --> D{"--create-missing?"}
    D -- no --> E["NotFoundError listing the missing groups"]
    D -- yes --> F["Create missing groups"]
    C -- yes --> G
    F --> G["Compute groups the user is not yet in"]
    G --> H["user.groups.add(...) inside atomic()"]
    H --> I["Audit: add_to_groups"]
```

[![Group membership change](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture9.jpg)](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture9.jpg)

## Last-superuser guard

Locking yourself out of the system is the most damaging administrative mistake, so three operations are guarded:
`delete-user`, `deactivate` (or `set-flags --no-active`) and `set-flags --no-superuser`.

```mermaid
flowchart TD
    A["Operation on USER"] --> B{"--force given?"}
    B -- yes --> ALLOW["Proceed"]
    B -- no --> C{"USER is an active superuser?"}
    C -- no --> ALLOW
    C -- yes --> D{"Other active superusers exist?"}
    D -- yes --> ALLOW
    D -- no --> BLOCK["SafetyError: last active superuser"]
```

[![Last-superuser guard](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture10.jpg)](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture10.jpg)

The check is a count query that excludes the target user. It is not serialised against concurrent administrators,
which is acceptable for an operator-driven CLI but worth knowing.

## Permission resolution and `who-can`

`resolve_permissions` turns user input into `Permission` rows:

```mermaid
flowchart TD
    I["Input string"] --> A{"Contains '.'?"}
    A -- no --> E1["InvalidInputError"]
    A -- yes --> B["Split into app_label and codename"]
    B --> C{"codename is '*'?"}
    C -- yes --> D["All permissions of the app"]
    D --> D1{"Any found?"}
    D1 -- no --> E2["NotFoundError"]
    D1 -- yes --> OUT["List of Permission objects"]
    C -- no --> F["Lookup by app_label and codename"]
    F --> G{"Matches"}
    G -- "0" --> E3["NotFoundError<br/>(hint: run migrate)"]
    G -- "more than 1" --> E4["InvalidInputError: ambiguous"]
    G -- "1" --> OUT
```

[![Permission](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture11.jpg)](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture11.jpg)

`who-can` reproduces the semantics of Django's `ModelBackend` in a single query for **active** users:

```mermaid
flowchart LR
    U["active users"] --> Q{"is_superuser<br/>OR direct permission<br/>OR group permission"}
    Q --> R["distinct, ordered by username"]
```

[![architecture12](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture12.jpg)](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture12.jpg)

## Transactions and consistency

| Operation | Boundary |
|---|---|
| `create_user` | One `atomic()` around user creation, group membership and permission grants |
| `update_user`, `set_flags`, `set_password`, `delete_user` | One `atomic()` around the write |
| Permission and group changes | `atomic()` around the many-to-many update |
| `import_users` | Outer `atomic()` plus one savepoint per row; `dry_run` rolls back the outer block |
| `revoke_sessions` | Per-session delete; not transactional (sessions are disposable) |

Audit records are emitted after the transaction block returns, so a rolled-back change is not logged as done.
(Exception: in `import_users --dry-run`, inner operations log as they run; the final `import_users` record
carries `dry_run=True`.)

## Custom user model support

```mermaid
flowchart TD
    A["get_user_model()"] --> B["USERNAME_FIELD<br/>(login attribute)"]
    A --> C["_has_field(name)<br/>feature detection"]
    B --> D["Lookups, uniqueness, create_user kwargs"]
    C --> E["email, first_name, last_name,<br/>is_staff, is_superuser, is_active,<br/>date_joined, last_login"]
    E --> F{"Field exists?"}
    F -- yes --> G["Use it"]
    F -- no --> H["Skip silently, or raise if the<br/>caller explicitly asked for it"]
    A --> I["--field KEY=VALUE<br/>for REQUIRED_FIELDS and extras"]
```

[![Custom user model support](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture13.jpg)](https://raw.githubusercontent.com/licface15/images/refs/heads/main/django-usermgmt/architecture13.jpg)

Requirements for a custom model: it must inherit `PermissionsMixin` (for groups and permissions) and its manager
must provide `create_user(<USERNAME_FIELD>=..., password=..., **extra)`.

## Design decisions

| Decision | Rationale |
|---|---|
| One command with sub-actions rather than many commands | One discoverable entry point (`usermgr -h`), shared options and helpers |
| Service layer separate from the CLI | Testable without parsing arguments; reusable from Celery, shell and views |
| Passwords never positional | Positional secrets leak through shell history and `ps` |
| Idempotent grants and group changes | Safe to re-run provisioning scripts |
| `full_clean()` on a candidate before creation | `create_user()` skips model validation; this catches bad e-mails and usernames early |
| Deactivate preferred over delete in docs | Preserves foreign keys and the audit trail |
| Audit via the `logging` module | Lets operators route records to files, syslog or SIEM without code changes |
| No `--database` option (yet) | Keeps every query on the default manager; see [limitations](../README.md#known-limitations) |

## 👤 Author
        
[Hadi Cahyadi](mailto:cumulus13@gmail.com)
    

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/cumulus13)

[![Donate via Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/cumulus13)
 
[Support me on Patreon](https://www.patreon.com/cumulus13)