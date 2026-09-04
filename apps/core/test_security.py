"""The security posture spec §11 asks for, asserted — build-order step 14.

Grouped by the promise being kept rather than by the module keeping it, because
most of these promises are kept in more than one place and a test tied to one
implementation stops testing the promise the moment somebody moves it.

Four of them:

* **The audit log cannot be altered.** Four independent layers, and the one
  this file adds is the one nobody can remove without changing the schema.
* **Every surface declares what it may do.** A CSP per surface, and the right
  one on each.
* **Uploads never leave through the front door.** ``MEDIA_ROOT`` is not routed.
* **Misconfiguration is loud.** The deploy checks fire on the things that are
  silent at run time and expensive later.
"""

from unittest import mock

from django.contrib.auth.models import Group
from django.core.checks import Error, Warning, run_checks
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user, verify_otp
from apps.core import checks as core_checks
from apps.core.choices import AuditAction
from apps.core.models import AppendOnlyError, AuditLog
from apps.core.services import record_audit

# ---------------------------------------------------------------------------
# The audit log is append-only — spec §11
# ---------------------------------------------------------------------------


class AuditLogStorageTests(TestCase):
    """The guarantee below Django, where a shell cannot reach around it.

    ``AppendOnlyModel`` already refuses ``save()`` and ``delete()``, and the
    ``change``/``delete`` permissions do not exist. All of that is Python.
    ``QuerySet.update()`` and ``QuerySet.delete()`` call neither method, and
    ``psql`` calls nothing at all — which is the gap the triggers close.
    """

    def setUp(self):
        self.user = make_user("auditor@maxifyfx.com", Role.FINANCE_ADMIN)
        self.entry = record_audit(
            action=AuditAction.LOGIN, target=self.user, actor=self.user
        )

    def test_the_triggers_are_installed(self):
        present = core_checks.installed_auditlog_triggers(connection)

        for name in core_checks.AUDITLOG_TRIGGERS:
            self.assertIn(name, present)

    def test_a_queryset_update_is_refused_by_the_database(self):
        # The path that bypasses Model.save() entirely.
        with self.assertRaises(IntegrityError), transaction.atomic():
            AuditLog.objects.filter(pk=self.entry.pk).update(action=AuditAction.LOGIN_FAILED)

        self.assertEqual(
            AuditLog.objects.get(pk=self.entry.pk).action, AuditAction.LOGIN
        )

    def test_a_queryset_delete_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AuditLog.objects.filter(pk=self.entry.pk).delete()

        self.assertTrue(AuditLog.objects.filter(pk=self.entry.pk).exists())

    def test_raw_sql_is_refused_too(self):
        # There is no Django ORM in this path at all. If this passes, the
        # guarantee belongs to the storage engine rather than to the framework.
        # DatabaseError is the widest thing both backends agree on: PostgreSQL
        # raises InternalError for a RAISE EXCEPTION and SQLite raises
        # IntegrityError for RAISE(ABORT).
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM core_auditlog WHERE id = %s", [self.entry.pk])

        self.assertTrue(AuditLog.objects.filter(pk=self.entry.pk).exists())

    def test_inserting_still_works(self):
        # A table nobody can write to is not an append-only log, it is a
        # broken one.
        second = record_audit(
            action=AuditAction.RATE_CHANGE, target=self.user, actor=self.user
        )

        self.assertNotEqual(second.pk, self.entry.pk)
        self.assertEqual(AuditLog.objects.count(), 2)

    def test_the_python_guard_is_still_there(self):
        # Belt and braces, and the one that produces a readable error.
        self.entry.action = AuditAction.RATE_CHANGE
        with self.assertRaises(AppendOnlyError):
            self.entry.save()
        with self.assertRaises(AppendOnlyError):
            self.entry.delete()

    def test_the_check_notices_a_dropped_trigger(self):
        with mock.patch.object(
            core_checks, "installed_auditlog_triggers", return_value=set()
        ):
            issues = core_checks.check_auditlog_is_append_only(None, databases=["default"])

        self.assertTrue(issues)
        self.assertEqual(issues[0].id, "core.E021")

    def test_the_check_passes_on_a_healthy_database(self):
        self.assertEqual(
            core_checks.check_auditlog_is_append_only(None, databases=["default"]), []
        )

    def test_the_check_is_silent_when_asked_about_no_database(self):
        # It runs during `migrate`, before the migration that installs the
        # triggers has had a chance to.
        self.assertEqual(core_checks.check_auditlog_is_append_only(None), [])


# ---------------------------------------------------------------------------
# Content-Security-Policy per surface — spec §11
# ---------------------------------------------------------------------------


