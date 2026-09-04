"""The merchant panel's JSON API (spec §8, §11).

Every endpoint here is read-only and merchant-scoped. Two properties hold on all
of them, and the base class is what makes both structural rather than habitual:

* **Scope.** :func:`~apps.merchant_panel.scoping.require_merchant` runs before
  any handler, so an endpoint cannot accidentally be written open. Every
  queryset then narrows to that merchant's own rows.
* **Anonymity.** :meth:`MerchantAPIMixin.finalize_response` walks the finished
  payload and refuses to send one that names the client — including payloads
  the serializers did not build, such as the paginator's wrapper and DRF's own
  error bodies (spec §2, §11).

Writes are not here. A lifecycle move is a form post from the panel
(:mod:`apps.merchant_panel.views`), for the same reason the Finance panel posts
its moves: there is one place that changes ``Request.status``, and giving it two
entry points would mean two places to keep honest. The ten-second poll step 13
added reads these endpoints; it does not write through them, and in particular
it never marks anything read — opening the detail *screen* does that.
"""

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.transactions import reads
from apps.transactions.models import RequestStatus
from apps.transactions.services import OPEN_STATUSES

from . import scoping
from .anonymity import assert_anonymous
from .serializers import (
    MerchantRequestDetailSerializer,
    MerchantRequestSummarySerializer,
    MerchantWalletSerializer,
)

#: Queue filters, named for what a merchant actually asks their worklist.
STATUS_GROUPS = {
    "awaiting_me": frozenset({RequestStatus.ASSIGNED}),
    "open": OPEN_STATUSES,
}

#: What the queue answers when asked for nothing in particular. A worklist that
#: opens on last month's closed requests is one nobody works from.
DEFAULT_STATUS = "open"


class MerchantAPIMixin:
    """Merchant-only, merchant-scoped, and never a source of client identity."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # Before any handler, and before any queryset: no endpoint on this
        # surface can exist for a non-merchant, however it was written.
        self.merchant = scoping.require_merchant(request.user)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # The user, explicitly. Attachment signatures are minted per person and
        # the available moves are worked out per person.
        context["user"] = self.request.user
        return context

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        # The outermost net. The serializers already guard what they build; this
        # covers everything wrapped around them — pagination, error details, and
        # any future endpoint that returns a hand-built dict.
        assert_anonymous(getattr(response, "data", None), where=type(self).__name__)
        return response


class MerchantRequestQueueAPI(MerchantAPIMixin, generics.ListAPIView):
    """``GET /merchant/api/requests/`` — assigned requests only (spec §8).

    ``?status=awaiting_me`` is the merchant's real question: what is waiting on
    me right now. ``open`` is everything still in flight including what they
    have already actioned and Finance has not yet closed, and an empty
    ``status`` is the full history.
    """

    serializer_class = MerchantRequestSummarySerializer

    def get_queryset(self):
        queryset = scoping.requests_for(self.merchant)
        status = self.request.query_params.get("status", DEFAULT_STATUS)
        if status in STATUS_GROUPS:
            return queryset.filter(status__in=STATUS_GROUPS[status])
        if status in RequestStatus.values:
            return queryset.filter(status=status)
        # An unknown status is not an error worth a 400: the queue is a
        # worklist, and the honest answer to a filter nobody meant is all of it.
        return queryset


class MerchantRequestDetailAPI(MerchantAPIMixin, generics.RetrieveAPIView):
    """``GET /merchant/api/requests/<ref>/`` — one request in full (spec §8)."""

    serializer_class = MerchantRequestDetailSerializer

    def get_object(self):
        request_obj = scoping.request_or_none(self.merchant, self.kwargs["reference"])
        if request_obj is None:
            # A request routed to somebody else is not "forbidden", it does not
            # exist. A 403 would confirm the reference names something real.
            raise NotFound(_("لا يوجد طلب بهذا المرجع في لوحتك."))
        return request_obj


class MerchantWalletListAPI(MerchantAPIMixin, generics.ListAPIView):
    """``GET /merchant/api/wallets/`` — read-only view of own wallets (spec §8)."""

    serializer_class = MerchantWalletSerializer
    pagination_class = None

    def get_queryset(self):
        return scoping.wallets_for(self.merchant)


class MerchantPulseAPI(MerchantAPIMixin, APIView):
    """``GET /merchant/api/pulse/`` — the ten-second heartbeat (spec §8, §10).

    Spec §8 wants the queue auto-refreshing every ten seconds and spec §10 wants
    a new assigned request to appear without a page refresh, with an unread
    badge. Both are answered from here: the counts are what the rail and the
    tabs render, and ``queue_version`` is a token the browser compares with the
    last one it saw so it only pays for a re-render when something has actually
    moved.

    Deliberately small. This runs six times a minute per open tab, so it does
    aggregates and no serialisation — and it goes through
    :meth:`MerchantAPIMixin.finalize_response` like every other endpoint on this
    surface, because a payload nobody thought of as "data" is exactly the kind
    that grows a client field later (spec §2).
    """

    def get(self, request, *args, **kwargs):
        assigned = scoping.requests_for(self.merchant)
        awaiting = assigned.filter(status=RequestStatus.ASSIGNED)

        return Response(
            {
                "counts": {
                    "awaiting_me": awaiting.count(),
                    "open": assigned.filter(status__in=OPEN_STATUSES).count(),
                    "all": assigned.count(),
                },
                # Spec §10's badge. Counted over everything still in flight
                # rather than over the whole history: a number that can only
                # ever rise is not a signal anybody acts on.
                "unread": reads.unread_count(
                    user=request.user,
                    audience=reads.MERCHANT,
                    queryset=assigned.filter(status__in=OPEN_STATUSES),
                ),
                "queue_version": reads.latest_activity(
                    user=request.user,
                    audience=reads.MERCHANT,
                    queryset=self._visible_queue(assigned),
                ),
                "server_time": timezone.now().isoformat(),
            }
        )

    def _visible_queue(self, assigned):
        """The tab the merchant is actually looking at.

        The poll echoes the queue screen's own ``?status=``, so a merchant on
        "بانتظاري" is not told to refresh because a request they already
        actioned changed somewhere else in the list.
        """
        status = self.request.query_params.get("status", DEFAULT_STATUS)
        if status in STATUS_GROUPS:
            return assigned.filter(status__in=STATUS_GROUPS[status])
        if status in RequestStatus.values:
            return assigned.filter(status=status)
        return assigned
