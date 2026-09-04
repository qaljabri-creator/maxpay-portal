"""Live updates for the Finance panel (spec §10) — build-order step 13.

Spec §10 is explicit about the mechanism and about its limits: *in-app polling
every ten seconds on the merchant and finance panels; no WebSocket in phase 1.*
So there is no channel layer, no ASGI worker and no long poll here — three
ordinary ``GET`` endpoints and a script that asks them for the time.

The shape is deliberately two-tier, because a ten-second poll that re-renders a
page every ten seconds is a page render every ten seconds per open tab:

``pulse``
    Counts, and a short **version token** summarising the newest activity the
    caller can see. Cheap: aggregates, no serialisation, no template. This is
    what actually runs six times a minute.
``rows`` / ``thread``
    HTML fragments, fetched **only when the version token has moved**. They are
    rendered from the same templates the full page includes, so the live view
    and the loaded view cannot drift apart, and there is no second copy of the
    markup living in JavaScript.

Fragments rather than JSON-plus-client-rendering is the important choice. The
alternative would put the queue's markup in two places — a Django template and
a JS renderer — and the two would disagree the first time anyone edited one of
them. Spec §2's masking would then have to hold in both.

Everything here is read-only. A poll never writes, and in particular never
marks anything read: opening the detail *screen* is what does that, because it
is the only moment the system can honestly claim a person looked.
"""

from django.http import JsonResponse
from django.utils import timezone
from django.views.generic import TemplateView
from django.views.generic.base import View

from apps.core import hours as business_hours
from apps.transactions import reads
from apps.transactions.models import Request
from apps.transactions.services import AWAITING_FINANCE

from .mixins import FinancePanelMixin
from .queue_views import RequestDetailView, RequestQueueView, queue_counts


class FinancePulseView(FinancePanelMixin, View):
    """``GET /finance/pulse/`` — what the rail and the tabs should say now.

    Answers in one small object so the poll costs a handful of aggregates. The
    ``version`` fields are opaque to the browser: it compares them with what it
    last saw and asks for a fragment only when one has changed.
    """

    http_method_names = ["get", "head"]

    def get(self, request, *args, **kwargs):
        counts = queue_counts()
        user = request.user

        # Scoped to what is still in flight. A badge counting closed history
        # would only ever go up, and a number that never falls is not a signal.
        open_requests = Request.objects.filter(status__in=AWAITING_FINANCE)

        body = {
            "counts": counts,
            # Spec §10: "new submission and merchant confirmation both raise a
            # badge". Both live in AWAITING_FINANCE, so the rail badge is the
            # badge — what this adds is how much of it nobody has looked at.
            "unread": reads.unread_count(
                user=user, audience=reads.FINANCE, queryset=open_requests
            ),
            "queue_version": reads.latest_activity(
                user=user, audience=reads.FINANCE, queryset=self._visible_queue()
            ),
            "portal_closed": not business_hours.is_open(),
            "server_time": timezone.now().isoformat(),
        }
        response = JsonResponse(body)
        response["Cache-Control"] = "no-store"
        return response

    def _visible_queue(self):
        """The queue the caller is actually looking at, filters and all.

        The poll sends back the queue screen's own query string, so a desk
        filtered to one merchant is not told to refresh because something moved
        on a request their filter excludes.
        """
        view = RequestQueueView()
        view.request = self.request
        view.kwargs = {}
        view.args = ()
        return view.get_queryset()


class FinanceQueueRowsView(FinancePanelMixin, TemplateView):
    """``GET /finance/requests/rows/`` — the queue's table body, on its own.

    The same template the full page includes, with the same context, so a
    refreshed queue is byte-for-byte what a reload would have produced.
    """

    template_name = "finance/_queue_rows.html"
    nav_section = "requests"

    def get_context_data(self, **kwargs):
        # Borrow the queue view wholesale rather than reimplementing its
        # filters, its pagination and its identity gating. Two copies of that
        # logic is two chances for the live view to show a row the loaded view
        # would have hidden.
        view = RequestQueueView()
        view.request = self.request
        view.kwargs = {}
        view.args = ()
        view.object_list = view.get_queryset()
        context = view.get_context_data(object_list=view.object_list)
        context.update(kwargs)
        context["live_fragment"] = True
        return context


class FinanceThreadView(FinancePanelMixin, TemplateView):
    """``GET /finance/requests/<ref>/thread/`` — the conversation, on its own.

    Only the thread. The composer beside it is deliberately *not* in the
    fragment: replacing a textarea somebody is typing into, every ten seconds,
    would be a worse feature than no live thread at all.
    """

    template_name = "finance/_thread.html"
    nav_section = "requests"

    def get_context_data(self, **kwargs):
        view = RequestDetailView()
        view.request = self.request
        view.kwargs = {"ref": kwargs["ref"]}
        view.args = ()
        view.object = view.get_object()
        context = view.get_context_data(object=view.object)
        context["live_fragment"] = True
        return context
