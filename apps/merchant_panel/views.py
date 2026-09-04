"""The merchant panel's screens (spec §8) — build-order step 8.

The masking rule these screens obey is not written in them, and that is the
point. Spec §2 puts client anonymity at the serializer level, so every view here
renders **plain dictionaries produced by
:mod:`apps.merchant_panel.serializers`** and puts no model instance into the
template context at all. A template cannot leak ``request.client.email`` because
there is no object in scope to reach it through; the worst a careless template
edit can do is print a key that does not exist.

That is also why the queue paginates by hand rather than through ``ListView``:
``ListView`` helpfully leaves ``object_list`` and ``page_obj.object_list`` in
the context, both of them querysets of live ``Request`` rows. Convenient, and
exactly the object the whole step is meant to keep out of reach.

Writes are the one thing that does touch a model, because a lifecycle move is
applied to a row. They go straight to :mod:`apps.transactions.services` through
:mod:`apps.merchant_panel.actions` and nothing about the request is rendered
from the object afterwards — the redirect re-reads it through the serializer
like every other read.
"""

from django.contrib import messages
from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from apps.core.choices import ActorRole
from apps.finance.queue_forms import AmountCorrectionForm
from apps.transactions import messaging, reads
from apps.transactions.models import RequestStatus
from apps.transactions.services import (
    AMOUNT_EDITABLE_STATUSES,
    TransitionError,
    correct_amount,
    get_transition,
)

from . import actions, scoping
from . import attachments as attachment_urls
from .api import DEFAULT_STATUS, STATUS_GROUPS
from .forms import MerchantMessageForm, form_for
from .serializers import (
    MerchantRequestDetailSerializer,
    MerchantRequestSummarySerializer,
    MerchantWalletSerializer,
)

PAGE_SIZE = 25


def panel_poll_ms() -> int:
    """The poll interval, in milliseconds. Spec §8, §10: ten seconds."""
    from django.conf import settings

    return int(getattr(settings, "PANEL_POLL_SECONDS", 10)) * 1000


class MerchantPanelMixin(AccessMixin):
    """Login, a merchant account wired to an active merchant record, nothing else.

    Finance is refused here as firmly as an anonymous visitor. This panel is one
    merchant's worklist; there is no Finance version of it, and leaving a door
    open "for support" would be a surface on which the anonymity guarantee has
    to be argued rather than simply held.
    """

    nav_section = ""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        # Raises PermissionDenied with a message worth reading: wrong role, no
        # merchant record, or a suspended one.
        self.merchant = scoping.require_merchant(request.user)
        return super().dispatch(request, *args, **kwargs)

    def serializer_context(self) -> dict:
        return {"user": self.request.user}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav_section"] = self.nav_section
        context["merchant_name"] = self.merchant.name
        # The rail badge (spec §10). Server-rendered on load and then kept
        # current by the ten-second poll in static/js/panel.js, which reads
        # ``merchant_panel:api_pulse`` — so the number is right on arrival even
        # with scripting off, and right afterwards with it on.
        context["awaiting_me"] = (
            scoping.requests_for(self.merchant)
            .filter(status=RequestStatus.ASSIGNED)
            .count()
        )
        context["poll"] = {
            "pulseUrl": reverse("merchant_panel:api_pulse"),
            "intervalMs": panel_poll_ms(),
        }
        return context


# ------------------------------------------------------------------- queue --


