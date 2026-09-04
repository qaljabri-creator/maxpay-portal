"""The masking itself (spec §2, §11) — build-order step 8.

Spec §2 puts the guarantee at the serializer level, so this is where it is
tested at the serializer level: not "the page does not show a name", but "the
serializer cannot be written to produce one, and does not produce one".

:mod:`.test_api` and :mod:`.test_views` then sweep the surfaces those
serializers feed, which is how a serializer-level guarantee is shown to be an
actual response-level one.
"""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import serializers

from apps.core.choices import ActorRole
from apps.merchant_panel.anonymity import AnonymityError, assert_anonymous, is_forbidden_key
from apps.merchant_panel.serializers import (
    MerchantAttachmentSerializer,
    MerchantMessageSerializer,
    MerchantRequestDetailSerializer,
    MerchantRequestSummarySerializer,
    MerchantSafeSerializer,
    MerchantWalletSerializer,
    visible_messages,
)
from apps.transactions.models import Attachment, Message, RequestStatus, RequestType

from .support import IDENTITY_MARKERS, PNG, MerchantPanelTestCase


class WhitelistTests(MerchantPanelTestCase):
    """The whitelist, written out. Spec §11 says merchant serializers list what
    they show; these tests are that list, so growing one is a decision somebody
    has to make on purpose rather than a diff nobody notices."""

    def test_summary_serializer_shows_exactly_this(self):
        self.assertEqual(
            set(MerchantRequestSummarySerializer().fields),
            {
                "reference",
                "type",
                "type_label",
                "status",
                "status_label",
                "is_closed",
                "needs_action",
                "amount_usd",
                "amount_iqd",
                "method",
                "wallet_number",
                "destination_account",
                "submitted_at",
                "assigned_at",
            },
        )

    def test_detail_serializer_shows_exactly_this(self):
        self.assertEqual(
            set(MerchantRequestDetailSerializer().fields)
            - set(MerchantRequestSummarySerializer().fields),
            {
                "merchant_actioned_at",
                "closed_at",
                "timeline",
                "attachments",
                "messages",
                "actions",
            },
        )

    def test_message_and_attachment_serializers_show_exactly_this(self):
        self.assertEqual(
            set(MerchantMessageSerializer().fields),
            {"id", "sender_role", "sender", "body", "created_at", "attachment"},
        )
        self.assertEqual(
            set(MerchantAttachmentSerializer().fields),
            {
                "id",
                "name",
                "content_type",
                "is_image",
                "size_bytes",
                "uploaded_by",
                "uploaded_at",
                "url",
            },
        )

    def test_wallet_serializer_shows_exactly_this(self):
        self.assertEqual(
            set(MerchantWalletSerializer().fields),
            {"id", "number", "label", "method", "is_active", "daily_cap", "deactivated_at"},
        )

    def test_no_merchant_serializer_names_an_identifying_field(self):
        for serializer_class in (
            MerchantRequestSummarySerializer,
            MerchantRequestDetailSerializer,
            MerchantMessageSerializer,
            MerchantAttachmentSerializer,
            MerchantWalletSerializer,
        ):
            for name in serializer_class().fields:
                self.assertFalse(
                    is_forbidden_key(name),
                    f"{serializer_class.__name__} declares {name!r} (spec §2).",
                )


class StaticGuardTests(MerchantPanelTestCase):
    """A merchant serializer that would leak must not survive being defined.

    Import time, not request time: the failure lands on whoever wrote the field,
    in the test run that introduced it, rather than on a merchant reading a
    client's name in production.
    """

    def test_declaring_an_identifying_field_is_refused(self):
        with self.assertRaises(AnonymityError):

            class Leaky(MerchantSafeSerializer):
                email = serializers.CharField()

    def test_a_field_named_innocently_but_sourced_from_identity_is_refused(self):
        with self.assertRaises(AnonymityError):

            class Sneaky(MerchantSafeSerializer):
                # Passes every check that looks only at field names.
                reference = serializers.CharField(source="client.account_number")

    def test_a_nested_identity_serializer_is_refused(self):
        with self.assertRaises(AnonymityError):

            class Nested(MerchantSafeSerializer):
                client = serializers.DictField()

    def test_meta_exclude_is_refused(self):
        with self.assertRaises(TypeError):

            class Excluding(MerchantSafeSerializer):
                class Meta:
                    exclude = ["client"]

    def test_meta_fields_all_is_refused(self):
        with self.assertRaises(TypeError):

            class Everything(MerchantSafeSerializer):
                class Meta:
                    fields = "__all__"

    def test_a_harmless_serializer_is_allowed(self):
        class Fine(MerchantSafeSerializer):
            reference = serializers.CharField(source="public_ref")
            destination_account = serializers.CharField()

        self.assertEqual(
            set(Fine().fields), {"reference", "destination_account"}
        )


