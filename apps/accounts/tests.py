"""Tests for roles, permissions and mandatory 2FA (build-order steps 1–2)."""

from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import Client as PortalClient
from apps.accounts.models import Role, User
from apps.accounts.permissions import (
    GLOBAL_DENY,
    MERCHANT_FORBIDDEN,
    PermissionMatrixError,
    assert_merchant_anonymity,
    expected_permissions,
    sync_role_groups,
)
from apps.core.models import AuditLog


def make_user(email, role, **extra):
    return User.objects.create_user(
        email=email, password="portal-test-pass-12345", full_name=f"Test {role}", role=role, **extra
    )


def verify_otp(test_client, user):
    """Put a confirmed TOTP device on ``user`` and mark the session verified."""
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    test_client.force_login(user)
    session = test_client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    return device


class RoleGroupTests(TestCase):
    def test_bootstrap_creates_the_three_role_groups(self):
        sync_role_groups()
        self.assertEqual(
            set(Group.objects.values_list("name", flat=True)),
            {"finance_admin", "finance_staff", "merchant"},
        )

    def test_role_is_mirrored_into_group_membership(self):
        sync_role_groups()
        user = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)
        self.assertEqual(list(user.groups.values_list("name", flat=True)), ["finance_staff"])

    def test_changing_role_swaps_the_group(self):
        sync_role_groups()
        user = make_user("promoted@maxifyfx.com", Role.FINANCE_STAFF)
        user.role = Role.FINANCE_ADMIN
        user.save()
        self.assertEqual(list(user.groups.values_list("name", flat=True)), ["finance_admin"])

    def test_extra_groups_survive_a_role_change(self):
        sync_role_groups()
        extra = Group.objects.create(name="on_call")
        user = make_user("oncall@maxifyfx.com", Role.FINANCE_STAFF)
        user.groups.add(extra)
        user.role = Role.FINANCE_ADMIN
        user.save()
        self.assertEqual(
            set(user.groups.values_list("name", flat=True)), {"finance_admin", "on_call"}
        )

    def test_bootstrap_roles_command_is_idempotent(self):
        call_command("bootstrap_roles")
        first = Group.objects.get(name="merchant").permissions.count()
        call_command("bootstrap_roles")
        self.assertEqual(Group.objects.get(name="merchant").permissions.count(), first)


class ClientAnonymityTests(TestCase):
    """Spec §2 — merchants never see who the client is."""

    def setUp(self):
        sync_role_groups()

    def test_merchant_group_holds_no_identity_permission(self):
        merchant_perms = {
            f"{p.content_type.app_label}.{p.codename}"
            for p in Group.objects.get(name="merchant").permissions.all()
        }
        self.assertEqual(merchant_perms & MERCHANT_FORBIDDEN, set())

    def test_merchant_user_cannot_view_client_identity(self):
        merchant = make_user("merchant@example.com", Role.MERCHANT)
        merchant = User.objects.get(pk=merchant.pk)  # drop the permission cache
        for label in sorted(MERCHANT_FORBIDDEN):
            self.assertFalse(merchant.has_perm(label), f"merchant must not hold {label}")
        self.assertFalse(merchant.can_see_client_identity)

    def test_finance_roles_can_view_client_identity(self):
        for role in (Role.FINANCE_ADMIN, Role.FINANCE_STAFF):
            user = User.objects.get(pk=make_user(f"{role}@maxifyfx.com", role).pk)
            self.assertTrue(user.can_see_client_identity)
            self.assertTrue(user.has_perm("accounts.view_client_identity"))

    def test_matrix_refuses_to_leak_identity_to_merchants(self):
        """A future edit that grants a merchant an identity permission must fail loudly."""
        from apps.accounts import permissions as perms_module

        leaked = set(perms_module.MERCHANT_PERMISSIONS) | {"accounts.view_client_pii"}
        with override_settings():
            original = perms_module.ROLE_PERMISSIONS["merchant"]
            perms_module.ROLE_PERMISSIONS["merchant"] = leaked
            try:
                with self.assertRaises(PermissionMatrixError):
                    sync_role_groups()
            finally:
                perms_module.ROLE_PERMISSIONS["merchant"] = original

    def test_assert_merchant_anonymity_catches_a_direct_grant(self):
        merchant = make_user("sneaky@example.com", Role.MERCHANT)
        merchant.user_permissions.add(
            Permission.objects.get(codename="view_client_pii", content_type__app_label="accounts")
        )
        merchant = User.objects.get(pk=merchant.pk)
        with self.assertRaises(PermissionMatrixError):
            assert_merchant_anonymity(merchant)