class MerchantQueueView(MerchantPanelMixin, TemplateView):
    """The queue of assigned requests (spec §8).

    Opens on what is waiting for the merchant, because that is the question a
    merchant opens the panel to ask.
    """

    template_name = "merchant/queue.html"
    nav_section = "requests"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.queue_context())
        context["counts"] = self._counts()
        context["rows_url"] = reverse("merchant_panel:queue_rows")
        return context

    def queue_context(self) -> dict:
        """Everything the row fragment needs — and nothing the shell needs.

        Split out so :class:`MerchantQueueRowsView` renders the *same* rows the
        full page does rather than a second implementation of them, which is
        the only way a live queue and a reloaded queue can be guaranteed to
        agree (spec §8).
        """
        status = self.request.GET.get("status", DEFAULT_STATUS)

        queryset = scoping.requests_for(self.merchant)
        if status in STATUS_GROUPS:
            queryset = queryset.filter(status__in=STATUS_GROUPS[status])
        elif status in RequestStatus.values:
            queryset = queryset.filter(status=status)

        paginator = Paginator(queryset, PAGE_SIZE)
        page = paginator.get_page(self.request.GET.get("page"))

        return {
            # The only thing that crosses into the template: dictionaries.
            "requests": MerchantRequestSummarySerializer(
                page.object_list, many=True, context=self.serializer_context()
            ).data,
            # Spec §10's unread badge, per row. References rather than ids: a
            # merchant never sees a database key (spec §2).
            "unread_refs": reads.unread_references(
                user=self.request.user,
                audience=reads.MERCHANT,
                requests=page.object_list,
            ),
            "page": {
                "number": page.number,
                "num_pages": paginator.num_pages,
                "count": paginator.count,
                "has_previous": page.has_previous(),
                "has_next": page.has_next(),
                "previous": page.previous_page_number() if page.has_previous() else None,
                "next": page.next_page_number() if page.has_next() else None,
            },
            "active_status": status,
        }

    def _counts(self) -> dict:
        base = scoping.requests_for(self.merchant)
        return {
            "awaiting_me": base.filter(status=RequestStatus.ASSIGNED).count(),
            "open": base.filter(status__in=STATUS_GROUPS["open"]).count(),
            "all": base.count(),
        }


# ------------------------------------------------------------------ detail --


class MerchantRequestDetailView(MerchantPanelMixin, TemplateView):
    """One request in full, masked, with the moves this merchant may make."""

    template_name = "merchant/request_detail.html"
    nav_section = "requests"

    def get(self, request, *args, **kwargs):
        """Render the screen, and only then record that this merchant saw it.

        Spec §10: opening the screen is what clears the unread badge, and it is
        the only moment the system can honestly claim somebody looked. Kept out
        of ``get_context_data`` so that a fragment view reusing the context
        cannot inherit the side effect — the trap the Finance panel fell into
        and the reason both panels now mark here.
        """
        response = super().get(request, *args, **kwargs)
        reads.mark_seen(user=request.user, request_obj=self.subject)
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request_obj = scoping.request_or_none(self.merchant, kwargs["reference"])
        if request_obj is None:
            # Not 403: a request routed elsewhere does not exist as far as this
            # merchant is concerned, and saying "forbidden" would confirm the
            # reference names something real.
            raise Http404("No such request in this merchant's queue.")
        self.subject = request_obj

        payload = MerchantRequestDetailSerializer(
            request_obj, context=self.serializer_context()
        ).data
        context["req"] = payload
        context["thread_url"] = reverse(
            "merchant_panel:request_thread", args=[request_obj.public_ref]
        )

        # 3.1. Same gate as the Finance panel's, read from the same source of
        # truth. `request_obj` rather than the payload: the serializer produces
        # what a merchant may *see*, and whether a correction is allowed is a
        # fact about the row, not about the view of it.
        context["amount_form"] = (
            AmountCorrectionForm(prefix="amount")
            if self.request.user.has_perm("transactions.change_request_amount")
            and request_obj.status in AMOUNT_EDITABLE_STATUSES
            else None
        )
        # Paired rather than parallel: a template cannot look a form up by a
        # variable key, and each action renders its own inputs.
        context["action_forms"] = [
            {"move": move, "form": form_for(move["action"])(prefix=move["action"])}
            for move in payload["actions"]
        ]
        # The thread is two-way from step 9 on, and the composer is offered
        # whatever state the request is in: a merchant with a question after a
        # request closed still has a question.
        context["message_form"] = MerchantMessageForm(prefix="message")
        context["can_write"] = self.request.user.has_perm("transactions.add_message")
        return context