class SecurityHeaderTestCase(TestCase):
    def setUp(self):
        sync_role_groups()
        self.staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)

    def policy(self, response) -> str:
        self.assertIn(
            "Content-Security-Policy",
            response.headers,
            "Every response declares what the page may do (spec §11).",
        )
        return response.headers["Content-Security-Policy"]


class InternalPanelHeaderTests(SecurityHeaderTestCase):
    def setUp(self):
        super().setUp()
        verify_otp(self.client, self.staff)

    def test_the_panel_forbids_inline_script(self):
        # The panels load one stylesheet and one script from us and nothing
        # else, so this costs nothing and takes a whole class of injection off
        # the table.
        policy = self.policy(self.client.get(reverse("finance:dashboard")))

        self.assertIn("script-src 'self'", policy)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", policy)

    def test_the_panel_may_not_be_framed(self):
        policy = self.policy(self.client.get(reverse("finance:dashboard")))

        self.assertIn("frame-ancestors 'none'", policy)

    def test_the_panel_declares_the_rest_of_the_policy(self):
        policy = self.policy(self.client.get(reverse("finance:dashboard")))

        for directive in (
            "default-src 'self'",
            "base-uri 'none'",
            "object-src 'none'",
            "form-action 'self'",
        ):
            self.assertIn(directive, policy)

    def test_style_attributes_are_the_one_relaxation(self):
        # Named rather than hidden: the templates carry style="" attributes and
        # the two-factor pages ship a <style> block. A style attribute cannot
        # execute; the directive that matters is script-src, tested above.
        policy = self.policy(self.client.get(reverse("finance:dashboard")))

        self.assertIn("style-src 'self' 'unsafe-inline'", policy)

    def test_the_transport_headers_are_set(self):
        response = self.client.get(reverse("finance:dashboard"))

        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Cross-Origin-Opener-Policy"], "same-origin")

    def test_the_merchant_panel_gets_the_same_policy(self):
        merchant_user = make_user("m@example.com", Role.MERCHANT)
        from apps.merchants.models import Merchant

        Merchant.objects.create(name="تاجر", user=merchant_user)
        verify_otp(self.client, merchant_user)

        policy = self.policy(self.client.get(reverse("merchant_panel:queue")))

        self.assertIn("script-src 'self'", policy)
        self.assertIn("frame-ancestors 'none'", policy)

    def test_the_login_page_is_covered_before_anybody_signs_in(self):
        self.client.logout()

        policy = self.policy(self.client.get(reverse("two_factor:login")))

        self.assertIn("frame-ancestors 'none'", policy)


class AdminHeaderTests(SecurityHeaderTestCase):
    def test_the_admin_keeps_framing_and_relaxes_only_script(self):
        # Its widgets ship inline handlers we do not control, and a policy that
        # breaks the admin is a policy somebody switches off.
        admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN, is_superuser=True)
        admin.is_staff = True
        admin.save()
        verify_otp(self.client, admin)

        policy = self.policy(self.client.get(reverse("admin:index"), follow=True))

        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("script-src 'self' 'unsafe-inline'", policy)


@override_settings(B2CORE_ORIGIN="https://portal.b2core.test")
class PortalHeaderTests(SecurityHeaderTestCase):
    def test_only_b2core_may_frame_the_portal(self):
        response = self.client.get(reverse("portal:bootstrap"))

        self.assertIn("frame-ancestors https://portal.b2core.test", self.policy(response))

    def test_the_portal_does_not_also_send_x_frame_options(self):
        # DENY would win over the CSP and break the embed outright.
        response = self.client.get(reverse("portal:bootstrap"))

        self.assertNotIn("X-Frame-Options", response.headers)

    def test_the_internal_panel_is_not_given_the_b2core_origin(self):
        verify_otp(self.client, self.staff)

        policy = self.policy(self.client.get(reverse("finance:dashboard")))

        self.assertNotIn("b2core", policy)


# ---------------------------------------------------------------------------
# Uploads never leave through the front door — spec §11
# ---------------------------------------------------------------------------


class MediaRoutingTests(TestCase):
    def test_media_root_is_not_routed(self):
        from django.conf import settings
        from django.urls import Resolver404, resolve

        with self.assertRaises(Resolver404):
            resolve(settings.MEDIA_URL)

    def test_the_check_fires_if_media_is_moved_under_static(self):
        with override_settings(MEDIA_URL="/static/uploads/", STATIC_URL="/static/"):
            issues = core_checks.check_media_is_never_routed(None)

        self.assertEqual([issue.id for issue in issues], ["core.E001"])

    def test_the_check_is_quiet_as_configured(self):
        self.assertEqual(core_checks.check_media_is_never_routed(None), [])


# ---------------------------------------------------------------------------
# Misconfiguration is loud — spec §11
# ---------------------------------------------------------------------------


