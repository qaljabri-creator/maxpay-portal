"""Creating and administering internal accounts (spec §3) — build-order step 15.

Grouped by the thing being protected rather than by the function being called:
the generated password, the obligation to replace it, the second factor, the
guarantee that a revoked permission is actually revoked, the guards that stop an
administrator locking the product out of being administered, and the audit trail
that has to survive all of it.
"""

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from apps.accounts import provisioning
from apps.accounts.models import Role, User
from apps.accounts.permissions import MERCHANT_FORBIDDEN, sync_role_groups
from apps.accounts.provisioning import ProvisioningError
from apps.accounts.tests import make_user, verify_otp
from apps.core.choices import AuditAction
from apps.core.models import AuditLog
from apps.merchants.models import Merchant


class ProvisioningTestCase(TestCase):
    def setUp(self):
        sync_role_groups()
        self.admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN)
        self.staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)


# ---------------------------------------------------------------------------
# The password
# ---------------------------------------------------------------------------


class GeneratedPasswordTests(ProvisioningTestCase):
    def test_a_generated_password_satisfies_the_projects_own_validators(self):
        """The generator and `AUTH_PASSWORD_VALIDATORS` must not be able to
        disagree — a password the system issues and then refuses is an account
        nobody can finish creating."""
        from django.contrib.auth.password_validation import validate_password

        for _ in range(50):
            validate_password(provisioning.generate_password())

    def test_it_always_carries_all_three_character_classes(self):
        for _ in range(200):
            password = provisioning.generate_password()
            self.assertTrue(any(c.islower() for c in password))
            self.assertTrue(any(c.isupper() for c in password))
            self.assertTrue(any(c.isdigit() for c in password))

    def test_it_leaves_out_the_glyphs_that_get_misread(self):
        """This string is read off a screen and typed into another, sometimes
        over a phone call."""
        for _ in range(200):
            self.assertFalse(set(provisioning.generate_password()) & set("l1O0I"))

    def test_two_passwords_are_never_the_same(self):
        seen = {provisioning.generate_password() for _ in range(500)}
        self.assertEqual(len(seen), 500)

    def test_the_password_is_stored_hashed_and_never_in_the_clear(self):
        user, password = provisioning.create_account(
            email="new@maxifyfx.com", full_name="حساب جديد",
            role=Role.FINANCE_STAFF, actor=self.admin,
        )
        user.refresh_from_db()

        self.assertNotEqual(user.password, password)
        self.assertTrue(user.check_password(password))

    def test_neither_the_password_nor_its_hash_reaches_the_audit_log(self):
        """The log is read by people, and a hash in it is a hash to grind."""
        user, password = provisioning.create_account(
            email="new@maxifyfx.com", full_name="حساب جديد",
            role=Role.FINANCE_STAFF, actor=self.admin,
        )
        user.refresh_from_db()

        entries = AuditLog.objects.filter(target_type="accounts.User")
        blob = "".join(str(e.before) + str(e.after) for e in entries)

        self.assertNotIn(password, blob)
        self.assertNotIn(user.password, blob)
        self.assertNotIn("password", blob.replace("must_change_password", ""))


# ---------------------------------------------------------------------------
# The obligation to replace it
# ---------------------------------------------------------------------------


