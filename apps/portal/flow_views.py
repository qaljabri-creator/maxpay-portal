"""The client request flow's endpoints (spec §7) — build-order step 6.

The six screens are one page. They have to be: the portal lives in a
cross-site iframe, where Django's CSRF cookie never arrives and a plain
``<form method="post">`` therefore cannot be defended (see the module docstring
of :mod:`apps.portal.views`). What defends these calls instead is a header the
page echoes from its session — and only ``fetch`` can set a header. So the
wizard navigates in the browser and talks to the JSON below, rather than
navigating between server-rendered pages.

Five endpoints, in the order the client meets them::

    GET  /portal/options/                    what can be chosen — progressively
    POST /portal/requests/                   submit a deposit or withdrawal
    GET  /portal/requests/                   the client's own history
    GET  /portal/requests/<ref>/             one request in full (screen 6)
    POST /portal/requests/<ref>/messages/    write into its thread (step 9)

plus two file routes: the signed attachment URL that spec §11 requires, and the
payment-method icons that screen 2 is made of.

Every one of them scopes to ``self.client`` — the client the *session* names,
never an identifier from the request. Spec §4: never trust a client-supplied
account number.
"""

import logging
import mimetypes

from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.cache import cache_control

from apps.core import hours as business_hours
from apps.core.choices import ActorRole
from apps.merchants.models import PaymentMethod, Wallet
from apps.transactions import messaging
from apps.transactions.models import Attachment, Request, RequestType

from . import attachments, catalog, payloads, pricing, ratelimit, submissions
from .views import PortalEndpoint, error

logger = logging.getLogger("maxpay.portal")

#: How many past requests the history screen asks for at once.
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 50


class ClientEndpoint(PortalEndpoint):
    """A portal endpoint that only exists for an authenticated client."""

    requires_client = True


# ---------------------------------------------------------------------------
# Screens 1–4: what can be chosen
# ---------------------------------------------------------------------------


class OptionsView(ClientEndpoint):
    """Everything screens 1 to 4 need, resolved as deep as the query goes.

    One endpoint rather than four because each screen's answer narrows the
    next: pass nothing and get the merchants, add ``merchant`` and get the
    methods that merchant covers, add ``method`` and get the wallet number
    screen 4 displays. A client stepping back and forth re-asks, and always
    gets what is true now.

    When a choice made on an earlier screen has since stopped being offerable —
    a merchant deactivated, a wallet stood down — the answer is not an error.
    It is the shallower answer plus ``unavailable``, naming the screen the
    client has to go back to.
    """

    def get(self, request, *args, **kwargs):
        if not ratelimit.allow(
            request, scope="catalog", rate=settings.PORTAL_CATALOG_RATE
        ):
            return error("rate_limited", _("محاولات كثيرة. أمهل قليلًا."), status=429)

        request_type = request.GET.get("type") or RequestType.DEPOSIT
        if request_type not in RequestType.values:
            return error("type_unknown", _("نوع طلب غير معروف."), status=400)

        hours = business_hours.evaluate()

        body = {
            "type": request_type,
            "types": self._types(),
            "rate": None,
            "methods": [],
            # Screen 4 is not the same screen in both directions, and the shape
            # of it is the server's to decide: a wallet number to pay into, or a
            # destination to be paid at (spec §7). The embed reads this rather
            # than branching on the type itself, so the two never disagree about
            # which fields a submission must carry.
            "needs": self._needs(request_type),
            # Build-order step 11. Sent with every catalogue answer rather than
            # from its own endpoint, so the screen that would collect a
            # submission and the fact that submissions are being refused can
            # never be one poll out of step with each other (spec §7).
            "hours": hours.payload(),
            "unavailable": None,
        }

        if not hours.is_open:
            # Spec §7: outside business hours every submission screen is
            # replaced by the closed notice. Nothing below this line would be
            # actionable, so nothing below it is resolved or sent — a wallet
            # number the client cannot pay into today is a number they should
            # not be looking at.
            return JsonResponse(body)

        try:
            body["rate"] = pricing.rate_payload(
                pricing.current_rate(request_type), request_type
            )
        except pricing.PricingError as exc:
            # Finance has not set a rate. The client cannot be quoted, so say
            # so plainly rather than showing them a zero.
            body["unavailable"] = "rate"
            body["detail"] = str(exc.message)
            return JsonResponse(body)

        merchants = list(catalog.available_merchants(request_type))
        body["merchants"] = [catalog.merchant_payload(m) for m in merchants]

        raw_merchant = (request.GET.get("merchant") or "").strip()
        if not raw_merchant:
            return JsonResponse(body)

        merchant = next((m for m in merchants if str(m.pk) == raw_merchant), None)
        if merchant is None:
            body["unavailable"] = "merchant"
            return JsonResponse(body)
        body["merchant"] = catalog.merchant_payload(merchant)

        methods = list(catalog.available_methods(request_type, merchant))
        body["methods"] = [catalog.method_payload(m) for m in methods]

        code = (request.GET.get("method") or "").strip()
        if not code:
            return JsonResponse(body)

        method = next((m for m in methods if m.code == code), None)
        if method is None:
            body["unavailable"] = "method"
            return JsonResponse(body)
        body["method"] = catalog.method_payload(method)

        link = catalog.merchant_method(request_type, method, merchant)
        if link is None:
            # The pairing went away between two screens. Named as a *method*
            # problem now rather than a merchant one: the merchant is still
            # offerable, it is this method of theirs that is not, and sending
            # the client back a screen further than they need to go would make
            # them re-choose something that is still fine.
            body["unavailable"] = "method"
            return JsonResponse(body)

        # A withdrawal stops here: the merchant pays out, so there is no number
        # to display and nothing further to resolve (spec §7, screen 4).
        if not catalog.requires_wallet(request_type):
            return JsonResponse(body)

        wallet = catalog.active_wallet(link)
        if wallet is None:
            body["unavailable"] = "wallet"
            return JsonResponse(body)
        body["wallet"] = catalog.wallet_payload(wallet)
        return JsonResponse(body)

    @staticmethod
    def _types() -> list[dict]:
        """Screen 1. ``available`` is what makes the tile tappable."""
        return [
            {
                "value": value,
                "label": str(RequestType(value).label),
                "available": True,
            }
            for value in (RequestType.DEPOSIT, RequestType.WITHDRAWAL)
        ]

    @staticmethod
    def _needs(request_type: str) -> dict:
        """What screen 4 collects for this direction, and what it displays."""
        withdrawal = request_type == RequestType.WITHDRAWAL
        return {
            "wallet": catalog.requires_wallet(request_type),
            "destination": withdrawal,
            "proof": not withdrawal,
        }


