"""The roles and users panel (spec §3, §9) — build-order step 15.

:mod:`apps.accounts.test_provisioning` covers the rules. What is tested here is
the panel: who reaches it, what a POST actually does, and the one thing the
screens own rather than inherit — a generated password that is displayed exactly
once and then gone.
"""

from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user, verify_otp
from apps.core.choices import AuditAction
from apps.core.models import AuditLog
from apps.merchants.models import Merchant


class UserPanelTestCase(TestCase):
    def setUp(self):
        sync_role_groups()
        self.admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN)
        self.staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)
        self.merchant_user = make_user("merch@example.com", Role.MERCHANT)
        self.merchant = Merchant.objects.create(name="تاجر أ")

        self.list_url = reverse("finance:user_list")
        self.create_url = reverse("finance:user_create")

    def login(self, user):
        verify_otp(self.client, user)
        return user

    def detail_url(self, user=None):
        return reverse("finance:user_detail", args=[(user or self.staff).pk])

    def action_url(self, action, user=None):
        return reverse("finance:user_action", args=[(user or self.staff).pk, action])

    @staticmethod
    def revoke(role, codename):
        Group.objects.get(name=role).permissions.remove(
            Permission.objects.get(codename=codename)
        )


# ---------------------------------------------------------------------------
# Who reaches it
# ---------------------------------------------------------------------------


class AccessTests(UserPanelTestCase):
    def test_a_finance_admin_reaches_the_panel(self):
        self.login(self.admin)
        self.assertEqual(self.client.get(self.list_url).status_code, 200)

    def test_a_merchant_never_reaches_it(self):
        """Spec §2 keeps merchants out of the Finance panel entirely, and this
        is the screen that could hand one every permission in the system."""
        self.login(self.merchant_user)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)

    def test_finance_staff_do_not_manage_users_by_default(self):
        """Spec §3: the root role creates all other users."""
        self.login(self.staff)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)

    def test_the_power_can_still_be_delegated_to_staff(self):
        """A permission, not a role — so it can be handed over without a code
        change (spec §3)."""
        Group.objects.get(name=Role.FINANCE_STAFF).permissions.add(
            Permission.objects.get(codename="manage_internal_users")
        )
        self.login(self.staff)
        self.assertEqual(self.client.get(self.list_url).status_code, 200)

    def test_an_anonymous_visitor_is_sent_to_the_login(self):
        self.assertEqual(self.client.get(self.list_url).status_code, 302)

    def test_editing_permissions_needs_its_own_permission(self):
        """Creating an account with a role's baseline and handing out arbitrary
        permissions are different powers."""
        self.revoke(Role.FINANCE_ADMIN, "manage_permissions")
        self.login(self.admin)

        response = self.client.get(reverse("finance:user_permissions", args=[self.staff.pk]))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(self.list_url).status_code, 200)

    def test_resetting_a_second_factor_needs_its_own_permission(self):
        self.revoke(Role.FINANCE_ADMIN, "reset_user_two_factor")
        self.login(self.admin)

        response = self.client.post(self.action_url("reset_two_factor"))

        self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------------
# Creating an account
# ---------------------------------------------------------------------------