class MerchantQueueRowsView(MerchantQueueView):
    """``GET /merchant/queue/rows/`` — the queue's table body on its own.

    The poll swaps this in when the pulse says something moved (spec §8: the
    queue auto-refreshes every ten seconds). It subclasses the queue view rather
    than reimplementing it, so the rows a merchant sees live are the rows a
    reload would have given them — including the scoping that makes them theirs.
    """

    template_name = "merchant/_queue_rows.html"


class MerchantThreadView(MerchantPanelMixin, TemplateView):
    """``GET /merchant/requests/<ref>/thread/`` — the conversation on its own.

    Only the messages. The composer beside them is not in the fragment on
    purpose: replacing a textarea somebody is typing into, six times a minute,
    would be worse than not refreshing at all.

    Reading the thread live does **not** mark it seen. The badge is cleared by
    opening the screen, not by having left it open.
    """

    template_name = "merchant/_thread.html"
    nav_section = "requests"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request_obj = scoping.request_or_none(self.merchant, kwargs["reference"])
        if request_obj is None:
            raise Http404("No such request in this merchant's queue.")
        context["req"] = MerchantRequestDetailSerializer(
            request_obj, context=self.serializer_context()
        ).data
        return context


# ----------------------------------------------------------------- wallets --


class MerchantWalletListView(MerchantPanelMixin, TemplateView):
    """Read-only view of own wallets and their active status (spec §8)."""

    template_name = "merchant/wallets.html"
    nav_section = "wallets"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["wallets"] = MerchantWalletSerializer(
            scoping.wallets_for(self.merchant),
            many=True,
            context=self.serializer_context(),
        ).data
        return context


# ------------------------------------------------------------------ action --


class MerchantActionView(MerchantPanelMixin, View):
    """POST one lifecycle move (spec §8: confirm, mark paid, reject).

    The permission is not fixed on the class — it belongs to the transition and
    is read from the table, the same way the Finance panel reads it. Whether the
    request is actually this merchant's is checked twice: once by the queryset
    that found it, and once inside ``apply_transition`` against the locked row,
    because a reroute can happen between rendering the page and posting it.
    """

    http_method_names = ["post"]

    def post(self, request, reference, action):
        request_obj = scoping.request_or_none(self.merchant, reference)
        if request_obj is None:
            raise Http404("No such request in this merchant's queue.")

        try:
            move = get_transition(action)
        except TransitionError as exc:
            raise PermissionDenied(exc.message) from exc
        if move.actor_side != "merchant" and action != "reject":
            # Finance's moves exist in the shared table so the lifecycle is
            # complete; they are not reachable from this panel.
            raise PermissionDenied(_("هذا الإجراء يخص لوحة المالية."))

        detail_url = reverse("merchant_panel:request_detail", args=[reference])
        form = form_for(action)(
            request.POST, request.FILES, prefix=action, request_obj=request_obj
        )
        if not form.is_valid():
            for field_errors in form.errors.values():
                for error in field_errors:
                    messages.error(request, error)
            return redirect(detail_url)

        try:
            updated = actions.execute(
                request_obj, action, actor=request.user, form=form, http_request=request
            )
        except TransitionError as exc:
            messages.error(request, exc.message)
            return redirect(detail_url)

        messages.success(request, self._confirmation(updated.public_ref, action))
        return redirect(detail_url)

    @staticmethod
    def _confirmation(reference: str, action: str):
        """Say what changed and who now holds it, not just that it saved."""
        if action == "confirm":
            return _("أكّدت استلام %(ref)s. أُبلغت المالية.") % {"ref": reference}
        if action == "pay":
            return _("سُجّل تحويل %(ref)s مع الإثبات. أُبلغت المالية.") % {"ref": reference}
        if action == "reject":
            return _("رُفض %(ref)s ونُشر السبب في المحادثة.") % {"ref": reference}
        return _("حُدّث %(ref)s.") % {"ref": reference}


