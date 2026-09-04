"""What a client may choose from, and how it is described to them (spec §7).

Screens 2 and 3 of the client flow are lists, and both have to be *offerable*
lists: a merchant with no active method, or a method of theirs with no active
wallet, is a dead end the client would only discover on the next screen. So the
availability rule is applied once, here, and every screen reads the same one.

**The merchant comes first, then their methods.** The client is choosing who to
hand money to before choosing how, because that is the order they actually care
in: reassurance about the counterparty first, then what that counterparty
covers. Screen 2 is therefore the merchant list and screen 3 is the methods
*that merchant* covers — so a method is never offered by a merchant who cannot
serve it, which the previous order could only guarantee one screen later.

For a deposit that rule is: the method is active and supports deposits, the
merchant is active, their ``MerchantMethod`` is active, and it has an active
wallet — because the client is about to be shown a number to pay into. A
withdrawal needs no wallet (the merchant pays out), which is why the wallet
requirement is derived from the request type rather than assumed.

Nothing here is client-specific. It is the public catalogue, and it deliberately
contains no client data at all.
"""

from django.db.models import Count, Exists, OuterRef, Q
from django.urls import reverse

from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.transactions.models import RequestType


def requires_wallet(request_type: str) -> bool:
    """A deposit needs somewhere to pay into; a withdrawal does not (spec §6)."""
    return request_type == RequestType.DEPOSIT


def _offerable_methods(request_type: str):
    """``MerchantMethod`` rows that can actually take this kind of request."""
    queryset = MerchantMethod.objects.filter(
        is_active=True,
        merchant__is_active=True,
        # An archived merchant is gone from the client's choice as thoroughly
        # as from Finance's lists. Stated here as well as relying on
        # ``is_active``, which archiving also clears: two conditions that must
        # both hold is the cheap way to survive somebody clearing one of them.
        merchant__archived_at__isnull=True,
        payment_method__is_active=True,
    )
    if request_type == RequestType.DEPOSIT:
        queryset = queryset.filter(payment_method__supports_deposit=True)
    elif request_type == RequestType.WITHDRAWAL:
        queryset = queryset.filter(payment_method__supports_withdrawal=True)
    else:
        return MerchantMethod.objects.none()

    if requires_wallet(request_type):
        queryset = queryset.filter(
            Exists(
                Wallet.objects.filter(
                    merchant_method=OuterRef("pk"),
                    is_active=True,
                    archived_at__isnull=True,
                )
            )
        )
    return queryset


def available_merchants(request_type: str, method: PaymentMethod | None = None):
    """Merchants who can take this direction (spec §7, screen 2).

    Annotated with ``method_count`` so a row can say how much a merchant
    covers without a second query per row.

    ``method`` narrows it to the merchants covering that one. Screen 2 never
    passes it — the client has not chosen a method yet — but the submission
    endpoint does, because what has to hold at submission is that the *pair* is
    offerable, whichever order the two were picked in.
    """
    offerable = _offerable_methods(request_type)
    if method is not None:
        offerable = offerable.filter(payment_method=method)
    return (
        Merchant.objects.filter(pk__in=offerable.values("merchant_id"))
        .annotate(
            method_count=Count(
                "methods__payment_method",
                filter=Q(methods__in=offerable),
                distinct=True,
            )
        )
        .order_by("name")
    )


def available_methods(request_type: str, merchant: Merchant | None = None):
    """Payment methods that can actually take this request (spec §7, screen 3).

    With a merchant, the methods *that merchant* covers — which is what screen
    3 lists. Without one, every method anybody covers: the submission endpoint
    resolves a posted method before it knows whether the merchant is still
    offerable, and needs the wider list to tell "no such method" apart from
    "not from this merchant".
    """
    offerable = _offerable_methods(request_type)
    if merchant is not None:
        offerable = offerable.filter(merchant=merchant)
    return PaymentMethod.objects.filter(
        pk__in=offerable.values("payment_method_id"),
    ).order_by("sort_order", "caption_ar")


def merchant_method(request_type: str, method: PaymentMethod, merchant: Merchant):
    """The offerable ``MerchantMethod`` joining the two, or ``None``.

    ``None`` means the pairing stopped being offerable — deactivated, or its
    last wallet stood down — between two screens.
    """
    return (
        _offerable_methods(request_type)
        .filter(payment_method=method, merchant=merchant)
        .select_related("merchant", "payment_method")
        .first()
    )


def active_wallet(link: MerchantMethod):
    """The one wallet currently accepting funds for ``link`` (spec §5)."""
    return (
        link.wallets.filter(is_active=True, archived_at__isnull=True)
        .order_by("-created_at", "-id")
        .first()
    )


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


def method_payload(method: PaymentMethod) -> dict:
    """One row on screen 3.

    ``icon`` is a URL into :func:`apps.portal.flow_views.MethodIconView` rather
    than a media path — ``MEDIA_ROOT`` is never served (spec §11) — and is
    ``None`` when nothing has been uploaded, so the embed can fall back to a
    monogram instead of rendering a broken image.
    """
    return {
        "code": method.code,
        "caption": method.caption_ar or method.caption_en or method.code,
        "caption_en": method.caption_en,
        "icon": (
            reverse("portal:method_icon", kwargs={"code": method.code})
            if method.icon
            else None
        ),
    }


def merchant_payload(merchant: Merchant) -> dict:
    """One row on screen 2. A merchant's name is public to the client — they
    are choosing who to pay, and the anonymity of spec §2 runs the other way."""
    return {
        "id": merchant.pk,
        "name": merchant.name,
        "method_count": getattr(merchant, "method_count", None),
    }


def wallet_payload(wallet: Wallet) -> dict:
    """What screen 4 shows, with the id the submission is checked against.

    ``id`` travels back with the submission so a wallet swapped between the two
    can be caught rather than silently snapshotted (spec §5).

    ``qr`` is the other way a client can be told where to pay: some rails
    (Super QI) are settled by scanning a code, not by typing an account, and
    for those ``number`` is empty and the code is the whole instruction. Both
    can be present — a rail may issue a QR *and* an account — so the screen is
    told about each independently rather than being handed a mode to switch on.

    It is deliberately not called ``image``. The other picture in this payload
    is ``method.icon``, and the two are for opposite things: one identifies a
    rail at favicon size, the other is pointed a camera at.
    """
    from django.urls import reverse

    qr = ""
    if wallet.qr_image:
        qr = reverse("portal:wallet_qr", kwargs={"pk": wallet.pk})
    return {
        "id": wallet.pk,
        "number": wallet.number,
        "label": wallet.label,
        "qr": qr,
    }