class RuntimeGuardTests(MerchantPanelTestCase):
    """What no static check can see: whatever a method field decides to build."""

    def test_a_method_field_that_returns_identity_raises_rather_than_renders(self):
        class Sneaky(MerchantSafeSerializer):
            detail = serializers.SerializerMethodField()

            def get_detail(self, obj):
                return {"who": {"email": IDENTITY_MARKERS["email"]}}

        with self.assertRaises(AnonymityError):
            _rendered = Sneaky(self.request_obj).data

    def test_assert_anonymous_walks_into_lists(self):
        with self.assertRaises(AnonymityError):
            assert_anonymous({"messages": [{"body": "hi"}, {"sender_id": 4}]})

    def test_assert_anonymous_accepts_a_clean_payload(self):
        assert_anonymous(
            {"reference": "MP-12345", "messages": [{"sender": "العميل", "body": "hi"}]}
        )


class DepositPayloadTests(MerchantPanelTestCase):
    """What a merchant is actually handed for a deposit (spec §2, §8)."""

    def setUp(self):
        super().setUp()
        self.payload = MerchantRequestDetailSerializer(
            self.request_obj, context={"user": self.user}
        ).data

    def test_it_carries_no_client_identity(self):
        self.assertNoIdentity(self.payload, "the deposit detail payload")

    def test_it_carries_what_spec_2_says_a_merchant_sees(self):
        self.assertEqual(self.payload["reference"], self.request_obj.public_ref)
        self.assertEqual(self.payload["type"], RequestType.DEPOSIT)
        self.assertEqual(self.payload["amount_usd"], "100.00")
        self.assertEqual(self.payload["amount_iqd"], "145500.00")
        self.assertEqual(self.payload["method"], "زين كاش")
        self.assertEqual(self.payload["wallet_number"], "07700000001")

    def test_it_does_not_disclose_which_merchant_the_client_chose(self):
        # Finance may route elsewhere (spec §5); that decision is Finance's.
        self.assertNotIn("merchant_selected", self.payload)
        self.assertNotIn("merchant_assigned", self.payload)

    def test_a_deposit_carries_no_destination_account(self):
        self.assertEqual(self.payload["destination_account"], "")


class WithdrawalPayloadTests(MerchantPanelTestCase):
    """Spec §2's single exception: the destination, never its owner."""

    def test_the_destination_is_shown_and_nothing_about_who_owns_it(self):
        withdrawal = self.make_request(
            type=RequestType.WITHDRAWAL,
            destination_account="6274-1111-2222-3333",
            wallet_number_snapshot="",
        )
        payload = MerchantRequestDetailSerializer(
            withdrawal, context={"user": self.user}
        ).data
        self.assertEqual(payload["destination_account"], "6274-1111-2222-3333")
        self.assertNoIdentity(payload, "the withdrawal detail payload")


