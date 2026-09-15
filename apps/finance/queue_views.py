"""The Finance request queue, routing and approval (build-order step 7).

Three surfaces, and one rule they share: **this is the identity-aware side of
the system.** Spec §9 gives Finance the full request detail including who the
client is, and every query here is written as if a merchant might one day reach
it by mistake — the client is only rendered behind
``accounts.view_client_identity``, and a Finance user who has had that
permission withdrawn sees the same masked label a merchant would.

Reads need a Finance role. Every *write* is a lifecycle move, and the permission
it needs comes from the transition table rather than from this module, so a
``finance_admin`` can hand any single step to a ``finance_staff`` account
without a code change (spec §3).
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, F, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView, ListView

from apps.core.choices import ActorRole
from apps.merchants import capacity
from apps.merchants.models import Wallet
from apps.portal import payloads as portal_payloads
from apps.transactions import messaging, reads
from apps.transactions.models import (
    Attachment,
    Message,
    Request,
    RequestStatus,
    RequestType,
)
from apps.transactions.services import (
    AMOUNT_EDITABLE_STATUSES,
    AWAITING_FINANCE,
    AWAITING_MERCHANT,
    OPEN_STATUSES,
    TransitionError,
    actor_role_of,
    apply_transition,
    available_transitions,
    correct_amount,
    get_transition,
    track,
)

from . import attachments as attachment_urls
from .mixins import FinancePanelMixin
from .queue_forms import (
    DEFAULT_STATUS,
    STATUS_GROUPS,
    AmountCorrectionForm,
    FinanceMessageForm,
    RequestFilterForm,
    form_for,
)
from .search import query as search_query

PERM_VIEW_IDENTITY = "accounts.view_client_identity"


class QueueAccessMixin(FinancePanelMixin):
    """Shared context for both queue screens."""

    nav_section = "requests"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_see_identity"] = self.request.user.has_perm(PERM_VIEW_IDENTITY)
        return context


#: 3.5 — the merchant's note, read without opening the request.
#:
#: Finance reconciles by running down the queue, and opening dozens of requests
#: to find the two that have a note on them is the habit this replaces. A
#: subquery rather than a prefetch: the row needs one string, not a thread, and
#: a prefetch would pull every message on every request on the page to find it.
LATEST_MERCHANT_NOTE = Subquery(
    Message.objects.filter(
        request=OuterRef("pk"),
        is_internal_note=True,
        sender_role=ActorRole.MERCHANT,
    )
    .order_by("-created_at", "-id")
    .values("body")[:1]
)


def _base_queryset():
    return Request.objects.select_related(
        "client", "payment_method", "merchant_selected", "merchant_assigned", "handled_by"
    ).annotate(merchant_note=LATEST_MERCHANT_NOTE)


# ------------------------------------------------------------------- queue --


class RequestQueueView(QueueAccessMixin, ListView):
    """The full queue with the filters spec §9 lists.

    It opens on everything still in flight rather than on the whole history:
    the queue is a worklist, and a worklist whose first page is last month's
    closed requests is one nobody works from.
    """

    template_name = "finance/request_list.html"
    context_object_name = "requests"
    paginate_by = 25

    def get_filter_form(self) -> RequestFilterForm:
        if not hasattr(self, "_filter_form"):
            data = self.request.GET.copy()
            if "status" not in self.request.GET:
                # Keyed on the parameter being absent, not on the query string
                # being empty: page 2 of the default queue must be the same
                # queue, and an explicitly empty status is how "everything,
                # closed included" is asked for.
                data["status"] = DEFAULT_STATUS
            form = RequestFilterForm(data)
            form.is_valid()  # populates cleaned_data; every field is optional
            self._filter_form = form
        return self._filter_form

    def get_queryset(self):
        form = self.get_filter_form()
        cleaned = getattr(form, "cleaned_data", {})
        queryset = _base_queryset()

        status = cleaned.get("status") or ""
        if status in STATUS_GROUPS:
            queryset = queryset.filter(status__in=STATUS_GROUPS[status])
        elif status in RequestStatus.values:
            queryset = queryset.filter(status=status)

        if cleaned.get("type"):
            queryset = queryset.filter(type=cleaned["type"])
        if cleaned.get("method"):
            queryset = queryset.filter(payment_method=cleaned["method"])
        if cleaned.get("merchant"):
            merchant = cleaned["merchant"]
            # "Requests involving this merchant" — the one the client chose and
            # the one Finance routed to are both meant, and they may differ.
            queryset = queryset.filter(
                Q(merchant_selected=merchant) | Q(merchant_assigned=merchant)
            )
        if cleaned.get("date_from"):
            queryset = queryset.filter(submitted_at__date__gte=cleaned["date_from"])
        if cleaned.get("date_to"):
            queryset = queryset.filter(submitted_at__date__lte=cleaned["date_to"])

        if cleaned.get("operator"):
            queryset = queryset.filter(handled_by=cleaned["operator"])

        # Resolution is what a daily reconciliation against B2CORE reads, and
        # it is not submission order: a request filed on Monday and settled on
        # Thursday belongs to Thursday's tally. Requests with no resolution yet
        # sort last either way rather than clumping at whichever end null
        # happens to fall on.
        sort = cleaned.get("sort") or ""
        if sort == "resolved_desc":
            queryset = queryset.order_by(F("closed_at").desc(nulls_last=True), "-id")
        elif sort == "resolved_asc":
            queryset = queryset.order_by(F("closed_at").asc(nulls_last=True), "id")

        search = (cleaned.get("q") or "").strip()
        if search:
            # Reference, amount, and — only for someone allowed to see client
            # identity — email and account number. What the term is matched
            # against is decided in apps.finance.search, so the rule is one
            # readable function rather than a lengthening chain of ORs here.
            queryset = queryset.filter(
                search_query(
                    search,
                    with_identity=self.request.user.has_perm(PERM_VIEW_IDENTITY),
                )
            )

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = self.get_filter_form()
        context["filter_form"] = form
        context["active_status"] = (getattr(form, "cleaned_data", {}) or {}).get("status")
        context["counts"] = queue_counts()
        # Spec §10's badge, per row: which of these has moved since this
        # operator last opened it. Computed over the page, not the queue —
        # the rail badge counts the queue and this marks what is on screen.
        page = context.get("page_obj")
        context["unread_refs"] = reads.unread_references(
            user=self.request.user,
            audience=reads.FINANCE,
            requests=page.object_list if page is not None else context["requests"],
        )
        context["rows_url"] = reverse("finance:request_rows")
        context["has_filters"] = any(
            self.request.GET.get(name) for name in ("q", "type", "method", "merchant", "date_from", "date_to")
        )
        # Without "page" — the pager appends its own, and a QueryDict keeps
        # the last value, so leaving it in would pin every link to page 1.
        params = self.request.GET.copy()
        params.pop("page", None)
        context["querystring"] = params.urlencode()
        return context


def queue_counts() -> dict:
    """How many requests sit in each bucket, in one query."""
    rows = Request.objects.values("status").annotate(n=Count("id"))
    by_status = {row["status"]: row["n"] for row in rows}
    total = sum(by_status.values())
    open_n = sum(by_status.get(s, 0) for s in OPEN_STATUSES)
    return {
        "awaiting_finance": sum(by_status.get(s, 0) for s in AWAITING_FINANCE),
        "awaiting_merchant": sum(by_status.get(s, 0) for s in AWAITING_MERCHANT),
        "open": open_n,
        "closed": total - open_n,
        "all": total,
        "by_status": by_status,
    }


# ------------------------------------------------------------------ detail --


class RequestDetailView(QueueAccessMixin, DetailView):
    """One request in full, with the moves this user may make on it."""

    model = Request
    template_name = "finance/request_detail.html"
    context_object_name = "req"
    slug_field = "public_ref"
    slug_url_kwarg = "ref"

    def get_queryset(self):
        return _base_queryset().prefetch_related("attachments", "messages__attachment")

    def get(self, request, *args, **kwargs):
        """Render the screen, and only then record that somebody saw it.

        The marking lives here rather than in ``get_context_data`` because
        :class:`apps.finance.live_views.FinanceThreadView` borrows that method
        to build the thread fragment — and it borrows it six times a minute.
        A side effect in a context builder is a side effect anyone reusing the
        builder inherits without meaning to, which is how a poll ends up
        clearing an unread badge for a tab nobody is looking at (spec §10).
        """
        response = super().get(request, *args, **kwargs)
        reads.mark_seen(user=request.user, request_obj=self.object)
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        req = self.object

        context["thread_url"] = reverse("finance:request_thread", args=[req.public_ref])

        # Paired rather than parallel: a template cannot look a form up by a
        # variable key, and every action renders its own inputs.
        context["action_forms"] = [
            {
                "move": move,
                # Resolved here rather than in the template: the wording depends
                # on the request's direction, and a template cannot call a
                # method with an argument.
                "confirm": move.confirm_for(req),
                "form": form_for(move.action)(request_obj=req, prefix=move.action),
            }
            for move in available_transitions(req, user)
        ]

        # 3.1. Offered only where the correction is actually allowed, so the
        # form is not a button that explains itself only after being pressed.
        context["amount_form"] = (
            AmountCorrectionForm(prefix="amount")
            if user.has_perm("transactions.change_request_amount")
            and req.status in AMOUNT_EDITABLE_STATUSES
            else None
        )

        context["track"] = track(req)
        context["attachments"] = [
            {
                "obj": attachment,
                "url": attachment_urls.url_for(attachment, user),
                "content_type": attachment_urls.content_type_of(attachment),
                "is_image": attachment_urls.content_type_of(attachment)
                in attachment_urls.INLINE_TYPES,
            }
            for attachment in req.attachments.all()
        ]
        # Spec §9: Finance has full read access to every thread — internal notes
        # included, since Finance is who writes them.
        # Paired rather than parallel: a message's file needs a signed URL, and
        # a template hunting for it in a second list is a nested loop nobody
        # should have to read.
        signed = {item["obj"].pk: item for item in context["attachments"]}
        context["thread"] = [
            {"message": message, "file": signed.get(message.attachment_id)}
            for message in messaging.visible_messages(req, audience=messaging.FINANCE)
        ]
        context["message_form"] = FinanceMessageForm(prefix="message")
        context["can_write"] = user.has_perm("transactions.add_message")
        # Who else is reading. An ordinary message on this thread is read by the
        # client *and* by whichever merchant holds the request, so the compose
        # box says so out loud — free text is the one place spec §2's masking
        # cannot reach, and a name typed here is a name a merchant reads.
        context["merchant_reading"] = (
            req.merchant_assigned.name if req.merchant_assigned_id else None
        )
        # The client's own breakdown, not a second copy of it. Subtracting the
        # commission here was right for a deposit and wrong for a withdrawal,
        # where the fee comes *off* the payout — and it had no idea about the
        # transfer rounding at all.
        context["converted_iqd"] = portal_payloads.converted_iqd(req)
        context["commission_sign"] = "−" if req.type == RequestType.WITHDRAWAL else "+"
        # Signs are decided here rather than in the template: a template that
        # branches on a negative renders the minus twice as readily as once,
        # and the figure it is branching on is money.
        rounding = portal_payloads.rounding_iqd(req)
        context["rounding_iqd"] = rounding
        context["rounding_sign"] = "+" if rounding >= 0 else "−"
        context["rounding_abs"] = abs(rounding)
        context["wallet_usage"] = self._wallet_usage(req)
        context["rerouted"] = bool(
            req.merchant_assigned_id
            and req.merchant_assigned_id != req.merchant_selected_id
        )
        return context

    def _wallet_usage(self, req):
        """Today's load on the wallet this deposit was paid into (spec §5).

        Matched by number, the same way the money was: a request snapshots the
        number it was shown, not a key to a row that may since have been edited.
        Nothing is shown when the wallet has been retired — its cap no longer
        governs anything.
        """
        if not req.is_deposit or not req.wallet_number_snapshot:
            return None
        wallet = (
            Wallet.objects.filter(
                merchant_method__merchant_id=req.merchant_selected_id,
                merchant_method__payment_method_id=req.payment_method_id,
                number=req.wallet_number_snapshot,
                is_active=True,
            )
            .select_related("merchant_method")
            .first()
        )
        if wallet is None or wallet.daily_cap is None:
            return None
        return capacity.usage(wallet)


# ------------------------------------------------------------------ action --


class AmountCorrectionView(FinancePanelMixin, View):
    """Correct a request to the amount that actually arrived (Finance review).

    Not a lifecycle move and deliberately not in the transition table: it
    changes what the request is worth, not where it is, and the two are
    different questions of the audit log. The rules live in
    :func:`apps.transactions.services.correct_amount`, which is also what the
    merchant panel calls — one implementation, so a figure corrected from one
    desk cannot be computed differently from the other.
    """

    http_method_names = ["post"]

    def post(self, request, ref):
        req = get_object_or_404(_base_queryset(), public_ref=ref)
        detail_url = reverse("finance:request_detail", args=[req.public_ref])
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


class RequestActionView(FinancePanelMixin, View):
    """POST one lifecycle move.

    The permission is not fixed on the class: it belongs to the transition, so
    it is read from the table. That keeps this view honest when the table grows
    a step, and keeps spec §3's delegation working without touching any view.
    """

    http_method_names = ["post"]

    def post(self, request, ref, action):
        req = get_object_or_404(_base_queryset(), public_ref=ref)
        try:
            move = get_transition(action)
        except TransitionError as exc:
            raise PermissionDenied(exc.message) from exc

        if move.actor_side != "finance":
            # Merchant moves exist in the table so the lifecycle is complete;
            # they are not reachable from the Finance panel.
            raise PermissionDenied(_("هذا الإجراء يخص لوحة التاجر."))
        if not request.user.has_perm(move.permission):
            raise PermissionDenied(_("لا تملك صلاحية هذا الإجراء."))

        form = form_for(action)(request.POST, prefix=action, request_obj=req)
        detail_url = reverse("finance:request_detail", args=[req.public_ref])

        if not form.is_valid():
            for field_errors in form.errors.values():
                for error in field_errors:
                    messages.error(request, error)
            return redirect(detail_url)

        try:
            updated = apply_transition(
                req, action, actor=request.user, http_request=request, **form.transition_kwargs()
            )
        except TransitionError as exc:
            messages.error(request, exc.message)
            return redirect(detail_url)

        messages.success(request, self._confirmation(updated, action))
        return redirect(detail_url)

    @staticmethod
    def _confirmation(req, action: str):
        """Say what changed for whom, not just that something was saved."""
        if action == "route":
            return _("أُسند %(ref)s إلى %(merchant)s. يظهر الآن في لوحة التاجر بلا بيانات العميل.") % {
                "ref": req.public_ref,
                "merchant": req.merchant_assigned.name,
            }
        if action == "reject":
            return _("رُفض %(ref)s ونُشر السبب في المحادثة.") % {"ref": req.public_ref}
        if action == "review" and req.is_withdrawal:
            # Spec §6 puts the client's B2CORE debit inside this step, and the
            # system cannot perform it. The flash is the last place to say so.
            return _("%(ref)s قيد المراجعة. تأكّد من خصم المبلغ من محفظة العميل في B2CORE.") % {
                "ref": req.public_ref
            }
        if action == "credit":
            return _("سُجّل %(ref)s كمُقيَّد في B2CORE. أغلقه عندما تكتمل المطابقة.") % {
                "ref": req.public_ref
            }
        if action == "close":
            return _("أُغلق %(ref)s.") % {"ref": req.public_ref}
        return _("حُدّثت حالة %(ref)s إلى «%(status)s».") % {
            "ref": req.public_ref,
            "status": req.get_status_display(),
        }


# ----------------------------------------------------------------- message --


class RequestMessageView(FinancePanelMixin, View):
    """POST a message into a thread (spec §9).

    Not a lifecycle move, so deliberately not on the transition table: a message
    has no source status and no target status, and nothing about the request
    changes because one was sent. Finance writes into any thread at any time,
    which is what "supervises all conversations" (spec §3) means in practice.

    The checkbox is the only thing separating two audiences. An ordinary message
    reaches the client and the merchant holding the request; an internal note
    stays on the desk, and every client- and merchant-facing serializer filters
    on that flag.
    """

    http_method_names = ["post"]

    def post(self, request, ref):
        req = get_object_or_404(_base_queryset(), public_ref=ref)
        if not request.user.has_perm("transactions.add_message"):
            raise PermissionDenied(_("لا تملك صلاحية الكتابة في المحادثة."))

        detail_url = reverse("finance:request_detail", args=[req.public_ref])
        form = FinanceMessageForm(request.POST, request.FILES, prefix="message")
        if not form.is_valid():
            for field_errors in form.errors.values():
                for error in field_errors:
                    messages.error(request, error)
            return redirect(detail_url)

        internal = form.cleaned_data["is_internal_note"]
        try:
            messaging.post(
                req,
                sender_role=actor_role_of(request.user),
                sender_id=request.user.pk,
                body=form.cleaned_data["body"],
                upload=form.cleaned_data.get("attachment"),
                is_internal_note=internal,
            )
        except messaging.MessageError as exc:
            messages.error(request, exc.message)
            return redirect(detail_url)

        # Say who just became able to read it, not merely that it saved. Which
        # of the two audiences a message went to is the thing worth confirming.
        if internal:
            messages.success(request, _("حُفظت الملاحظة الداخلية. لا يراها العميل ولا التاجر."))
        elif req.merchant_assigned_id:
            messages.success(
                request,
                _("أُرسلت الرسالة. يقرؤها العميل و%(merchant)s.")
                % {"merchant": req.merchant_assigned.name},
            )
        else:
            messages.success(request, _("أُرسلت الرسالة. يقرؤها العميل."))
        return redirect(detail_url)


# ------------------------------------------------------------- attachments --


class AttachmentView(FinancePanelMixin, View):
    """Serve a proof file to Finance through a signed, short-lived URL (spec §11).

    Four things must hold, and the signature is only one of them: a live Finance
    session, the ``view_attachment`` permission, a token that has not expired,
    and a token minted for this very user.
    """

    http_method_names = ["get"]

    def get(self, request, pk, token):
        if not request.user.has_perm("transactions.view_attachment"):
            raise PermissionDenied(_("لا تملك صلاحية عرض المرفقات."))
        attachment_id = attachment_urls.unsign(token, request.user)
        if attachment_id != int(pk):
            # The path and the token disagree; neither is trusted over the other.
            raise PermissionDenied(_("رابط غير صالح."))
        attachment = get_object_or_404(Attachment, pk=attachment_id)
        return attachment_urls.serve(attachment)