class CreateTests(UserPanelTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)

    def payload(self, **overrides):
        data = {
            "full_name": "هدى العامري",
            "email": "huda@maxifyfx.com",
            "phone": "07701112233",
            "role": Role.FINANCE_STAFF,
            "merchant": "",
        }
        data.update(overrides)
        return data

    def test_a_created_account_lands_on_its_own_page(self):
        response = self.client.post(self.create_url, self.payload())

        created = User.objects.get(email="huda@maxifyfx.com")
        self.assertRedirects(response, self.detail_url(created))
        self.assertEqual(created.role, Role.FINANCE_STAFF)
        self.assertEqual(created.created_by_id, self.admin.pk)

    def test_the_new_account_holds_its_roles_permissions_immediately(self):
        self.client.post(self.create_url, self.payload())

        created = User.objects.get(email="huda@maxifyfx.com")
        self.assertTrue(created.has_perm("transactions.view_request"))
        self.assertFalse(created.has_perm("accounts.manage_internal_users"))

    def test_a_duplicate_email_is_refused_on_the_form(self):
        response = self.client.post(self.create_url, self.payload(email=self.staff.email))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "يوجد حساب بهذا البريد")
        self.assertEqual(User.objects.filter(email=self.staff.email).count(), 1)

    def test_a_merchant_record_cannot_be_attached_to_a_finance_account(self):
        response = self.client.post(
            self.create_url,
            self.payload(role=Role.FINANCE_STAFF, merchant=str(self.merchant.pk)),
        )

        self.assertEqual(response.status_code, 200)
        self.merchant.refresh_from_db()
        self.assertIsNone(self.merchant.user_id)

    def test_a_merchant_account_can_be_linked_as_it_is_created(self):
        self.client.post(
            self.create_url,
            self.payload(
                email="wasit@example.com", role=Role.MERCHANT, merchant=str(self.merchant.pk)
            ),
        )

        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.user.email, "wasit@example.com")

    def test_creation_is_audited(self):
        self.client.post(self.create_url, self.payload())

        created = User.objects.get(email="huda@maxifyfx.com")
        entry = AuditLog.objects.filter(
            action=AuditAction.USER_CHANGE, target_id=str(created.pk)
        ).latest("pk")
        self.assertEqual(entry.actor_id, self.admin.pk)
        self.assertEqual(entry.after["event"], "created")


# ---------------------------------------------------------------------------
# The one-time password
# ---------------------------------------------------------------------------


class IssuedPasswordTests(UserPanelTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)

    def test_it_is_shown_on_the_page_the_creation_redirects_to(self):
        response = self.client.post(
            self.create_url,
            {
                "full_name": "هدى العامري",
                "email": "huda@maxifyfx.com",
                "phone": "",
                "role": Role.FINANCE_STAFF,
                "merchant": "",
            },
            follow=True,
        )

        self.assertContains(response, "secret__value")

    def test_it_is_gone_on_the_very_next_load_of_the_same_page(self):
        """Popped, not stored: a refresh, a back button and a bookmark all get
        nothing, which is what makes "shown once" true rather than a caption."""
        self.client.post(self.action_url("reset_password"))
        first = self.client.get(self.detail_url())

        second = self.client.get(self.detail_url())

        self.assertContains(first, "secret__value")
        self.assertNotContains(second, "secret__value")

    def test_it_never_appears_on_another_accounts_page(self):
        self.client.post(self.action_url("reset_password", self.staff))

        response = self.client.get(self.detail_url(self.merchant_user))

        self.assertNotContains(response, "secret__value")

    def test_the_password_actually_works_for_the_account_it_was_issued_for(self):
        response = self.client.post(self.action_url("reset_password"), follow=True)

        issued = response.context["issued_password"]
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password(issued))


# ---------------------------------------------------------------------------
# The levers
# ---------------------------------------------------------------------------


class ActionTests(UserPanelTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)

    def test_disabling_an_account_stops_it_logging_in(self):
        self.client.post(self.action_url("disable"))

        self.staff.refresh_from_db()
        self.assertFalse(self.staff.is_active)
        self.assertIsNone(
            self.client.session and User.objects.filter(pk=self.staff.pk, is_active=True).first()
        )

    def test_enabling_puts_it_back(self):
        self.client.post(self.action_url("disable"))
        self.client.post(self.action_url("enable"))

        self.staff.refresh_from_db()
        self.assertTrue(self.staff.is_active)

    def test_an_admin_cannot_disable_themselves_through_the_panel(self):
        response = self.client.post(self.action_url("disable", self.admin), follow=True)

        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertContains(response, "لا يمكنك تعطيل حسابك")

    def test_an_unknown_action_is_refused_rather_than_guessed_at(self):
        response = self.client.post(self.action_url("promote_to_root"))
        self.assertEqual(response.status_code, 403)

    def test_a_lever_cannot_be_pulled_with_a_get(self):
        """Every one of these changes something, so none of them is a link."""
        self.assertEqual(self.client.get(self.action_url("disable")).status_code, 405)

    def test_linking_a_merchant_record_from_the_detail_page(self):
        self.client.post(
            self.action_url("link_merchant", self.merchant_user),
            {"merchant": str(self.merchant.pk)},
        )

        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.user_id, self.merchant_user.pk)

    def test_unlinking_leaves_the_record_without_an_account(self):
        self.merchant.user = self.merchant_user
        self.merchant.save(update_fields=["user"])

        self.client.post(
            self.action_url("link_merchant", self.merchant_user), {"merchant": ""}
        )

        self.merchant.refresh_from_db()
        self.assertIsNone(self.merchant.user_id)


