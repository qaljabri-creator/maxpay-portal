"""Finance panel — merchant, method and wallet management (build-order step 3)
and exchange-rate management with history (step 4).

Every mutation writes an audit entry. Read access needs a Finance role; each
write needs a specific permission, so a ``finance_admin`` can delegate any of it
to ``finance_staff`` without a code change (spec §3).
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from apps.core import hours as business_hours
from apps.core.choices import AuditAction
from apps.core.models import SystemSettings
from apps.core.services import record_audit, snapshot
from apps.merchants import lifecycle
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.rates.models import ExchangeRate, RateType

from .forms import (
    BusinessHoursForm,
    ExchangeRateForm,
    MerchantForm,
    MerchantMethodForm,
    PaymentMethodForm,
    RateHistoryFilterForm,
    WalletForm,
)
from .mixins import (
    PERM_ADD_PAYMENT_METHOD,
    PERM_ADD_RATE,
    PERM_CHANGE_PAYMENT_METHOD,
    PERM_MANAGE_MERCHANTS,
    PERM_SET_HOURS,
    AuditedFormMixin,
    FinancePanelMixin,
    FinanceWriteMixin,
    ToastMixin,
)
from .queue_views import queue_counts

# ---------------------------------------------------------------- overview --


class DashboardView(FinancePanelMixin, TemplateView):
    """Landing page: the numbers currently governing new requests."""

    template_name = "finance/dashboard.html"
    nav_section = "overview"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["queue"] = queue_counts()
        context["deposit_rate"] = ExchangeRate.current(RateType.DEPOSIT)
        context["withdrawal_rate"] = ExchangeRate.current(RateType.WITHDRAWAL)
        context["merchant_count"] = Merchant.objects.filter(
            is_active=True, archived_at__isnull=True
        ).count()
        context["method_count"] = PaymentMethod.objects.filter(is_active=True).count()
        context["active_wallet_count"] = Wallet.objects.filter(is_active=True).count()
        # A merchant method with no active wallet cannot be offered to a client
        # for a deposit, so it is worth surfacing rather than leaving to chance.
        context["gaps"] = (
            MerchantMethod.objects.filter(is_active=True, merchant__is_active=True)
            .annotate(active_wallets=Count("wallets", filter=Q(wallets__is_active=True)))
            .filter(active_wallets=0)
            .select_related("merchant", "payment_method")
        )
        return context


# ---------------------------------------------------------------- merchants --


class MerchantListView(FinancePanelMixin, ListView):
    template_name = "finance/merchant_list.html"
    context_object_name = "merchants"
    nav_section = "merchants"
    paginate_by = 25

    def get_queryset(self):
        queryset = (
            Merchant.objects.select_related("user")
            .annotate(
                method_total=Count("methods", distinct=True),
                active_wallets=Count(
                    "methods__wallets",
                    filter=Q(methods__wallets__is_active=True),
                    distinct=True,
                ),
            )
            .order_by("-is_active", "name")
        )
        search = self.request.GET.get("q", "").strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(user__full_name__icontains=search)
            )
        status = self.request.GET.get("status", "")
        if status == "archived":
            # The one view an archived merchant appears in. Asked for by name
            # rather than mixed into the default: archiving means gone from the
            # working lists, and a list that quietly still contains them is the
            # thing archiving was supposed to fix. Reachable, though — an
            # operation with no way back and nothing to look at is not a
            # retirement, it is a deletion with extra steps.
            return queryset.filter(archived_at__isnull=False)

        queryset = queryset.filter(archived_at__isnull=True)
        if status == "active":
            queryset = queryset.filter(is_active=True)
        elif status == "inactive":
            queryset = queryset.filter(is_active=False)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search"] = self.request.GET.get("q", "")
        context["status"] = self.request.GET.get("status", "")
        context["archived_count"] = Merchant.objects.filter(
            archived_at__isnull=False
        ).count()
        context["can_archive"] = self.request.user.has_perm(lifecycle.PERM_ARCHIVE)
        return context


class MerchantDetailView(FinancePanelMixin, DetailView):
    """One merchant, their methods, and every wallet under each method."""

    model = Merchant
    template_name = "finance/merchant_detail.html"
    context_object_name = "merchant"
    nav_section = "merchants"

    def get_queryset(self):
        return Merchant.objects.select_related("user").prefetch_related(
            Prefetch(
                "methods",
                queryset=MerchantMethod.objects.select_related("payment_method").prefetch_related(
                    # Archived wallets are gone from here too. The audit log
                    # keeps them; this page is a worklist.
                    Prefetch(
                        "wallets",
                        queryset=Wallet.objects.filter(archived_at__isnull=True).order_by(
                            "-is_active", "-created_at"
                        ),
                    )
                ),
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["unassigned_methods"] = PaymentMethod.objects.filter(is_active=True).exclude(
            pk__in=self.object.methods.values_list("payment_method_id", flat=True)
        )
        context["can_archive"] = self.request.user.has_perm(lifecycle.PERM_ARCHIVE)
        return context


class MerchantCreateView(FinanceWriteMixin, AuditedFormMixin, ToastMixin, CreateView):
    model = Merchant
    form_class = MerchantForm
    template_name = "finance/merchant_form.html"
    permission_required = PERM_MANAGE_MERCHANTS
    nav_section = "merchants"
    audit_action = AuditAction.MERCHANT_CHANGE
    success_message = _("أُنشئ التاجر. أضف طرق الدفع ثم محفظة نشطة لكل طريقة.")

    def get_success_url(self):
        return reverse("finance:merchant_detail", args=[self.object.pk])


class MerchantUpdateView(FinanceWriteMixin, AuditedFormMixin, ToastMixin, UpdateView):
    model = Merchant
    form_class = MerchantForm
    template_name = "finance/merchant_form.html"
    permission_required = PERM_MANAGE_MERCHANTS
    nav_section = "merchants"
    audit_action = AuditAction.MERCHANT_CHANGE
    success_message = _("حُفظت بيانات التاجر.")

    def get_success_url(self):
        return reverse("finance:merchant_detail", args=[self.object.pk])


class MerchantToggleView(FinanceWriteMixin, View):
    """Activate or deactivate a merchant. POST only — it changes state."""

    permission_required = PERM_MANAGE_MERCHANTS

    def post(self, request, pk):
        merchant = get_object_or_404(Merchant, pk=pk)
        if merchant.is_archived:
            # Reactivating from here would put an archived merchant back in
            # front of clients while still being invisible in every list —
            # active and unfindable at the same time.
            messages.info(
                request,
                _("هذا التاجر مؤرشف. أعِده من الأرشيف أولًا ثم فعّله."),
            )
            return redirect("finance:merchant_detail", pk=pk)
        before = snapshot(merchant, ["name", "is_active"])
        merchant.is_active = not merchant.is_active
        merchant.save(update_fields=["is_active", "updated_at"])

        record_audit(
            action=AuditAction.MERCHANT_CHANGE,
            target=merchant,
            request=request,
            before=before,
            after=snapshot(merchant, ["name", "is_active"]),
        )
        messages.success(
            request,
            _("فُعّل التاجر «%(name)s».") % {"name": merchant.name}
            if merchant.is_active
            else _("أُلغي تفعيل التاجر «%(name)s». لن يظهر للعملاء في الطلبات الجديدة.")
            % {"name": merchant.name},
        )
        return redirect(request.POST.get("next") or reverse("finance:merchant_detail", args=[pk]))


class MerchantArchiveView(FinanceWriteMixin, View):
    """Retire a merchant, or bring one back. POST only — it changes state.

    Behind ``merchants.archive_merchants`` rather than ``manage_merchants``:
    the latter is delegable to a ``finance_staff`` account so it can add a
    wallet, and adding a wallet is not the same size of act as retiring the
    merchant it belongs to (spec §3).
    """

    permission_required = lifecycle.PERM_ARCHIVE

    def post(self, request, pk):
        merchant = get_object_or_404(Merchant, pk=pk)
        restoring = request.POST.get("restore") == "1"
        try:
            if restoring:
                lifecycle.restore_merchant(merchant, actor=request.user, http_request=request)
                messages.success(
                    request,
                    _(
                        "أُعيد التاجر «%(name)s» من الأرشيف، وهو موقوف حتى تفعّله. "
                        "الإعادة والتفعيل قراران منفصلان."
                    ) % {"name": merchant.name},
                )
            else:
                lifecycle.archive_merchant(merchant, actor=request.user, http_request=request)
                messages.warning(
                    request,
                    _(
                        "أُرشف التاجر «%(name)s». اختفى من كل القوائم ومن اختيار العميل "
                        "ولن يُسند إليه طلب جديد. طلباته القائمة باقية كما هي، وسجله محفوظ."
                    ) % {"name": merchant.name},
                )
        except lifecycle.LifecycleError as exc:
            messages.info(request, exc.message)

        if restoring:
            return redirect("finance:merchant_detail", pk=pk)
        return redirect(request.POST.get("next") or reverse("finance:merchant_list"))


# --------------------------------------------------------- merchant methods --


class MerchantMethodCreateView(FinanceWriteMixin, AuditedFormMixin, ToastMixin, CreateView):
    model = MerchantMethod
    form_class = MerchantMethodForm
    template_name = "finance/merchant_method_form.html"
    permission_required = PERM_MANAGE_MERCHANTS
    nav_section = "merchants"
    audit_action = AuditAction.MERCHANT_CHANGE
    success_message = _("أُسندت الطريقة. أضف محفظة نشطة حتى تُعرض على العملاء.")

    @property
    def merchant(self):
        return get_object_or_404(Merchant, pk=self.kwargs["merchant_pk"])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["merchant"] = self.merchant
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["merchant"] = self.merchant
        return context

    def get_success_url(self):
        return reverse("finance:merchant_detail", args=[self.object.merchant_id])


class MerchantMethodToggleView(FinanceWriteMixin, View):
    permission_required = PERM_MANAGE_MERCHANTS

    def post(self, request, pk):
        method = get_object_or_404(
            MerchantMethod.objects.select_related("merchant", "payment_method"), pk=pk
        )
        before = snapshot(method, ["merchant", "payment_method", "is_active"])
        method.is_active = not method.is_active
        method.save(update_fields=["is_active", "updated_at"])

        record_audit(
            action=AuditAction.MERCHANT_CHANGE,
            target=method,
            request=request,
            before=before,
            after=snapshot(method, ["merchant", "payment_method", "is_active"]),
        )
        messages.success(
            request,
            _("حُدّثت حالة طريقة «%(m)s».") % {"m": method.payment_method},
        )
        return redirect("finance:merchant_detail", pk=method.merchant_id)


# ------------------------------------------------------------------ wallets --


class WalletCreateView(FinanceWriteMixin, AuditedFormMixin, CreateView):
    model = Wallet
    form_class = WalletForm
    template_name = "finance/wallet_form.html"
    permission_required = PERM_MANAGE_MERCHANTS
    nav_section = "merchants"
    audit_action = AuditAction.WALLET_CHANGE

    @property
    def merchant_method(self):
        return get_object_or_404(
            MerchantMethod.objects.select_related("merchant", "payment_method"),
            pk=self.kwargs["method_pk"],
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["merchant_method"] = self.merchant_method
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["merchant_method"] = self.merchant_method
        context["merchant"] = self.merchant_method.merchant
        return context

    def form_valid(self, form):
        superseded = form.current_active if form.cleaned_data.get("is_active") else None
        response = super().form_valid(form)
        if superseded:
            # The model stood it down; say so, because it changes what clients
            # are shown from this moment on.
            record_audit(
                action=AuditAction.WALLET_CHANGE,
                target=superseded,
                request=self.request,
                before={"is_active": True},
                after={"is_active": False, "reason": "superseded_by", "wallet": self.object.pk},
            )
            messages.warning(
                self.request,
                _("أُلغي تفعيل المحفظة السابقة %(old)s تلقائيًا.") % {"old": superseded.number},
            )
        messages.success(self.request, _("أُضيفت المحفظة %(n)s.") % {"n": self.object.number})
        return response

    def get_success_url(self):
        return reverse("finance:merchant_detail", args=[self.merchant_method.merchant_id])


class WalletUpdateView(FinanceWriteMixin, AuditedFormMixin, ToastMixin, UpdateView):
    model = Wallet
    form_class = WalletForm
    template_name = "finance/wallet_form.html"
    permission_required = PERM_MANAGE_MERCHANTS
    nav_section = "merchants"
    audit_action = AuditAction.WALLET_CHANGE
    success_message = _("حُفظت المحفظة.")

    def get_queryset(self):
        return Wallet.objects.select_related("merchant_method__merchant", "merchant_method__payment_method")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["merchant_method"] = self.object.merchant_method
        context["merchant"] = self.object.merchant_method.merchant
        return context

    def get_success_url(self):
        return reverse("finance:merchant_detail", args=[self.object.merchant_method.merchant_id])


class WalletActivateView(FinanceWriteMixin, View):
    """Make this the wallet clients are shown, standing down the current one."""

    permission_required = PERM_MANAGE_MERCHANTS

    def post(self, request, pk):
        wallet = get_object_or_404(Wallet.objects.select_related("merchant_method"), pk=pk)
        previous = wallet.merchant_method.active_wallet

        if previous and previous.pk == wallet.pk:
            messages.info(request, _("هذه المحفظة نشطة بالفعل."))
            return redirect("finance:merchant_detail", pk=wallet.merchant_method.merchant_id)

        wallet.is_active = True
        wallet.save()  # stands down the previous holder (spec §5)

        record_audit(
            action=AuditAction.WALLET_CHANGE,
            target=wallet,
            request=request,
            before={"is_active": False},
            after={"is_active": True, "replaced": previous.number if previous else None},
        )
        if previous:
            record_audit(
                action=AuditAction.WALLET_CHANGE,
                target=previous,
                request=request,
                before={"is_active": True},
                after={"is_active": False, "reason": "superseded_by", "wallet": wallet.pk},
            )
            messages.warning(
                request, _("أُلغي تفعيل %(old)s.") % {"old": previous.number}
            )
        messages.success(request, _("صارت %(n)s المحفظة النشطة.") % {"n": wallet.number})
        return redirect("finance:merchant_detail", pk=wallet.merchant_method.merchant_id)


class WalletDeactivateView(FinanceWriteMixin, View):
    permission_required = PERM_MANAGE_MERCHANTS

    def post(self, request, pk):
        wallet = get_object_or_404(Wallet.objects.select_related("merchant_method"), pk=pk)
        if not wallet.is_active:
            messages.info(request, _("هذه المحفظة غير نشطة أصلًا."))
            return redirect("finance:merchant_detail", pk=wallet.merchant_method.merchant_id)

        wallet.deactivate()
        record_audit(
            action=AuditAction.WALLET_CHANGE,
            target=wallet,
            request=request,
            before={"is_active": True},
            after={"is_active": False, "reason": "manual"},
        )
        messages.warning(
            request,
            _("أُلغي تفعيل %(n)s. لم تعد هذه الطريقة قابلة للإيداع لدى هذا التاجر حتى تُفعّل محفظة أخرى.")
            % {"n": wallet.number},
        )
        return redirect("finance:merchant_detail", pk=wallet.merchant_method.merchant_id)


class WalletRemoveView(FinanceWriteMixin, View):
    """Get rid of a wallet: deleted if it was never used, archived if it was.

    Which of the two happened is decided in
    :func:`apps.merchants.lifecycle.remove_wallet` and *reported*, because the
    difference matters to whoever pressed the button. A wallet that vanishes
    and a wallet that goes quiet look identical from here otherwise, and the
    operator would have no way to tell whether the history they may need later
    still exists.
    """

    permission_required = lifecycle.PERM_ARCHIVE

    def post(self, request, pk):
        wallet = get_object_or_404(
            Wallet.objects.select_related("merchant_method__merchant"), pk=pk
        )
        merchant_id = wallet.merchant_method.merchant_id
        try:
            removal = lifecycle.remove_wallet(
                wallet, actor=request.user, http_request=request
            )
        except lifecycle.LifecycleError as exc:
            messages.info(request, exc.message)
            return redirect("finance:merchant_detail", pk=merchant_id)

        name = removal.number or removal.label or _("المحفظة")
        if removal.deleted:
            messages.success(
                request,
                _("حُذفت المحفظة %(n)s نهائيًا. لم يُقدَّم عليها أي طلب، فلا سجل يرتبط بها.")
                % {"n": name},
            )
        else:
            messages.warning(
                request,
                _(
                    "أُرشفت المحفظة %(n)s ولم تُحذف: قُدِّمت عليها طلبات، وحذفها يقطع "
                    "مرجعًا يحتاجه سجل التدقيق. اختفت من كل مكان، والطلبات القائمة "
                    "تحتفظ برقمها كما عُرض."
                ) % {"n": name},
            )
        return redirect("finance:merchant_detail", pk=removal.merchant_id)


# ---------------------------------------------------------- payment methods --


class PaymentMethodListView(FinancePanelMixin, ListView):
    model = PaymentMethod
    template_name = "finance/payment_method_list.html"
    context_object_name = "payment_methods"
    nav_section = "payment_methods"

    def get_queryset(self):
        return PaymentMethod.objects.annotate(
            merchant_total=Count("merchant_methods", distinct=True)
        ).order_by("sort_order", "caption_ar")


class PaymentMethodCreateView(FinanceWriteMixin, AuditedFormMixin, ToastMixin, CreateView):
    model = PaymentMethod
    form_class = PaymentMethodForm
    template_name = "finance/payment_method_form.html"
    permission_required = PERM_ADD_PAYMENT_METHOD
    nav_section = "payment_methods"
    audit_action = AuditAction.MERCHANT_CHANGE
    success_url = reverse_lazy("finance:payment_method_list")
    success_message = _("أُضيفت طريقة الدفع.")


class PaymentMethodUpdateView(FinanceWriteMixin, AuditedFormMixin, ToastMixin, UpdateView):
    model = PaymentMethod
    form_class = PaymentMethodForm
    template_name = "finance/payment_method_form.html"
    permission_required = PERM_CHANGE_PAYMENT_METHOD
    nav_section = "payment_methods"
    audit_action = AuditAction.MERCHANT_CHANGE
    success_url = reverse_lazy("finance:payment_method_list")
    success_message = _("حُفظت طريقة الدفع.")


class PaymentMethodToggleView(FinanceWriteMixin, View):
    permission_required = PERM_CHANGE_PAYMENT_METHOD

    def post(self, request, pk):
        method = get_object_or_404(PaymentMethod, pk=pk)
        before = snapshot(method, ["code", "is_active"])
        method.is_active = not method.is_active
        method.save(update_fields=["is_active", "updated_at"])

        record_audit(
            action=AuditAction.MERCHANT_CHANGE,
            target=method,
            request=request,
            before=before,
            after=snapshot(method, ["code", "is_active"]),
        )
        messages.success(request, _("حُدّثت حالة «%(m)s».") % {"m": method})
        return redirect("finance:payment_method_list")


# -------------------------------------------------------------------- rates --


class RateListView(FinancePanelMixin, ListView):
    """Current rates plus the full immutable history (spec §5, §9)."""

    template_name = "finance/rate_list.html"
    context_object_name = "history"
    nav_section = "rates"
    paginate_by = 40

    def get_queryset(self):
        queryset = ExchangeRate.objects.select_related("set_by").order_by(
            "-effective_from", "-id"
        )
        rate_type = self.request.GET.get("rate_type", "")
        if rate_type in RateType.values:
            queryset = queryset.filter(rate_type=rate_type)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter_form"] = RateHistoryFilterForm(self.request.GET or None)
        context["rate_type"] = self.request.GET.get("rate_type", "")
        context["deposit_rate"] = ExchangeRate.current(RateType.DEPOSIT)
        context["withdrawal_rate"] = ExchangeRate.current(RateType.WITHDRAWAL)
        context["now"] = timezone.now()
        # Marking the row that is actually in force is the point of the page.
        context["current_ids"] = {
            rate.pk
            for rate in (context["deposit_rate"], context["withdrawal_rate"])
            if rate is not None
        }
        return context


class RateCreateView(FinanceWriteMixin, CreateView):
    """Set a new rate. Always an insert — never an edit (spec §5)."""

    model = ExchangeRate
    form_class = ExchangeRateForm
    template_name = "finance/rate_form.html"
    permission_required = PERM_ADD_RATE
    nav_section = "rates"
    success_url = reverse_lazy("finance:rate_list")

    def get_initial(self):
        initial = super().get_initial()
        requested = self.request.GET.get("rate_type")
        if requested in RateType.values:
            initial["rate_type"] = requested
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["deposit_rate"] = ExchangeRate.current(RateType.DEPOSIT)
        context["withdrawal_rate"] = ExchangeRate.current(RateType.WITHDRAWAL)
        return context

    def form_valid(self, form):
        superseded = ExchangeRate.current(form.cleaned_data["rate_type"])
        form.instance.set_by = self.request.user
        response = super().form_valid(form)

        record_audit(
            action=AuditAction.RATE_CHANGE,
            target=self.object,
            request=self.request,
            before=snapshot(superseded) if superseded else None,
            after=snapshot(self.object),
        )
        if self.object.effective_from > timezone.now():
            messages.success(
                self.request,
                _("سُجّل السعر الجديد وسيسري في %(when)s.")
                % {"when": timezone.localtime(self.object.effective_from).strftime("%Y-%m-%d %H:%M")},
            )
        else:
            messages.success(
                self.request,
                _("سرى السعر الجديد. الطلبات القائمة تحتفظ بلقطة سعرها دون تغيير."),
            )
        return response


# ----------------------------------------------------------- business hours --


class BusinessHoursView(FinancePanelMixin, AuditedFormMixin, ToastMixin, UpdateView):
    """Configure when the portal accepts submissions (spec §7, §9) — step 11.

    Readable by anyone on the Finance desk, writable only with
    ``core.change_systemsettings`` — which by default is a ``finance_admin``
    permission, and which a ``finance_admin`` can hand to a ``finance_staff``
    account without a code change (spec §3). A reader gets the same page with
    the settings rendered as text instead of as a form, rather than a 403: the
    hours govern whether the queue fills up at all, and knowing the desk is shut
    is part of working it.

    The screen deliberately carries no JavaScript. The countdown spec §7 asks
    for belongs on the client's closed notice; here the next change is a
    timestamp, and the panel stays script-free.
    """

    model = SystemSettings
    form_class = BusinessHoursForm
    template_name = "finance/business_hours.html"
    nav_section = "hours"
    audit_action = AuditAction.SETTINGS_CHANGE
    audit_fields = [
        "open_time",
        "close_time",
        "timezone",
        "is_open_override",
        "closed_message_ar",
    ]
    success_url = reverse_lazy("finance:business_hours")
    success_message = _("حُفظت مواعيد العمل.")

    def get_object(self, queryset=None):
        return SystemSettings.load()

    def post(self, request, *args, **kwargs):
        if not request.user.has_perm(PERM_SET_HOURS):
            raise PermissionDenied(_("تغيير مواعيد العمل يحتاج صلاحية إضافية."))
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Re-read rather than reusing ``self.object``: a rejected POST leaves
        # the instance carrying the values that were refused, and the panel
        # must report the door the portal is actually enforcing, not the one
        # someone just failed to configure.
        saved = SystemSettings.load()
        hours = business_hours.evaluate(saved)
        context["hours"] = hours
        context["changes_at_local"] = hours.local(hours.changes_at)
        context["always_open"] = hours.reason == business_hours.REASON_ALWAYS_OPEN
        context["overnight"] = saved.open_time > saved.close_time
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        # Saying only "saved" would leave the operator to work out what the
        # change means; the one thing they came here to set is whether the
        # portal is taking submissions right now.
        hours = business_hours.evaluate(self.object)
        if hours.is_open:
            messages.info(self.request, _("البوابة تستقبل الطلبات الآن."))
        else:
            messages.warning(
                self.request,
                _("البوابة لا تستقبل الطلبات الآن. يرى العملاء شاشة الإغلاق."),
            )
        return response