# ---------------------------------------------------------------------------
# Screens 4–5: submitting, and the history it joins
# ---------------------------------------------------------------------------


class RequestsView(ClientEndpoint):
    """``GET`` the client's own requests, ``POST`` a new one of either direction.

    The type is a field on the submission, not a route: what a deposit and a
    withdrawal have in common is nearly everything, and
    :mod:`apps.portal.submissions` is where the little that differs is decided.
    """

    def get(self, request, *args, **kwargs):
        limit = self._limit(request.GET.get("limit"))
        queryset = (
            Request.objects.filter(client=self.client)
            .select_related("payment_method")
            .order_by("-submitted_at", "-id")
        )
        rows = list(queryset[: limit + 1])
        return JsonResponse(
            {
                "requests": [payloads.summary_payload(r) for r in rows[:limit]],
                "has_more": len(rows) > limit,
            }
        )

    def post(self, request, *args, **kwargs):
        # Spec §11: rate limiting on the submission endpoints. Applied before
        # the file is read, so a flood costs no disk.
        if not ratelimit.allow(
            request, scope="submission", rate=settings.PORTAL_SUBMISSION_RATE
        ):
            return error(
                "rate_limited",
                _("قدّمت طلبات كثيرة خلال وقت قصير. أمهل قليلًا ثم أعد المحاولة."),
                status=429,
            )

        try:
            draft = submissions.build_draft(self.client, request.POST, request.FILES)
            created = submissions.create_request(draft, http_request=request)
        except submissions.SubmissionError as exc:
            return error(
                exc.code,
                str(exc.message),
                status=exc.status,
                remedy="fix_input" if exc.status == 400 else "review",
                **exc.extra,
            )

        return JsonResponse(
            {"request": payloads.detail_payload(created, self.client)}, status=201
        )

    @staticmethod
    def _limit(raw) -> int:
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_HISTORY_LIMIT
        return max(1, min(limit, MAX_HISTORY_LIMIT))


class RequestDetailView(ClientEndpoint):
    """Screen 6 — one request, scoped to the client the session names."""

    def get(self, request, *args, **kwargs):
        deposit = self.find(kwargs.get("reference", ""))
        if deposit is None:
            # Someone else's reference and a reference that never existed are
            # the same answer: guessing must not confirm anything.
            return error("not_found", _("لا يوجد طلب بهذا المرجع."), status=404)
        return JsonResponse({"request": payloads.detail_payload(deposit, self.client)})

    def find(self, reference: str):
        return (
            Request.objects.filter(client=self.client, public_ref=reference)
            .select_related("payment_method", "merchant_selected")
            .prefetch_related("attachments", "messages__attachment")
            .first()
        )


