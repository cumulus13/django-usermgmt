# File: django-usermgmt/examples/settings_snippet.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-11-17 04:19:48 SE Asia Standard Time
# Description:
# License: MIT

# Add the following to your project's settings.py.
INSTALLED_APPS = [
    # ... your other apps ...
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",  # optional: only needed for the revoke-sessions feature
    "usermgmt",
]

# Audit log: every change to users, groups and permissions is written to the
# "usermgmt.audit" logger. Passwords are never logged.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"audit": {"format": "%(asctime)s %(message)s"}},
    "handlers": {
        "audit_file": {
            "class": "logging.handlers.WatchedFileHandler",
            "filename": "/var/log/app/usermgmt-audit.log",
            "formatter": "audit",
        }
    },
    "loggers": {
        "usermgmt.audit": {
            "handlers": ["audit_file"],
            "level": "INFO",
            "propagate": False,
        }
    },
}
