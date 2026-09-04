"""A request as the merchant executing it is allowed to see it (spec §2, §8).

This module is the enforcement point spec §11 names: *all merchant-facing
serializers explicitly whitelist fields, and client identity fields must never
be serializable in a merchant context.* Not the template, not the view, not the
queryset — here. The screens in :mod:`apps.merchant_panel.views` render from the
output of these serializers rather than from model instances, so a template has
no ``.client`` to reach for even if somebody wrote one.

Spec §2 fixes the whitelist, and it is short: reference, type, amount, payment
method, wallet, attachments, and the message thread. Withdrawals add the
destination card or wallet number, because the merchant cannot pay without it —
but never the identity of who owns it. Status and its timestamps come with the
queue: a worklist without them is not a worklist.

What is deliberately absent, and why:

``client``
    The whole point (spec §2).
``merchant_selected``
    Which merchant the client originally picked. Finance may route elsewhere
    (spec §5); that decision is Finance's and does not travel.
``rejection_reason``
    Not withheld — spec §6 posts it into the thread, which the merchant reads.
    A second copy as a structured field would be one more thing to keep masked
    for no gain.
``is_internal_note``
    Notes Finance writes to itself. They are filtered out of the thread rather
    than flagged in it, so the merchant is not even told they exist.

Every class below derives from :class:`MerchantSafeSerializer`, which will not
let itself be defined if it names, or reaches through ``source=``, anything
identifying. See :mod:`apps.merchant_panel.anonymity`.
"""

from rest_framework import serializers

from apps.core.choices import ActorRole
from apps.transactions import messaging
from apps.transactions.models import RequestStatus
from apps.transactions.services import available_transitions, track

from . import attachments as attachment_urls
from .anonymity import assert_anonymous, check_field_names

#: How each sender is named to a merchant (spec §5). The client is "العميل" and
#: nothing else. The two Finance roles collapse into one label as well — which
#: desk member replied is no more the merchant's business than who the client
#: is, and the distinction would only leak how the desk is staffed.
SENDER_LABELS = {
    ActorRole.CLIENT: "العميل",
    ActorRole.FINANCE_ADMIN: "المالية",
    ActorRole.FINANCE_STAFF: "المالية",
    ActorRole.MERCHANT: "أنت",
    ActorRole.SYSTEM: "النظام",
}