# ----------------------------------------------------------------- message --


class MerchantAmountCorrectionView(MerchantPanelMixin, View):
    """The merchant correcting a request to what actually arrived.

    Finance asked for this on both desks by name, and the merchant is the one
    who watches the money land. The rules — who may, when, and what the other
    three figures become — are
    :func:`apps.transactions.services.correct_amount`, the same function the
    Finance panel calls. Nothing about the arithmetic is decided here.
    """

    http_method_names = ["post"]

    def post(self, request, reference):
        req = scoping.request_or_none(self.merchant, reference)
        if req is None:
            raise Http404("No such request for this merchant.")

        detail_url = reverse("merchant_panel:request_detail", args=[reference])
        form = AmountCorrectionForm(request.POST, prefix="amount")

        if not form.is_valid():
            for field_errors in form.errors.values():
                for error in field_errors:
                    messages.error(request, error)
            return redirect(detail_url)

        try:
            updated = correct_amount(
                req,
                form.cleaned_data["amount_usd"],
                actor=request.user,
                reason=form.cleaned_data.get("reason", ""),
                http_request=request,
            )
        except TransitionError as exc:
            messages.error(request, exc.message)
            return redirect(detail_url)

        messages.success(
            request,
            _("صُحّح المبلغ إلى %(amount)s دولار.") % {"amount": f"{updated.amount_usd:.2f}"},
        )
        return redirect(detail_url)


class MerchantMessageView(MerchantPanelMixin, View):
    """POST a message into the thread (spec §9).

    Deliberately not an action on the transition table. A message is not a
    lifecycle move: it has no source status, no target status, and nothing about
    the request changes because one was sent. The merchant writes to the client
    whenever they want, whatever state the request is in.

    What the merchant may *read* back is still decided by the serializers —
    their own message returns through the same masked payload as everyone
    else's (spec §2).
    """

    http_method_names = ["post"]

    def post(self, request, reference):
        request_obj = scoping.request_or_none(self.merchant, reference)
        if request_obj is None:
            raise Http404("No such request in this merchant's queue.")
        if not request.user.has_perm("transactions.add_message"):
            raise PermissionDenied(_("لا تملك صلاحية المشاركة في المحادثة."))

        detail_url = reverse("merchant_panel:request_detail", args=[reference])
        form = MerchantMessageForm(request.POST, request.FILES, prefix="message")
        if not form.is_valid():
            for field_errors in form.errors.values():
                for error in field_errors:
                    messages.error(request, error)
            return redirect(detail_url)

        try:
            messaging.post(
                request_obj,
                sender_role=ActorRole.MERCHANT,
                sender_id=request.user.pk,
                body=form.cleaned_data["body"],
                upload=form.cleaned_data.get("attachment"),
            )
        except messaging.MessageError as exc:
            messages.error(request, exc.message)
            return redirect(detail_url)

        messages.success(request, _("أُرسلت رسالتك."))
        return redirect(detail_url)


# ------------------------------------------------------------- attachments --


class MerchantAttachmentView(MerchantPanelMixin, View):
    """Serve a proof file through a signed, short-lived URL (spec §11).

    Four things must hold, and the signature is only one: a live merchant
    session, the ``view_attachment`` permission, a token that has neither
    expired nor been minted for somebody else, and a file hanging off a request
    actually routed to this merchant.
    """

    http_method_names = ["get"]

    def get(self, request, pk, token):
        if not request.user.has_perm("transactions.view_attachment"):
            raise PermissionDenied(_("لا تملك صلاحية عرض المرفقات."))
        attachment_id = attachment_urls.unsign(token, request.user)
        if attachment_id != int(pk):
            # The path and the token disagree; neither is trusted over the other.
            raise PermissionDenied(_("رابط غير صالح."))
        attachment = scoping.attachment_or_none(self.merchant, attachment_id)
        if attachment is None:
            raise Http404("No such attachment in this merchant's queue.")
        return attachment_urls.serve(attachment)