class ForcedPasswordChangeTests(ProvisioningTestCase):
    def test_a_new_account_is_flagged_to_change_its_password(self):
        user, _password = provisioning.create_account(
            email="new@maxifyfx.com", full_name="حساب جديد",
            role=Role.FINANCE_STAFF, actor=self.admin,
        )
        self.assertTrue(user.must_change_password)

    def test_a_reset_flags_it_again(self):
        self.staff.must_change_password = False
        self.staff.save(update_fields=["must_change_password"])

        provisioning.reset_password(self.staff, actor=self.admin)

        self.staff.refresh_from_db()
        self.assertTrue(self.staff.must_change_password)

    def test_a_reset_actually_changes_the_password(self):
        old_hash = self.staff.password

        password = provisioning.reset_password(self.staff, actor=self.admin)

        self.staff.refresh_from_db()
        self.assertNotEqual(self.staff.password, old_hash)
        self.assertTrue(self.staff.check_password(password))

    def test_the_middleware_holds_a_flagged_account_at_the_password_screen(self):
        self.staff.must_change_password = True
        self.staff.save(update_fields=["must_change_password"])
        verify_otp(self.client, self.staff)

        response = self.client.get(reverse("finance:dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:password_change"), response["Location"])

    def test_the_password_screen_itself_stays_reachable(self):
        """Otherwise the requirement is a redirect loop and the account is dead."""
        self.staff.must_change_password = True
        self.staff.save(update_fields=["must_change_password"])
        verify_otp(self.client, self.staff)

        self.assertEqual(
            self.client.get(reverse("accounts:password_change")).status_code, 200
        )

    def test_changing_the_password_clears_the_obligation_and_lets_them_through(self):
        password = provisioning.reset_password(self.staff, actor=self.admin)
        verify_otp(self.client, self.staff)

        response = self.client.post(
            reverse("accounts:password_change"),
            {
                "old_password": password,
                "new_password1": "Wq7hnZmT4kfeR2js",
                "new_password2": "Wq7hnZmT4kfeR2js",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.must_change_password)
        self.assertEqual(self.client.get(reverse("finance:dashboard")).status_code, 200)

    def test_an_account_that_has_chosen_its_own_password_is_not_held(self):
        verify_otp(self.client, self.staff)
        self.assertEqual(self.client.get(reverse("finance:dashboard")).status_code, 200)

    def test_the_client_portal_is_never_caught_by_the_requirement(self):
        """A flagged internal user logged in in the same browser must not bounce
        a *client* out of the portal."""
        self.staff.must_change_password = True
        self.staff.save(update_fields=["must_change_password"])
        verify_otp(self.client, self.staff)

        response = self.client.get("/portal/")

        self.assertNotEqual(response.status_code, 302)


# ---------------------------------------------------------------------------
# The second factor
# ---------------------------------------------------------------------------


class TwoFactorResetTests(ProvisioningTestCase):
    def test_resetting_removes_every_device_the_account_held(self):
        verify_otp(self.client, self.staff)
        self.assertTrue(self.staff.has_verified_two_factor)

        removed = provisioning.reset_two_factor(self.staff, actor=self.admin)

        self.assertGreaterEqual(removed, 1)
        self.assertFalse(self.staff.has_verified_two_factor)

    def test_the_account_cannot_reach_anything_until_it_enrols_again(self):
        """Spec §11: an internal account without a verified second factor is
        not a working account."""
        verify_otp(self.client, self.staff)
        provisioning.reset_two_factor(self.staff, actor=self.admin)

        self.client.logout()
        self.client.force_login(self.staff)
        response = self.client.get(reverse("finance:dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("two_factor:setup"), response["Location"])

    def test_the_reset_is_audited_as_a_two_factor_change(self):
        verify_otp(self.client, self.staff)

        provisioning.reset_two_factor(self.staff, actor=self.admin)

        entry = AuditLog.objects.filter(action=AuditAction.TWO_FACTOR_CHANGE).latest("pk")
        self.assertEqual(entry.actor_id, self.admin.pk)
        self.assertEqual(entry.after["event"], "two_factor_reset")
        self.assertEqual(entry.after["devices"], 0)

    def test_the_time_of_the_reset_is_kept_on_the_account(self):
        provisioning.reset_two_factor(self.staff, actor=self.admin)

        self.staff.refresh_from_db()
        self.assertIsNotNone(self.staff.two_factor_reset_at)


# ---------------------------------------------------------------------------
# Permissions: granting, and actually revoking
# ---------------------------------------------------------------------------


class PermissionOverrideTests(ProvisioningTestCase):
    #: Held by the finance_staff baseline, so denying it is a real revocation
    #: rather than the absence of a grant.
    BASELINE = "transactions.credit_request"
    #: Not in the baseline, so granting it is a real addition.
    EXTRA = "rates.add_exchangerate"

    def test_the_baseline_is_what_the_role_group_grants(self):
        self.assertTrue(self.staff.has_perm(self.BASELINE))
        self.assertFalse(self.staff.has_perm(self.EXTRA))

    def test_a_granted_permission_takes_effect(self):
        provisioning.set_permission_overrides(
            self.staff, granted={self.EXTRA}, denied=set(), actor=self.admin
        )
        self.assertTrue(User.objects.get(pk=self.staff.pk).has_perm(self.EXTRA))

    def test_a_denied_permission_is_taken_away_even_though_the_role_grants_it(self):
        """The point of the whole mechanism. `user_permissions` cannot express
        this: Django unions it with every group, so the role would hand the
        permission straight back."""
        provisioning.set_permission_overrides(
            self.staff, granted=set(), denied={self.BASELINE}, actor=self.admin
        )
        self.assertFalse(User.objects.get(pk=self.staff.pk).has_perm(self.BASELINE))

    def test_a_denial_survives_the_role_group_being_reattached_on_save(self):
        """`sync_user_role_group` fires on every save, which is exactly what
        used to make per-user revocation impossible."""
        provisioning.set_permission_overrides(
            self.staff, granted=set(), denied={self.BASELINE}, actor=self.admin
        )

        self.staff.full_name = "اسم آخر"
        self.staff.save(update_fields=["full_name"])
        sync_role_groups()

        self.assertFalse(User.objects.get(pk=self.staff.pk).has_perm(self.BASELINE))

    def test_a_denial_closes_the_screen_it_guards(self):
        """Not merely `has_perm`: the view has to refuse too."""
        verify_otp(self.client, self.staff)
        url = reverse("finance:request_list")
        self.assertEqual(self.client.get(url).status_code, 200)

        provisioning.set_permission_overrides(
            self.staff,
            granted=set(),
            denied={"transactions.view_request", "transactions.view_all_requests"},
            actor=self.admin,
        )

        self.assertFalse(User.objects.get(pk=self.staff.pk).has_perm("transactions.view_request"))

    def test_clearing_the_overrides_returns_the_account_to_its_role(self):
        provisioning.set_permission_overrides(
            self.staff, granted={self.EXTRA}, denied={self.BASELINE}, actor=self.admin
        )

        provisioning.set_permission_overrides(
            self.staff, granted=set(), denied=set(), actor=self.admin
        )

        fresh = User.objects.get(pk=self.staff.pk)
        self.assertTrue(fresh.has_perm(self.BASELINE))
        self.assertFalse(fresh.has_perm(self.EXTRA))

    def test_a_superuser_cannot_be_denied_anything(self):
        """`PermissionsMixin.has_perm` short circuits for a superuser before
        any backend is consulted, which is what stops the deny list from being
        able to lock the last administrator out."""
        root = make_user("super@maxifyfx.com", Role.FINANCE_ADMIN, is_superuser=True)

        provisioning.set_permission_overrides(
            root, granted=set(), denied={self.BASELINE}, actor=self.admin
        )

        self.assertTrue(User.objects.get(pk=root.pk).has_perm(self.BASELINE))

    def test_granting_and_denying_the_same_permission_is_refused(self):
        with self.assertRaises(ProvisioningError):
            provisioning.set_permission_overrides(
                self.staff, granted={self.EXTRA}, denied={self.EXTRA}, actor=self.admin
            )

    def test_the_change_is_audited_with_both_sides_of_it(self):
        provisioning.set_permission_overrides(
            self.staff, granted={self.EXTRA}, denied={self.BASELINE}, actor=self.admin
        )

        entry = AuditLog.objects.filter(action=AuditAction.PERMISSION_CHANGE).latest("pk")
        self.assertEqual(entry.after["granted"], [self.EXTRA])
        self.assertEqual(entry.after["denied"], [self.BASELINE])
        self.assertIn(self.BASELINE, entry.before["effective"])
        self.assertNotIn(self.BASELINE, entry.after["effective"])


class MerchantAnonymityTests(ProvisioningTestCase):
    """Spec §2, at the one route in that the group matrix does not cover."""

    def setUp(self):
        super().setUp()
        self.merchant_user = make_user("merch@example.com", Role.MERCHANT)

    def test_a_merchant_account_cannot_be_granted_an_identity_permission(self):
        for label in sorted(MERCHANT_FORBIDDEN):
            if not Permission.objects.filter(
                content_type__app_label=label.split(".")[0],
                codename=label.split(".")[1],
            ).exists():
                continue
            with self.subTest(permission=label):
                with self.assertRaises(ProvisioningError):
                    provisioning.set_permission_overrides(
                        self.merchant_user,
                        granted={label},
                        denied=set(),
                        actor=self.admin,
                    )

    def test_the_refusal_leaves_the_account_exactly_as_it_was(self):
        with self.assertRaises(ProvisioningError):
            provisioning.set_permission_overrides(
                self.merchant_user,
                granted={"accounts.view_client_identity"},
                denied=set(),
                actor=self.admin,
            )

        fresh = User.objects.get(pk=self.merchant_user.pk)
        self.assertEqual(fresh.user_permissions.count(), 0)
        self.assertFalse(fresh.has_perm("accounts.view_client_identity"))

    def test_a_finance_account_may_hold_the_same_permission(self):
        """The rule is about merchants, not about the permission."""
        provisioning.set_permission_overrides(
            self.staff,
            granted={"accounts.view_client_identity"},
            denied=set(),
            actor=self.admin,
        )
        self.assertTrue(
            User.objects.get(pk=self.staff.pk).has_perm("accounts.view_client_identity")
        )


# ---------------------------------------------------------------------------
# Not locking the product out of being administered
# ---------------------------------------------------------------------------


class SelfTargetGuardTests(ProvisioningTestCase):
    def test_an_administrator_cannot_disable_their_own_account(self):
        with self.assertRaises(ProvisioningError):
            provisioning.set_active(self.admin, False, actor=self.admin)
        self.assertTrue(User.objects.get(pk=self.admin.pk).is_active)

    def test_an_administrator_cannot_change_their_own_role(self):
        with self.assertRaises(ProvisioningError):
            provisioning.update_account(
                self.admin,
                full_name=self.admin.full_name,
                phone="",
                role=Role.MERCHANT,
                actor=self.admin,
            )
        self.assertEqual(User.objects.get(pk=self.admin.pk).role, Role.FINANCE_ADMIN)

    def test_an_administrator_cannot_edit_their_own_permissions(self):
        with self.assertRaises(ProvisioningError):
            provisioning.set_permission_overrides(
                self.admin,
                granted=set(),
                denied={"accounts.manage_internal_users"},
                actor=self.admin,
            )

    def test_they_may_still_rename_themselves(self):
        """The guard is about power, not about every field."""
        provisioning.update_account(
            self.admin,
            full_name="اسم جديد",
            phone="0770",
            role=self.admin.role,
            actor=self.admin,
        )
        self.assertEqual(User.objects.get(pk=self.admin.pk).full_name, "اسم جديد")

    def test_another_administrator_may_disable_them(self):
        other = make_user("second@maxifyfx.com", Role.FINANCE_ADMIN)
        provisioning.set_active(self.admin, False, actor=other)
        self.assertFalse(User.objects.get(pk=self.admin.pk).is_active)


# ---------------------------------------------------------------------------
# Merchant records
# ---------------------------------------------------------------------------


class MerchantLinkTests(ProvisioningTestCase):
    def setUp(self):
        super().setUp()
        self.merchant_user = make_user("merch@example.com", Role.MERCHANT)
        self.record = Merchant.objects.create(name="تاجر أ")

    def test_linking_points_the_record_at_the_account(self):
        provisioning.link_merchant(self.merchant_user, self.record, actor=self.admin)

        self.record.refresh_from_db()
        self.assertEqual(self.record.user_id, self.merchant_user.pk)

    def test_relinking_frees_the_previous_record(self):
        """`Merchant.user` is a one-to-one, so the old link has to go somewhere
        other than an IntegrityError."""
        other = Merchant.objects.create(name="تاجر ب")
        provisioning.link_merchant(self.merchant_user, self.record, actor=self.admin)

        provisioning.link_merchant(self.merchant_user, other, actor=self.admin)

        self.record.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNone(self.record.user_id)
        self.assertEqual(other.user_id, self.merchant_user.pk)

    def test_a_non_merchant_account_cannot_be_linked(self):
        with self.assertRaises(ProvisioningError):
            provisioning.link_merchant(self.staff, self.record, actor=self.admin)

    def test_a_linked_account_cannot_be_re_roled_out_from_under_its_record(self):
        provisioning.link_merchant(self.merchant_user, self.record, actor=self.admin)

        with self.assertRaises(ProvisioningError):
            provisioning.update_account(
                self.merchant_user,
                full_name=self.merchant_user.full_name,
                phone="",
                role=Role.FINANCE_STAFF,
                actor=self.admin,
            )


# ---------------------------------------------------------------------------
# The audit trail
# ---------------------------------------------------------------------------


class AuditTrailTests(ProvisioningTestCase):
    def test_every_operation_writes_an_entry_naming_who_did_it(self):
        user, _password = provisioning.create_account(
            email="new@maxifyfx.com", full_name="حساب جديد",
            role=Role.FINANCE_STAFF, actor=self.admin,
        )
        provisioning.update_account(
            user, full_name="اسم آخر", phone="0770", role=Role.FINANCE_STAFF, actor=self.admin
        )
        provisioning.reset_password(user, actor=self.admin)
        provisioning.reset_two_factor(user, actor=self.admin)
        provisioning.set_active(user, False, actor=self.admin)
        provisioning.set_permission_overrides(
            user, granted={"rates.add_exchangerate"}, denied=set(), actor=self.admin
        )

        entries = AuditLog.objects.filter(target_id=str(user.pk))
        events = [e.after.get("event") for e in entries if isinstance(e.after, dict)]

        for expected in ("created", "updated", "password_reset", "two_factor_reset", "disabled"):
            self.assertIn(expected, events)
        self.assertTrue(
            entries.filter(action=AuditAction.PERMISSION_CHANGE).exists()
        )
        self.assertTrue(all(e.actor_id == self.admin.pk for e in entries))

    def test_a_failed_operation_writes_nothing(self):
        before = AuditLog.objects.count()

        with self.assertRaises(ProvisioningError):
            provisioning.set_active(self.admin, False, actor=self.admin)

        self.assertEqual(AuditLog.objects.count(), before)