class MerchantSafeSerializer(serializers.Serializer):
    """A serializer that cannot be defined, or run, into a client identity leak.

    Two guards, because they fail at different times and catch different
    mistakes:

    * :meth:`__init_subclass__` runs when the class is created, so a serializer
      declaring ``email`` — or a harmless-looking ``reference`` sourced from
      ``client.account_number`` — breaks at import. It never reaches a request.
    * :meth:`to_representation` runs on every payload, because a
      ``SerializerMethodField`` returns whatever its method builds and no static
      check can see inside that method.

    Both raise. Returning a scrubbed payload instead would leave the bug in
    place and hide it, and spec §2 is not a field that may be quietly dropped.
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        check_field_names(cls.__name__, getattr(cls, "_declared_fields", {}))

        # A ``ModelSerializer`` under this base would build its own field list
        # from the model, which is the opposite of a whitelist. If one is ever
        # wanted, it has to name its fields.
        meta = getattr(cls, "Meta", None)
        if meta is not None:
            if getattr(meta, "exclude", None):
                raise TypeError(
                    f"{cls.__name__}: a merchant serializer lists what it shows, "
                    "not what it hides. Use Meta.fields (spec §11)."
                )
            if getattr(meta, "fields", None) == "__all__":
                raise TypeError(
                    f"{cls.__name__}: Meta.fields of every field is never a "
                    "whitelist (spec §11). Name them."
                )

    def to_representation(self, instance):
        data = super().to_representation(instance)
        assert_anonymous(data, where=type(self).__name__)
        return data

    # -- context helpers ---------------------------------------------------

    @property
    def viewer(self):
        """The merchant *user* the payload is being built for.

        Needed to mint attachment signatures and to work out which lifecycle
        moves to offer. Passed explicitly by every caller; the DRF ``request``
        in context is only a fallback for a view that forgot.
        """
        user = self.context.get("user")
        if user is None:
            user = getattr(self.context.get("request"), "user", None)
        return user


def _iso(value):
    return value.isoformat() if value else None


class MerchantAttachmentSerializer(MerchantSafeSerializer):
    """A proof file, reachable only through a signed time-limited URL (spec §11).

    ``uploaded_by`` is the uploader's *role*, never their id: "العميل" tells the
    merchant which side of the request the file came from, which is all they
    need and all they get.
    """

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(source="original_name", read_only=True)
    content_type = serializers.SerializerMethodField()
    is_image = serializers.SerializerMethodField()
    size_bytes = serializers.IntegerField(read_only=True)
    uploaded_by = serializers.SerializerMethodField()
    uploaded_at = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()

    def get_content_type(self, attachment) -> str:
        return attachment_urls.content_type_of(attachment)

    def get_is_image(self, attachment) -> bool:
        return attachment_urls.content_type_of(attachment) in attachment_urls.INLINE_TYPES

    def get_uploaded_by(self, attachment) -> str:
        return SENDER_LABELS.get(
            attachment.uploaded_by_role, str(attachment.get_uploaded_by_role_display())
        )

    def get_uploaded_at(self, attachment):
        return _iso(attachment.uploaded_at)

    def get_url(self, attachment) -> str:
        return attachment_urls.url_for(attachment, self.viewer)


class MerchantMessageSerializer(MerchantSafeSerializer):
    """One entry in the thread, labelled by role rather than by person (spec §5).

    Which messages are *in* the thread is decided by :func:`visible_messages`,
    not here: what a merchant may read is a rule about the thread, and keeping
    it beside the serializer that renders it means neither can be used without
    the other.
    """

    id = serializers.IntegerField(read_only=True)
    sender_role = serializers.CharField(read_only=True)
    sender = serializers.SerializerMethodField()
    body = serializers.CharField(read_only=True)
    created_at = serializers.SerializerMethodField()
    attachment = serializers.SerializerMethodField()

    def get_sender(self, message) -> str:
        return SENDER_LABELS.get(message.sender_role, str(message.display_sender))

    def get_created_at(self, message):
        return _iso(message.created_at)

    def get_attachment(self, message):
        if not message.attachment_id or message.attachment is None:
            return None
        return MerchantAttachmentSerializer(message.attachment, context=self.context).data


def visible_messages(request_obj, viewer) -> list:
    """The thread as this merchant may read it.

    The rule itself lives in :mod:`apps.transactions.messaging`, because the
    client portal and the Finance panel ask the same question of the same thread
    and three copies of an answer is three chances to get it wrong. What is
    merchant-specific is only the viewer: "their own messages" needs to know
    whose they are.
    """
    return messaging.visible_messages(
        request_obj,
        audience=messaging.MERCHANT,
        viewer_id=getattr(viewer, "pk", None),
    )


class MerchantRequestSummarySerializer(MerchantSafeSerializer):
    """One row of the queue (spec §8): enough to pick the next job, no more."""

    reference = serializers.CharField(source="public_ref", read_only=True)
    type = serializers.CharField(read_only=True)
    type_label = serializers.CharField(source="get_type_display", read_only=True)
    status = serializers.CharField(read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    is_closed = serializers.BooleanField(read_only=True)
    needs_action = serializers.SerializerMethodField()
    amount_usd = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    amount_iqd = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    method = serializers.SerializerMethodField()
    wallet_number = serializers.CharField(source="wallet_number_snapshot", read_only=True)
    # Spec §2: on a withdrawal the merchant sees where the money goes, because
    # they cannot send it otherwise — and never whose account that is.
    destination_account = serializers.CharField(read_only=True)
    submitted_at = serializers.SerializerMethodField()
    assigned_at = serializers.SerializerMethodField()

    def get_needs_action(self, request_obj) -> bool:
        return request_obj.status == RequestStatus.ASSIGNED

    def get_method(self, request_obj) -> str:
        method = request_obj.payment_method
        return method.caption_ar or method.caption_en or method.code

    def get_submitted_at(self, request_obj):
        return _iso(request_obj.submitted_at)

    def get_assigned_at(self, request_obj):
        return _iso(request_obj.assigned_at)


class MerchantRequestDetailSerializer(MerchantRequestSummarySerializer):
    """The request detail screen (spec §8), and the only place the thread lives."""

    merchant_actioned_at = serializers.SerializerMethodField()
    closed_at = serializers.SerializerMethodField()
    timeline = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()
    messages = serializers.SerializerMethodField()
    actions = serializers.SerializerMethodField()

    def get_merchant_actioned_at(self, request_obj):
        return _iso(request_obj.merchant_actioned_at)

    def get_closed_at(self, request_obj):
        return _iso(request_obj.closed_at)

    def get_timeline(self, request_obj) -> list:
        """The lifecycle, from the shared state machine rather than a copy here.

        ``track()`` names statuses and times only, so there is nothing in it to
        mask — but it goes through the same guard as everything else.
        """
        return [
            {
                "key": step["key"],
                "label": str(step["label"]),
                "state": step["state"],
                "at": _iso(step["at"]),
            }
            for step in track(request_obj)
        ]

    def get_attachments(self, request_obj) -> list:
        return MerchantAttachmentSerializer(
            request_obj.attachments.all(), many=True, context=self.context
        ).data

    def get_messages(self, request_obj) -> list:
        return MerchantMessageSerializer(
            visible_messages(request_obj, self.viewer), many=True, context=self.context
        ).data

    def get_actions(self, request_obj) -> list:
        """The moves this merchant may make right now.

        Read from :mod:`apps.transactions.services`, which already checks the
        lifecycle, the permission, and that the request is actually theirs — so
        the panel renders exactly the buttons that will work and the API
        advertises exactly the same set.
        """
        return [
            {
                "action": move.action,
                "label": str(move.label),
                "tone": move.tone,
                "confirm": str(move.confirm_for(request_obj)),
                "requires_reason": move.requires_reason,
            }
            for move in available_transitions(request_obj, self.viewer)
        ]


class MerchantWalletSerializer(MerchantSafeSerializer):
    """A merchant's own wallet, read-only (spec §8).

    ``created_by`` — which Finance user added it — and Finance's notes on the
    merchant are not here. The merchant sees their own numbers and which one is
    currently taking money.
    """

    id = serializers.IntegerField(read_only=True)
    number = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    method = serializers.SerializerMethodField()
    is_active = serializers.BooleanField(read_only=True)
    daily_cap = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True, allow_null=True
    )
    deactivated_at = serializers.SerializerMethodField()

    def get_method(self, wallet) -> str:
        method = wallet.merchant_method.payment_method
        return method.caption_ar or method.caption_en or method.code

    def get_deactivated_at(self, wallet):
        return _iso(wallet.deactivated_at)
