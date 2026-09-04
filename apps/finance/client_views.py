"""One client's whole history, in one place (Finance review 4.1).

Finance's complaint was concrete: a client rings about a request, and the only
way to find out whether this is the third time this week is to search the queue
four different ways and read the results by eye. Every request this client has
ever filed, with what they add up to, is one screen.

**It is the most identifying screen in the system, and it is gated as one.**
Behind ``accounts.view_client_identity`` and nothing less — not the Finance
role, which every ``finance_staff`` account has, but the permission a
``finance_admin`` grants and withdraws per person (spec §3). The queue's client
*column* disappears without it; this page does not exist without it, because a
page whose entire subject is "who is this person" has nothing left to render
once the answer is withheld.

Nothing here is a second query. The rows come from the queue's own
:func:`~apps.finance.queue_views._base_queryset` and render through the queue's
own row partial, so a request cannot look like one thing in the worklist and
another in the history.
"""

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView

from apps.accounts.models import Client
from apps.reports import aggregates
from apps.transactions import reads

from .mixins import FinancePanelMixin
from .queue_views import PERM_VIEW_IDENTITY, _base_queryset
from .report_views import PERM_EXPORT


class ClientHistoryView(FinancePanelMixin, ListView):
    """Every request by one client, newest first."""

    template_name = "finance/client_history.html"
    context_object_name = "requests"
    paginate_by = 25
    nav_section = "requests"

    def dispatch(self, request, *args, **kwargs):
        # Checked before the Finance-role check has even run its course, and
        # raised rather than redirected: there is no version of this page for
        # somebody without the permission, so there is nothing to degrade to.
        if request.user.is_authenticated and not request.user.has_perm(PERM_VIEW_IDENTITY):
            raise PermissionDenied(_("سجل العميل متاح لمن يملك صلاحية الاطلاع على هوية العميل."))
        return super().dispatch(request, *args, **kwargs)

    def get_client(self) -> Client:
        if not hasattr(self, "_client"):
            self._client = get_object_or_404(Client, pk=self.kwargs["pk"])
        return self._client

    def get_queryset(self):
        return _base_queryset().filter(client=self.get_client()).order_by("-submitted_at", "-id")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        client = self.get_client()
        queryset = self.get_queryset()

        context["client_record"] = client
        # Over the whole history, not over the page: "this client has moved
        # $12,000 through us" is the question, and answering it from twenty-five
        # rows would answer a different one.
        context["summary"] = aggregates.summarise(queryset)
        context["by_method"] = aggregates.by_method(queryset)
        context["first_request"] = queryset.order_by("submitted_at", "id").first()
        context["last_request"] = queryset.first()

        # The same unread marking the queue does, so a client's history is a
        # worklist too rather than an archive that forgets what you have read.
        page = context.get("page_obj")
        context["unread_refs"] = reads.unread_references(
            user=self.request.user,
            audience=reads.FINANCE,
            requests=page.object_list if page is not None else context["requests"],
        )
        context["can_see_identity"] = True  # or dispatch would have refused
        context["can_export"] = self.request.user.has_perm(PERM_EXPORT)
        # 4.3: the history exports through the report's own gates rather than
        # growing an export of its own. One filtered report, one permission,
        # one audit entry — see apps.reports.filters.ReportFilter.client.
        context["report_url"] = f"{reverse('finance:report')}?client={client.pk}"
        context["export_url"] = f"{reverse('finance:report_export')}?client={client.pk}"
        context["querystring"] = ""
        return context
