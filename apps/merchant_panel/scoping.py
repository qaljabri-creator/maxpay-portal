"""Which rows exist, as far as a merchant is concerned (spec §8).

"Queue of assigned requests only" is an *object-level* rule, so no permission
expresses it — the merchant role's permissions say a merchant may view requests,
not which ones. That "which ones" lives here, in one queryset, so every screen
and every endpoint narrows the same way and none of them has to remember to.

Only ``merchant_assigned`` counts. A merchant the client picked but Finance
routed elsewhere (spec §5 allows exactly that) has no business with the request
and must not see it — not even to know it existed.
"""

from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext_lazy as _

from apps.accounts.permissions import assert_merchant_anonymity
from apps.merchants.models import Merchant, Wallet
from apps.transactions.models import Attachment, Request


def merchant_of(user) -> Merchant | None:
    """The :class:`~apps.merchants.models.Merchant` ``user`` signs in as."""
    return getattr(user, "merchant_profile", None)


def require_merchant(user) -> Merchant:
    """The caller's merchant, or refuse.

    Three separate things have to hold, and all three are refusals rather than
    empty results: the account is a merchant, it is wired to a merchant record,
    and that record is still active. A deactivated merchant keeps their history
    in the database and loses their panel.
    """
    if not getattr(user, "is_authenticated", False):
        raise PermissionDenied(_("سجّل الدخول أولًا."))
    if getattr(user, "role", None) != "merchant":
        # Finance is refused here as deliberately as anyone else. Client
        # identity is not the only reason a surface is scoped: this panel shows
        # one merchant's worklist, and there is no such thing as Finance's.
        raise PermissionDenied(_("لوحة التاجر متاحة لحسابات التجار فقط."))

    # Cheap, and it is the assertion spec §2 is actually made of: a merchant
    # account that somehow acquired an identity permission stops here.
    assert_merchant_anonymity(user)

    merchant = merchant_of(user)
    if merchant is None:
        raise PermissionDenied(_("هذا الحساب غير مرتبط بسجل تاجر. راجع المالية."))
    if not merchant.is_active:
        raise PermissionDenied(_("حساب التاجر موقوف حاليًا. راجع المالية."))
    return merchant


def requests_for(merchant: Merchant):
    """Every request routed to ``merchant``, newest first.

    ``client`` is not in ``select_related`` on purpose. The serializers cannot
    reach it whatever the queryset does, but a query that never fetches the row
    is one that cannot end up in a debug page, a log line or a ``repr``.
    """
    return (
        Request.objects.filter(merchant_assigned=merchant)
        .select_related("payment_method")
        .order_by("-assigned_at", "-submitted_at", "-id")
    )


def request_or_none(merchant: Merchant, reference: str) -> Request | None:
    return (
        requests_for(merchant)
        .prefetch_related("attachments", "messages__attachment")
        .filter(public_ref=reference)
        .first()
    )


def attachment_or_none(merchant: Merchant, attachment_id: int) -> Attachment | None:
    """A file, but only if it hangs off a request this merchant holds."""
    return (
        Attachment.objects.filter(
            pk=attachment_id, request__merchant_assigned=merchant
        )
        .select_related("request")
        .first()
    )


def wallets_for(merchant: Merchant):
    """The merchant's own wallets, read-only (spec §8).

    Deactivated wallets are included: a merchant looking at an old request needs
    to recognise the number it was paid into, and ``is_active`` is what the
    screen shows rather than what it filters on.

    **Archived** ones are not. Deactivated means stood down and still theirs;
    archived means Finance is done with it, and "disappears from everywhere" has
    to include the screen its owner reads or it does not mean anything.
    """
    return (
        Wallet.objects.filter(
            merchant_method__merchant=merchant, archived_at__isnull=True
        )
        .select_related("merchant_method__payment_method")
        .order_by("merchant_method__payment_method__sort_order", "-is_active", "-created_at")
    )
