"""The unread rule (spec §10) — build-order step 13.

What is being tested is not "a row was written" but the property both panels
actually promise: *this request has moved since you last looked at it, and that
one has not.* Grouped by the two audiences, because the rule genuinely differs
between them and a test that only exercised one would pass while the other was
backwards.
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Client, Role
from apps.accounts.tests import make_user
from apps.core.choices import ActorRole
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod
from apps.transactions import reads
from apps.transactions.models import (
    Message,
    Request,
    RequestRead,
    RequestStatus,
    RequestType,
)


class ReadsTestCase(TestCase):
    """One merchant, one Finance user, one assigned deposit."""

    def setUp(self):
        self.staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)
        self.merchant_user = make_user("merch@example.com", Role.MERCHANT)
        self.merchant = Merchant.objects.create(
            name="تاجر بغداد", user=self.merchant_user
        )
        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        MerchantMethod.objects.create(merchant=self.merchant, payment_method=self.method)
        self.client_record = Client.objects.create(
            b2core_id="b2c-1", display_name="زينب", email="z@example.com"
        )
        self.request_obj = self.make_request()

    def make_request(self, **overrides):
        values = {
            "type": RequestType.DEPOSIT,
            "client": self.client_record,
            "payment_method": self.method,
            "merchant_selected": self.merchant,
            "merchant_assigned": self.merchant,
            "status": RequestStatus.ASSIGNED,
            "amount_usd": Decimal("100.00"),
            "amount_iqd": Decimal("147000.00"),
            "rate_applied": Decimal("1470.00"),
            "commission_applied": Decimal("5000.00"),
            "assigned_at": timezone.now(),
        }
        values.update(overrides)
        return Request.objects.create(**values)

    def post(self, role, body="مرحبًا", **extra):
        return Message.objects.create(
            request=self.request_obj, sender_role=role, body=body, **extra
        )

    def unread(self, user, audience):
        return reads.unread_count(
            user=user, audience=audience, queryset=Request.objects.all()
        )


class MerchantUnreadTests(ReadsTestCase):
    """Spec §10: a new assigned request appears with an unread badge."""

    def test_an_assigned_request_starts_unread(self):
        # Before a word has been written. The arrival is the event.
        self.assertEqual(self.unread(self.merchant_user, reads.MERCHANT), 1)

    def test_opening_it_clears_the_badge(self):
        reads.mark_seen(user=self.merchant_user, request_obj=self.request_obj)

        self.assertEqual(self.unread(self.merchant_user, reads.MERCHANT), 0)

    def test_a_message_from_the_client_makes_it_unread_again(self):
        reads.mark_seen(user=self.merchant_user, request_obj=self.request_obj)
        self.post(ActorRole.CLIENT)

        self.assertEqual(self.unread(self.merchant_user, reads.MERCHANT), 1)

    def test_the_merchants_own_reply_does_not_wake_them(self):
        reads.mark_seen(user=self.merchant_user, request_obj=self.request_obj)
        self.post(ActorRole.MERCHANT)

        self.assertEqual(self.unread(self.merchant_user, reads.MERCHANT), 0)

    def test_an_internal_note_is_invisible_and_therefore_silent(self):
        # A merchant is not told internal notes exist (spec §5); a badge that
        # rose for one would tell them.
        reads.mark_seen(user=self.merchant_user, request_obj=self.request_obj)
        self.post(ActorRole.FINANCE_STAFF, is_internal_note=True)

        self.assertEqual(self.unread(self.merchant_user, reads.MERCHANT), 0)

    def test_a_message_from_finance_does_wake_them(self):
        reads.mark_seen(user=self.merchant_user, request_obj=self.request_obj)
        self.post(ActorRole.FINANCE_STAFF)

        self.assertEqual(self.unread(self.merchant_user, reads.MERCHANT), 1)

    def test_one_persons_marker_is_not_anothers(self):
        other = make_user("other@example.com", Role.MERCHANT)
        reads.mark_seen(user=self.merchant_user, request_obj=self.request_obj)

        self.assertEqual(self.unread(self.merchant_user, reads.MERCHANT), 0)
        self.assertEqual(self.unread(other, reads.MERCHANT), 1)


class FinanceUnreadTests(ReadsTestCase):
    """Spec §10: a new submission and a merchant confirmation raise a badge."""

    def test_a_fresh_submission_is_unread(self):
        self.assertEqual(self.unread(self.staff, reads.FINANCE), 1)

    def test_opening_it_clears_the_badge(self):
        reads.mark_seen(user=self.staff, request_obj=self.request_obj)

        self.assertEqual(self.unread(self.staff, reads.FINANCE), 0)

    def test_a_merchant_confirmation_raises_it_again(self):
        reads.mark_seen(user=self.staff, request_obj=self.request_obj)

        self.request_obj.status = RequestStatus.MERCHANT_CONFIRMED
        self.request_obj.merchant_actioned_at = timezone.now() + timedelta(seconds=1)
        self.request_obj.save()

        self.assertEqual(self.unread(self.staff, reads.FINANCE), 1)

    def test_a_message_from_the_merchant_raises_it(self):
        reads.mark_seen(user=self.staff, request_obj=self.request_obj)
        self.post(ActorRole.MERCHANT)

        self.assertEqual(self.unread(self.staff, reads.FINANCE), 1)

    def test_finances_own_note_does_not_raise_it(self):
        reads.mark_seen(user=self.staff, request_obj=self.request_obj)
        self.post(ActorRole.FINANCE_STAFF, is_internal_note=True)
        self.post(ActorRole.FINANCE_ADMIN)

        self.assertEqual(self.unread(self.staff, reads.FINANCE), 0)

    def test_two_staff_keep_separate_markers(self):
        colleague = make_user("colleague@maxifyfx.com", Role.FINANCE_STAFF)
        reads.mark_seen(user=self.staff, request_obj=self.request_obj)

        self.assertEqual(self.unread(self.staff, reads.FINANCE), 0)
        self.assertEqual(self.unread(colleague, reads.FINANCE), 1)


class ReferenceTests(ReadsTestCase):
    def test_references_are_returned_rather_than_ids(self):
        # A merchant never sees a database key (spec §2), and the queue
        # templates are keyed on the reference.
        refs = reads.unread_references(
            user=self.merchant_user,
            audience=reads.MERCHANT,
            requests=[self.request_obj],
        )

        self.assertEqual(refs, {self.request_obj.public_ref})

    def test_a_page_of_a_paginator_is_an_acceptable_argument(self):
        # The real callers hand over a sliced queryset, which cannot be
        # filtered again — the reason the signature takes rows, not a queryset.
        from django.core.paginator import Paginator

        page = Paginator(Request.objects.all(), 25).get_page(1)
        refs = reads.unread_references(
            user=self.merchant_user, audience=reads.MERCHANT, requests=page.object_list
        )

        self.assertEqual(refs, {self.request_obj.public_ref})

    def test_an_empty_page_costs_no_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(
                reads.unread_references(
                    user=self.merchant_user, audience=reads.MERCHANT, requests=[]
                ),
                set(),
            )


class VersionTokenTests(ReadsTestCase):
    """The poll compares one string; it has to move when the screen should."""

    def token(self, user=None, audience=reads.MERCHANT):
        return reads.latest_activity(
            user=user or self.merchant_user,
            audience=audience,
            queryset=Request.objects.all(),
        )

    def test_a_new_message_moves_it(self):
        before = self.token()
        self.post(ActorRole.CLIENT)

        self.assertNotEqual(self.token(), before)

    def test_a_new_request_moves_it_even_at_the_same_instant(self):
        before = self.token()
        # The count is in the token precisely for this: a second request
        # arriving in the same second moves no timestamp.
        self.make_request(assigned_at=self.request_obj.assigned_at)

        self.assertNotEqual(self.token(), before)

    def test_looking_at_it_does_not_move_it(self):
        # Reading is not activity; otherwise every poll would trigger a
        # re-render on the tab that just polled.
        before = self.token()
        reads.mark_seen(user=self.merchant_user, request_obj=self.request_obj)

        self.assertEqual(self.token(), before)

    def test_an_empty_queue_has_a_stable_token(self):
        Request.objects.all().delete()

        self.assertEqual(self.token(), self.token())


class MarkerTests(ReadsTestCase):
    def test_marking_twice_updates_rather_than_duplicates(self):
        reads.mark_seen(user=self.staff, request_obj=self.request_obj)
        first = RequestRead.objects.get(user=self.staff, request=self.request_obj)
        reads.mark_seen(user=self.staff, request_obj=self.request_obj)
        second = RequestRead.objects.get(user=self.staff, request=self.request_obj)

        self.assertEqual(RequestRead.objects.count(), 1)
        self.assertGreaterEqual(second.seen_at, first.seen_at)

    def test_an_anonymous_caller_writes_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        reads.mark_seen(user=AnonymousUser(), request_obj=self.request_obj)
        reads.mark_seen(user=None, request_obj=self.request_obj)

        self.assertFalse(RequestRead.objects.exists())