class ThreadTests(MerchantPanelTestCase):
    """Spec §5: the merchant reads the thread, labelled by role only."""

    def test_the_client_is_labelled_and_never_named(self):
        self.add_thread()
        payload = MerchantRequestDetailSerializer(
            self.request_obj, context={"user": self.user}
        ).data
        senders = [m["sender"] for m in payload["messages"]]
        self.assertIn("العميل", senders)
        self.assertNoIdentity(payload, "the thread payload")

    def test_internal_notes_never_reach_a_merchant(self):
        internal = self.add_thread()
        payload = MerchantRequestDetailSerializer(
            self.request_obj, context={"user": self.user}
        ).data
        bodies = [m["body"] for m in payload["messages"]]
        self.assertNotIn(internal.body, bodies)
        # And the note's very existence is not advertised.
        self.assertEqual(len(payload["messages"]), 2)

    def test_both_finance_roles_appear_as_one_label(self):
        Message.objects.create(
            request=self.request_obj,
            sender_role=ActorRole.FINANCE_ADMIN,
            sender_id=self.finance.pk,
            body="تم.",
        )
        payload = MerchantRequestDetailSerializer(
            self.request_obj, context={"user": self.user}
        ).data
        self.assertEqual([m["sender"] for m in payload["messages"]], ["المالية"])

    def test_a_previous_merchants_messages_are_not_handed_to_their_replacement(self):
        """A re-routed request (spec §5) carries the first merchant's words.

        They are not the second merchant's business, so they do not travel with
        the request when Finance moves it.
        """
        Message.objects.create(
            request=self.request_obj,
            sender_role=ActorRole.MERCHANT,
            sender_id=self.other_user.pk,
            body="لم يصلني شيء حتى الآن.",
        )
        Message.objects.create(
            request=self.request_obj,
            sender_role=ActorRole.MERCHANT,
            sender_id=self.user.pk,
            body="سأتحقق.",
        )
        visible = visible_messages(self.request_obj, self.user)
        self.assertEqual([m.body for m in visible], ["سأتحقق."])


class AttachmentPayloadTests(MerchantPanelTestCase):
    """A proof file names its uploader's role and never their id (spec §11)."""

    def test_the_uploader_is_a_role_not_a_person(self):
        attachment = Attachment.objects.create(
            request=self.request_obj,
            file=SimpleUploadedFile("proof.png", PNG, content_type="image/png"),
            content_type="image/png",
            uploaded_by_role=ActorRole.CLIENT,
            uploaded_by_id=self.client_record.pk,
        )
        payload = MerchantAttachmentSerializer(
            attachment, context={"user": self.user}
        ).data
        self.assertEqual(payload["uploaded_by"], "العميل")
        self.assertNotIn(str(self.client_record.pk), str(payload.get("uploaded_by")))
        self.assertNoIdentity(payload, "the attachment payload")

    def test_the_url_is_signed_rather_than_a_stored_path(self):
        attachment = Attachment.objects.create(
            request=self.request_obj,
            file=SimpleUploadedFile("proof.png", PNG, content_type="image/png"),
            content_type="image/png",
            uploaded_by_role=ActorRole.CLIENT,
        )
        url = MerchantAttachmentSerializer(
            attachment, context={"user": self.user}
        ).data["url"]
        self.assertTrue(url.startswith("/merchant/attachments/"))
        self.assertNotIn(attachment.file.name, url)


class ActionTests(MerchantPanelTestCase):
    """The moves offered are the ones the shared state machine would allow."""

    def test_an_assigned_deposit_offers_confirm_hand_back_and_reject(self):
        payload = MerchantRequestDetailSerializer(
            self.request_obj, context={"user": self.user}
        ).data
        self.assertEqual(
            {a["action"] for a in payload["actions"]},
            {"confirm", "hand_back", "reject"},
        )

    def test_a_closed_request_offers_nothing(self):
        self.request_obj.status = RequestStatus.CLOSED
        self.request_obj.save(update_fields=["status"])
        payload = MerchantRequestDetailSerializer(
            self.request_obj, context={"user": self.user}
        ).data
        self.assertEqual(payload["actions"], [])

    def test_a_merchant_is_offered_nothing_on_somebody_elses_request(self):
        theirs = self.make_request(assigned_to=self.other)
        payload = MerchantRequestDetailSerializer(
            theirs, context={"user": self.user}
        ).data
        self.assertEqual(payload["actions"], [])


class WalletPayloadTests(MerchantPanelTestCase):
    """Spec §8: read-only view of own wallets and their active status."""

    def test_a_wallet_shows_its_own_numbers_and_no_finance_metadata(self):
        wallet = self.merchant_method.wallets.first()
        wallet.daily_cap = Decimal("5000000.00")
        wallet.created_by = self.finance
        wallet.save()
        payload = MerchantWalletSerializer(wallet, context={"user": self.user}).data
        self.assertEqual(payload["number"], "07700000001")
        self.assertTrue(payload["is_active"])
        self.assertNotIn("created_by", payload)
        self.assertNoIdentity(payload, "the wallet payload")
