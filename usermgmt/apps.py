# File: django-usermgmt/usermgmt/apps.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-11-17 04:19:48 SE Asia Standard Time
# Description:
# License: MIT

from django.apps import AppConfig


class UsermgmtConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "usermgmt"
    verbose_name = "User Management CLI"
