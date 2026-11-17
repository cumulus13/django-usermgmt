# File: django-usermgmt/usermgmt/tests/test_usermgr.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-11-17 04:19:48 SE Asia Standard Time
# Description:
# License: MIT

import os
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import CommandError, call_command
from django.test import TestCase

from usermgmt import services as svc

User = get_user_model()
STRONG = "Xk9#mPq2-vLz!7"


def run(*args, stdin_tty=False):
    out, err = StringIO(), StringIO()
    call_command("usermgr", *args, stdout=out, stderr=err)
    return out.getvalue()


class ServiceTests(TestCase):
    def test_create_regular_user(self):
        u = svc.create_user("alice", password=STRONG, email="a@example.com")
        self.assertFalse(u.is_staff or u.is_superuser)
        self.assertTrue(u.check_password(STRONG))

    def test_duplicate_user(self):
        svc.create_user("alice", password=STRONG)
        with self.assertRaises(svc.AlreadyExistsError):
            svc.create_user("alice", password=STRONG)

    def test_weak_password_rejected_unless_skipped(self):
        with self.assertRaises(svc.InvalidInputError):
            svc.create_user("bob", password="123")
        self.assertFalse(User.objects.filter(username="bob").exists())

    def test_no_password_gives_unusable_password(self):
        self.assertFalse(svc.create_user("sso", password=None).has_usable_password())

    def test_superuser_implies_staff(self):
        u = svc.create_user("root", password=STRONG, is_superuser=True)
        self.assertTrue(u.is_staff)

    def test_last_superuser_guard(self):
        u = svc.create_user("root", password=STRONG, is_superuser=True)
        with self.assertRaises(svc.SafetyError):
            svc.delete_user(u)
        with self.assertRaises(svc.SafetyError):
            svc.set_flags(u, is_active=False)
        svc.create_user("root2", password=STRONG, is_superuser=True)
        svc.delete_user(u)  # allowed now

    def test_groups_and_permissions(self):
        u = svc.create_user("carol", password=STRONG)
        with self.assertRaises(svc.NotFoundError):
            svc.add_to_groups(u, ["editors"])
        svc.add_to_groups(u, ["editors"], create_missing=True)
        svc.grant_group_permissions("editors", ["auth.view_user"])
        self.assertTrue(User.objects.get(pk=u.pk).has_perm("auth.view_user"))
        self.assertIn(u, list(svc.who_can("auth.view_user")))
        changed, same = svc.grant_permissions(u, ["auth.add_group"])
        self.assertEqual((changed, same), (["auth.add_group"], []))
        changed, same = svc.grant_permissions(u, ["auth.add_group"])
        self.assertEqual((changed, same), ([], ["auth.add_group"]))

    def test_delete_group_with_members_requires_force(self):
        u = svc.create_user("dave", password=STRONG)
        svc.add_to_groups(u, ["ops"], create_missing=True)
        with self.assertRaises(svc.SafetyError):
            svc.delete_group("ops")
        svc.delete_group("ops", force=True)
        self.assertFalse(Group.objects.filter(name="ops").exists())

    def test_import_dry_run_rolls_back_and_isolates_errors(self):
        rows = [
            {"username": "u1", "password": STRONG, "groups": "g1;g2"},
            {"username": "u2", "password": "1"},  # weak
        ]
        res = svc.import_users(rows, create_missing_groups=True, dry_run=True)
        self.assertEqual([r["status"] for r in res], ["created", "error"])
        self.assertFalse(User.objects.filter(username="u1").exists())
        res = svc.import_users(rows, create_missing_groups=True)
        self.assertTrue(User.objects.filter(username="u1").exists())
        self.assertFalse(User.objects.filter(username="u2").exists())


class CommandTests(TestCase):
    @mock.patch.dict(os.environ, {"PW": STRONG})
    def test_create_list_show(self):
        run("create-user", "erin", "--email", "e@example.com", "--password-env", "PW", "--group", "qa",
            "--create-missing-groups")
        self.assertIn("erin", run("list-users"))
        self.assertIn('"username": "erin"', run("show-user", "erin", "--format", "json"))

    def test_create_requires_password_source_when_non_interactive(self):
        with mock.patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(CommandError):
                run("create-user", "frank")

    def test_delete_requires_yes_when_non_interactive(self):
        svc.create_user("gina", password=STRONG)
        with mock.patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(CommandError):
                run("delete-user", "gina")
        run("delete-user", "gina", "--yes")
        self.assertFalse(User.objects.filter(username="gina").exists())

    def test_check_user_exit_code(self):
        svc.create_user("hank", password=STRONG)
        with self.assertRaises(CommandError) as ctx:
            run("check-user", "hank", "--perm", "auth.add_group")
        self.assertEqual(ctx.exception.returncode, 3)