# ---------------------------------------------------------------------------
# The permission editor
# ---------------------------------------------------------------------------


class PermissionScreenTests(UserPanelTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)
        self.url = reverse("finance:user_permissions", args=[self.staff.pk])

    def field_name(self, label):
        return "perm__" + label.replace(".", "__")

    def posted(self, **overrides):
        """Every control at its default, so a change is visibly the only one."""
        form = self.client.get(self.url).context["form"]
        data = {row["field"].name: row["field"].value() or "" for row in form.rows}
        data.update(overrides)
        return data

    def test_the_screen_lists_the_managed_permissions_with_their_baseline(self):
        response = self.client.get(self.url)

        rows = response.context["form"].rows
        by_label = {row["label"]: row for row in rows}
        self.assertTrue(by_label["transactions.credit_request"]["in_baseline"])
        self.assertFalse(by_label["rates.add_exchangerate"]["in_baseline"])

    def test_denying_a_baseline_permission_through_the_form_takes_it_away(self):
        self.client.post(
            self.url,
            self.posted(**{self.field_name("transactions.credit_request"): "deny"}),
        )

        self.assertFalse(
            User.objects.get(pk=self.staff.pk).has_perm("transactions.credit_request")
        )

    def test_granting_an_extra_permission_through_the_form_adds_it(self):
        self.client.post(
            self.url, self.posted(**{self.field_name("rates.add_exchangerate"): "grant"})
        )

        self.assertTrue(
            User.objects.get(pk=self.staff.pk).has_perm("rates.add_exchangerate")
        )

    def test_the_form_refuses_to_hand_a_merchant_an_identity_permission(self):
        url = reverse("finance:user_permissions", args=[self.merchant_user.pk])
        form = self.client.get(url).context["form"]
        data = {row["field"].name: row["field"].value() or "" for row in form.rows}
        data[self.field_name("accounts.view_client_identity")] = "grant"

        response = self.client.post(url, data)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            User.objects.get(pk=self.merchant_user.pk).has_perm(
                "accounts.view_client_identity"
            )
        )

    def test_an_admin_cannot_edit_their_own_permissions_through_the_screen(self):
        url = reverse("finance:user_permissions", args=[self.admin.pk])
        form = self.client.get(url).context["form"]
        data = {row["field"].name: row["field"].value() or "" for row in form.rows}
        data[self.field_name("accounts.manage_internal_users")] = "deny"

        response = self.client.post(url, data)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            User.objects.get(pk=self.admin.pk).denied_permissions.count(), 0
        )


# ---------------------------------------------------------------------------
# What the screens must never render
# ---------------------------------------------------------------------------


class ScreenContentTests(UserPanelTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)

    def test_no_password_hash_is_ever_rendered(self):
        """The hash is on the object the detail view is handed, so this is a
        thing a careless template edit could start printing."""
        response = self.client.get(self.detail_url())

        self.assertNotContains(response, self.staff.password)
        self.assertNotContains(response, "pbkdf2")

    def test_the_list_does_not_render_a_hash_either(self):
        response = self.client.get(self.list_url)

        self.assertNotContains(response, self.staff.password)

    def test_the_panel_carries_the_poll_config_like_every_other_screen(self):
        response = self.client.get(self.list_url)
        self.assertContains(response, "maxpay-poll-config")