class DenyListTests(TestCase):
    def setUp(self):
        sync_role_groups()

    def test_nobody_holds_a_denied_permission(self):
        for role in ("finance_admin", "finance_staff", "merchant"):
            self.assertEqual(
                expected_permissions(role) & GLOBAL_DENY,
                set(),
                f"{role} must not hold any denied permission",
            )

    def test_audit_log_has_no_change_or_delete_permission_at_all(self):
        codenames = set(
            Permission.objects.filter(
                content_type__app_label="core", content_type__model="auditlog"
            ).values_list("codename", flat=True)
        )
        self.assertEqual(codenames, {"add_auditlog", "view_auditlog"})


class TwoFactorEnforcementTests(TestCase):
    """Spec §11 — all internal accounts require 2FA."""

    def setUp(self):
        sync_role_groups()
        self.user = make_user("admin@maxifyfx.com", Role.FINANCE_ADMIN, is_superuser=True)

    def test_password_only_session_cannot_reach_the_admin(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("two_factor:setup"), response["Location"])

    def test_enrolled_but_unverified_session_is_sent_to_the_login_step(self):
        TOTPDevice.objects.create(user=self.user, name="default", confirmed=True)
        self.client.force_login(self.user)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("two_factor:login"), response["Location"])

    def test_verified_session_reaches_the_admin(self):
        verify_otp(self.client, self.user)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)

    def test_setup_page_stays_reachable_while_non_compliant(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("two_factor:setup"))
        self.assertEqual(response.status_code, 200)

    def test_json_client_gets_403_rather_than_a_redirect(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/anything", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "otp_setup_required")

    def test_healthz_needs_no_authentication(self):
        self.assertEqual(self.client.get(reverse("healthz")).status_code, 200)

    def test_every_internal_role_requires_two_factor(self):
        for role in Role.values:
            user = make_user(f"2fa-{role}@maxifyfx.com", role)
            self.assertTrue(user.requires_two_factor, role)

    @override_settings(TWO_FACTOR_REQUIRED_ROLES=[])
    def test_enforcement_can_be_switched_off_for_a_fixture_load(self):
        user = make_user("nofa@maxifyfx.com", Role.FINANCE_STAFF)
        self.assertFalse(user.requires_two_factor)


class AdminAccessTests(TestCase):
    def setUp(self):
        sync_role_groups()

    def test_merchant_cannot_open_the_request_admin(self):
        """The admin exposes client identity, so merchants must not reach it."""
        merchant = make_user("m2@example.com", Role.MERCHANT)
        verify_otp(self.client, merchant)
        response = self.client.get("/admin/transactions/request/")
        self.assertIn(response.status_code, (302, 403))

    def test_merchant_cannot_open_the_client_admin(self):
        merchant = make_user("m3@example.com", Role.MERCHANT)
        verify_otp(self.client, merchant)
        response = self.client.get("/admin/accounts/client/")
        self.assertIn(response.status_code, (302, 403))

    def test_finance_staff_cannot_manage_internal_users(self):
        staff = make_user("staff2@maxifyfx.com", Role.FINANCE_STAFF)
        verify_otp(self.client, staff)
        response = self.client.get("/admin/accounts/user/")
        self.assertIn(response.status_code, (302, 403))

    def test_finance_admin_can_manage_internal_users(self):
        admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN, is_superuser=True)
        verify_otp(self.client, admin)
        self.assertEqual(self.client.get("/admin/accounts/user/").status_code, 200)


class LoginAuditTests(TestCase):
    def setUp(self):
        sync_role_groups()

    def test_login_writes_an_audit_entry(self):
        user = make_user("audited@maxifyfx.com", Role.FINANCE_ADMIN)
        self.client.force_login(user)
        self.assertTrue(AuditLog.objects.filter(action="login", actor=user).exists())

    def test_failed_login_is_recorded_without_the_password(self):
        self.client.post(
            reverse("two_factor:login"),
            {
                "auth-username": "ghost@maxifyfx.com",
                "auth-password": "super-secret-value",
                "login_view-current_step": "auth",
            },
        )
        entry = AuditLog.objects.filter(action="login_failed").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.target_id, "ghost@maxifyfx.com")
        self.assertNotIn("super-secret-value", str(entry.before) + str(entry.after))


class ClientModelTests(TestCase):
    def test_masked_label_carries_no_identity(self):
        client = PortalClient.objects.create(
            b2core_id="b2c-1", display_name="اسم العميل", email="c@example.com"
        )
        self.assertNotIn("اسم العميل", client.masked_label)
        self.assertEqual(client.masked_label, "العميل")

    def test_b2core_id_is_unique(self):
        PortalClient.objects.create(b2core_id="b2c-2")
        with self.assertRaises(IntegrityError):
            PortalClient.objects.create(b2core_id="b2c-2")