class DeployCheckTests(TestCase):
    """Each of these is silent at run time. That is why they are checks."""

    def ids(self, issues):
        return [issue.id for issue in issues]

    def test_a_per_process_cache_is_reported(self):
        # Both rate limiters count in the cache. Behind four workers, LocMem
        # gives an attacker four times the allowance.
        with override_settings(
            CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
        ):
            self.assertIn("core.W010", self.ids(core_checks.check_shared_cache(None)))

    def test_a_shared_cache_passes(self):
        with override_settings(
            CACHES={"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache"}}
        ):
            self.assertEqual(core_checks.check_shared_cache(None), [])

    def test_an_unset_backup_directory_is_reported(self):
        with override_settings(BACKUP_DIR=""):
            self.assertIn("core.W011", self.ids(core_checks.check_backups(None)))

    def test_a_one_day_retention_is_reported(self):
        # Corruption is usually noticed after the corrupt copy has replaced the
        # good one.
        with override_settings(BACKUP_DIR="/srv/backups", BACKUP_RETENTION_DAYS=1):
            self.assertIn("core.W012", self.ids(core_checks.check_backups(None)))

    def test_a_configured_backup_passes(self):
        with override_settings(BACKUP_DIR="/srv/backups", BACKUP_RETENTION_DAYS=14):
            self.assertEqual(core_checks.check_backups(None), [])

    def test_a_wildcard_host_is_an_error(self):
        with override_settings(ALLOWED_HOSTS=["*"]):
            self.assertIn("core.E013", self.ids(core_checks.check_allowed_hosts(None)))

    def test_the_development_secret_key_is_an_error(self):
        with override_settings(SECRET_KEY="dev-only-insecure-key-do-not-use"):
            self.assertIn("core.E014", self.ids(core_checks.check_secret_key(None)))

    def test_a_short_secret_key_is_a_warning(self):
        with override_settings(SECRET_KEY="x" * 20):
            self.assertIn("core.W015", self.ids(core_checks.check_secret_key(None)))

    def test_a_proper_secret_key_passes(self):
        with override_settings(SECRET_KEY="k" * 64):
            self.assertEqual(core_checks.check_secret_key(None), [])

    def test_an_absurd_poll_interval_is_an_error(self):
        with override_settings(PANEL_POLL_SECONDS=1):
            self.assertIn("core.E003", self.ids(core_checks.check_panel_poll(None)))

    def test_a_poll_so_slow_it_is_not_live_is_a_warning(self):
        with override_settings(PANEL_POLL_SECONDS=600):
            self.assertIn("core.W004", self.ids(core_checks.check_panel_poll(None)))

    def test_ten_seconds_is_what_the_spec_asks_for_and_passes(self):
        with override_settings(PANEL_POLL_SECONDS=10):
            self.assertEqual(core_checks.check_panel_poll(None), [])

    def test_removing_the_login_lockout_is_reported(self):
        with override_settings(
            AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"]
        ):
            self.assertIn("core.W005", self.ids(core_checks.check_login_throttle(None)))

    def test_a_lockout_that_fires_on_typos_is_an_error(self):
        with override_settings(LOGIN_FAILURE_LIMIT=2):
            self.assertIn("core.E006", self.ids(core_checks.check_login_throttle(None)))


class ShippedConfigurationTests(TestCase):
    """The settings actually shipped pass their own always-on checks."""

    def test_manage_py_check_is_clean(self):
        issues = [
            issue
            for issue in run_checks(include_deployment_checks=False)
            if isinstance(issue, (Error, Warning)) and issue.id and issue.id.startswith("core.")
        ]

        self.assertEqual(issues, [], f"Unexpected core checks: {issues}")

    def test_the_lockout_backend_is_the_one_in_use(self):
        from django.conf import settings

        self.assertEqual(
            settings.AUTHENTICATION_BACKENDS,
            ["apps.accounts.throttling.ThrottledModelBackend"],
        )


class RolePermissionTests(TestCase):
    """Spec §11: the audit log has no update or delete path anywhere.

    Including through a permission somebody could be granted — the model's
    ``default_permissions`` is what makes that structural rather than a policy.
    """

    def test_change_and_delete_permissions_do_not_exist(self):
        from django.contrib.auth.models import Permission

        codenames = set(
            Permission.objects.filter(
                content_type__app_label="core", content_type__model="auditlog"
            ).values_list("codename", flat=True)
        )

        self.assertEqual(codenames, {"add_auditlog", "view_auditlog"})

    def test_no_role_group_holds_a_write_on_the_audit_log(self):
        sync_role_groups()

        for group in Group.objects.all():
            held = set(
                group.permissions.filter(
                    content_type__app_label="core", content_type__model="auditlog"
                ).values_list("codename", flat=True)
            )
            with self.subTest(group=group.name):
                self.assertFalse(held - {"add_auditlog", "view_auditlog"})
