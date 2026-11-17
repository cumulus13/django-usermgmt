# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] - 2026-11-16

### Added
- `usermgr` management command with 26 actions covering users, permissions, groups and bulk operations.
- Service layer (`usermgmt.services`) reusable from scripts, Celery tasks and tests.
- Support for custom user models via `USERNAME_FIELD` and `--field KEY=VALUE`.
- Safe password input (interactive prompt, stdin, environment variable) with `AUTH_PASSWORD_VALIDATORS` enforcement.
- Last-active-superuser guard on delete, deactivate and demote.
- Bulk import (CSV/JSON) with per-row savepoints and `--dry-run`; export to CSV/JSON.
- Audit logging through the `usermgmt.audit` logger.
- `table`, `json` and `csv` output formats.
- Documentation: README, user guide, command reference, architecture, security and troubleshooting guides.