class RequestMessagesView(RequestDetailView):
    """``POST`` a message into the client's own thread (spec §9).

    The conversation is not gated on the request's status. A client asking
    something after their request closed is still asking something, and refusing
    it would only move the conversation somewhere nobody can audit.

    Who the message reaches is not this view's business: it writes one row, and
    :mod:`apps.transactions.messaging` decides who may read it. The merchant
    holding the request sees it labelled "العميل" and nothing more (spec §2, §5).
    """

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        # Before the file is read, so a flood costs no disk (spec §11).
        if not ratelimit.allow(
            request, scope="message", rate=settings.PORTAL_MESSAGE_RATE
        ):
            return error(
                "rate_limited",
                _("رسائل كثيرة خلال وقت قصير. أمهل قليلًا ثم أعد المحاولة."),
                status=429,
            )

        deposit = self.find(kwargs.get("reference", ""))
        if deposit is None:
            return error("not_found", _("لا يوجد طلب بهذا المرجع."), status=404)

        try:
            message = messaging.post(
                deposit,
                sender_role=ActorRole.CLIENT,
                sender_id=self.client.pk,
                body=request.POST.get("body"),
                upload=request.FILES.get("attachment"),
            )
        except messaging.MessageError as exc:
            return error(
                exc.code,
                str(exc.message),
                status=exc.status,
                remedy="fix_input" if exc.status == 400 else "review",
            )

        return JsonResponse(
            {"message": payloads.message_payload(message, self.client)}, status=201
        )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class AttachmentView(ClientEndpoint):
    """A stored file, behind a signed URL *and* the session it was signed for.

    The signature proves the link was minted by us for this client; the query
    below proves the file belongs to a request that client owns. Neither is
    dropped in favour of the other — a signature that outlives a revoked client
    would otherwise keep working (spec §11).
    """

    def get(self, request, *args, **kwargs):
        attachment_id = attachments.unsign(kwargs["token"], self.client)
        if attachment_id != int(kwargs["pk"]):
            raise Http404("Token does not match the requested attachment.")

        attachment = get_object_or_404(
            Attachment.objects.select_related("request"),
            pk=attachment_id,
            request__client=self.client,
        )
        return attachments.serve(attachment)


@method_decorator(
    # Brand marks, not client data: they change about never, and a client
    # walking the wizard would otherwise refetch every tile on every step.
    cache_control(private=True, max_age=3600),
    name="dispatch",
)
class MethodIconView(View):
    """The icon on a payment-method tile (spec §7, screen 2).

    Deliberately outside :class:`ClientEndpoint`. ``MEDIA_ROOT`` is never served
    (spec §11), so an icon needs a route of its own; requiring a session for it
    would make every tile uncacheable to save nothing, since the only thing
    disclosed is which payment rails MaxPay offers — the same list any client
    sees on the screen after login, and no client's data at all.
    """

    def get(self, request, *args, **kwargs):
        method = get_object_or_404(
            PaymentMethod, code=kwargs["code"], is_active=True
        )
        if not method.icon:
            raise Http404("No icon for this payment method.")
        try:
            data = method.icon.read()
        except (FileNotFoundError, OSError) as exc:
            raise Http404("The stored icon is missing.") from exc
        finally:
            method.icon.close()

        response = HttpResponse(data, content_type=_image_type(method.icon.name))
        response["X-Content-Type-Options"] = "nosniff"
        return response


@method_decorator(
    # The same reasoning as the method icon above, with one difference worth
    # naming: a wallet's QR resolves to a merchant's account, so it is *not*
    # cached as long and it is only served for a wallet that is currently
    # active — a retired code should stop being fetchable with the URL somebody
    # kept.
    cache_control(private=True, max_age=300),
    name="dispatch",
)
class WalletQrView(View):
    """The scannable code screen 4 shows, in its own block under its own title.

    Outside :class:`ClientEndpoint` for the same reason the method icon is:
    ``MEDIA_ROOT`` is never served (spec §11), so the picture needs a route,
    and what it discloses is a merchant's own payment destination — which is
    exactly what screen 4 shows every client who reaches it anyway.

    Not to be confused with :class:`MethodIconView`. That one serves a brand
    mark rendered at 1.9rem beside a method's name; this one serves a code
    rendered as large as the frame allows on a white plate. Separate routes for
    separate purposes, so neither can end up in the other's slot.
    """

    def get(self, request, *args, **kwargs):
        wallet = get_object_or_404(
            Wallet.objects.select_related("merchant_method__merchant"),
            pk=kwargs["pk"],
            is_active=True,
            merchant_method__is_active=True,
            merchant_method__merchant__is_active=True,
        )
        if not wallet.qr_image:
            raise Http404("No QR for this wallet.")
        try:
            data = wallet.qr_image.read()
        except (FileNotFoundError, OSError) as exc:
            raise Http404("The stored wallet QR is missing.") from exc
        finally:
            wallet.qr_image.close()

        response = HttpResponse(data, content_type=_image_type(wallet.qr_image.name))
        response["X-Content-Type-Options"] = "nosniff"
        return response


def _image_type(name: str) -> str:
    """The icon's type from its name, refusing to claim anything is an image.

    Anything that does not resolve to an image is served as an opaque download
    rather than mislabelled — the field is an ``ImageField``, so this only
    triggers on something that got past Pillow, and that is worth not rendering.
    """
    guessed, _encoding = mimetypes.guess_type(name or "")
    return guessed if guessed and guessed.startswith("image/") else "application/octet-stream"
