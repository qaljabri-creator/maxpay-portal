"""The roles and users panel (spec §3, §9) — build-order step 15.

Spec §9 gives ``finance_admin`` "user and role management"; until this step the
only way to exercise it was a shell on the production host. These are the
screens, and they are deliberately thin: every operation is a call into
:mod:`apps.accounts.provisioning`, which owns the rules and writes the audit
entry in the same transaction as the change.

**Three permissions, not one.** Seeing the panel, administering an account, and
clearing somebody's second factor are different powers with different blast
radii, so they are separate gates and a ``finance_admin`` can hand out any of
them individually (spec §3).

**A generated password is shown exactly once.** It is put in the session by the
view that created it and popped by the view that renders it, so it survives the
redirect after the POST and nothing else — not a refresh, not the back button,
and not the template of any other screen.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView, FormView, ListView

from apps.accounts import provisioning
from apps.accounts.models import Role, User
from apps.accounts.provisioning import ProvisioningError

from .mixins import FinancePanelMixin
from .user_forms import (
    InternalUserCreateForm,
    InternalUserUpdateForm,
    MerchantLinkForm,
    PermissionOverrideForm,
    UserFilterForm,
)

#: Spec §3: the root role creates all other users and assigns their roles.
PERM_MANAGE_USERS = "accounts.manage_internal_users"
#: Granting and revoking individual permissions, which is a step further than
#: creating an account with a role's baseline.
PERM_MANAGE_PERMISSIONS = "accounts.manage_permissions"
#: Clearing a second factor. Separated because it is the one action that
#: temporarily lowers another account's defences (spec §11).
PERM_RESET_TWO_FACTOR = "accounts.reset_user_two_factor"

#: Where a one-time password waits out the redirect that follows its POST.
SECRET_SESSION_KEY = "maxpay_issued_password"


class UserPanelMixin(FinancePanelMixin):
    """Finance panel access, plus the permission this section is behind."""

    nav_section = "users"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["can_manage_users"] = user.has_perm(PERM_MANAGE_USERS)
        context["can_manage_permissions"] = user.has_perm(PERM_MANAGE_PERMISSIONS)
        context["can_reset_two_factor"] = user.has_perm(PERM_RESET_TWO_FACTOR)
        return context


class RequirePermissionMixin:
    """Refuse before anything is read, not after it is rendered."""

    required_permission = PERM_MANAGE_USERS

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.has_perm(
            self.required_permission
        ):
            raise PermissionDenied(_("لا تملك صلاحية إدارة المستخدمين."))
        return super().dispatch(request, *args, **kwargs)


def issue_secret(request, user: User, password: str) -> None:
    """Stash a one-time password for the next render, and only the next one."""
    request.session[SECRET_SESSION_KEY] = {"user": user.pk, "password": password}


def take_secret(request, user: User) -> str:
    """Pop the password if it belongs to this account, otherwise nothing."""
    stashed = request.session.pop(SECRET_SESSION_KEY, None)
    if not stashed or stashed.get("user") != user.pk:
        return ""
    return stashed.get("password", "")


# ------------------------------------------------------------------- list --


class UserListView(RequirePermissionMixin, UserPanelMixin, ListView):
    template_name = "finance/user_list.html"
    context_object_name = "users"
    paginate_by = 25

    def get_queryset(self):
        self.filter_form = UserFilterForm(self.request.GET or None)
        queryset = User.objects.select_related("merchant_profile").order_by(
            "-is_active", "full_name"
        )
        if not self.filter_form.is_valid():
            return queryset

        data = self.filter_form.cleaned_data
        if data.get("q"):
            queryset = queryset.filter(
                Q(full_name__icontains=data["q"]) | Q(email__icontains=data["q"])
            )
        if data.get("role"):
            queryset = queryset.filter(role=data["role"])
        if data.get("status") == "active":
            queryset = queryset.filter(is_active=True)
        elif data.get("status") == "inactive":
            queryset = queryset.filter(is_active=False)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter_form"] = self.filter_form
        context["has_filters"] = any(self.request.GET.get(k) for k in ("q", "role", "status"))
        context["role_counts"] = {
            role: User.objects.filter(role=role, is_active=True).count()
            for role, _label in Role.choices
        }
        return context


# ----------------------------------------------------------------- create --


class UserCreateView(RequirePermissionMixin, UserPanelMixin, FormView):
    template_name = "finance/user_form.html"
    form_class = InternalUserCreateForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_create"] = True
        return context

    def form_valid(self, form):
        try:
            user, password = provisioning.create_account(
                email=form.cleaned_data["email"],
                full_name=form.cleaned_data["full_name"],
                phone=form.cleaned_data.get("phone", ""),
                role=form.cleaned_data["role"],
                actor=self.request.user,
                http_request=self.request,
            )
            merchant = form.cleaned_data.get("merchant")
            if merchant is not None:
                provisioning.link_merchant(
                    user, merchant, actor=self.request.user, http_request=self.request
                )
        except ProvisioningError as exc:
            form.add_error(None, exc.message)
            return self.form_invalid(form)

        issue_secret(self.request, user, password)
        messages.success(
            self.request,
            _("أُنشئ الحساب. انسخ كلمة المرور المؤقتة الآن؛ لن تُعرض مرة أخرى."),
        )
        return redirect("finance:user_detail", pk=user.pk)


# ----------------------------------------------------------------- detail --


class UserDetailView(RequirePermissionMixin, UserPanelMixin, DetailView):
    template_name = "finance/user_detail.html"
    context_object_name = "account"
    queryset = User.objects.select_related("merchant_profile", "created_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        account = self.object
        context["issued_password"] = take_secret(self.request, account)
        context["is_self"] = account.pk == self.request.user.pk
        context["has_two_factor"] = account.has_verified_two_factor
        context["merchant_form"] = MerchantLinkForm(user=account)
        context["effective_count"] = len(provisioning.effective_permissions(account))
        context["granted_extra"] = sorted(
            f"{p.content_type.app_label}.{p.codename}"
            for p in account.user_permissions.select_related("content_type")
        )
        context["denied"] = sorted(account.denied_permission_labels())
        return context


class UserUpdateView(RequirePermissionMixin, UserPanelMixin, FormView):
    template_name = "finance/user_form.html"
    form_class = InternalUserUpdateForm

    def get_account(self) -> User:
        return get_object_or_404(User, pk=self.kwargs["pk"])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.get_account()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["account"] = self.get_account()
        context["is_create"] = False
        return context

    def form_valid(self, form):
        account = self.get_account()
        try:
            provisioning.update_account(
                account,
                full_name=form.cleaned_data["full_name"],
                phone=form.cleaned_data["phone"],
                role=form.cleaned_data["role"],
                actor=self.request.user,
                http_request=self.request,
            )
        except ProvisioningError as exc:
            form.add_error(None, exc.message)
            return self.form_invalid(form)

        messages.success(self.request, _("حُدّثت بيانات الحساب."))
        return redirect("finance:user_detail", pk=account.pk)


# ------------------------------------------------------------ permissions --


class UserPermissionsView(RequirePermissionMixin, UserPanelMixin, FormView):
    template_name = "finance/user_permissions.html"
    form_class = PermissionOverrideForm
    required_permission = PERM_MANAGE_PERMISSIONS

    def get_account(self) -> User:
        return get_object_or_404(User, pk=self.kwargs["pk"])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.get_account()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["account"] = self.get_account()
        return context

    def form_valid(self, form):
        account = self.get_account()
        granted, denied = form.overrides()
        try:
            provisioning.set_permission_overrides(
                account,
                granted=granted,
                denied=denied,
                actor=self.request.user,
                http_request=self.request,
            )
        except ProvisioningError as exc:
            form.add_error(None, exc.message)
            return self.form_invalid(form)

        messages.success(self.request, _("حُفظت الصلاحيات."))
        return redirect("finance:user_detail", pk=account.pk)


# --------------------------------------------------------------- actions --


class UserActionView(RequirePermissionMixin, FinancePanelMixin, View):
    """One POST per lever. Each is its own URL so each is its own audit entry."""

    def post(self, request, pk, action):
        account = get_object_or_404(User, pk=pk)
        handler = getattr(self, f"do_{action}", None)
        if handler is None:
            raise PermissionDenied(_("إجراء غير معروف."))
        try:
            handler(request, account)
        except ProvisioningError as exc:
            messages.error(request, exc.message)
        return redirect("finance:user_detail", pk=account.pk)

    # -- levers ------------------------------------------------------------

    def do_reset_password(self, request, account: User):
        password = provisioning.reset_password(
            account, actor=request.user, http_request=request
        )
        issue_secret(request, account, password)
        messages.success(
            request,
            _("أُصدرت كلمة مرور مؤقتة. انسخها الآن؛ لن تُعرض مرة أخرى."),
        )

    def do_reset_two_factor(self, request, account: User):
        if not request.user.has_perm(PERM_RESET_TWO_FACTOR):
            raise PermissionDenied(_("لا تملك صلاحية إعادة تعيين المصادقة الثنائية."))
        removed = provisioning.reset_two_factor(
            account, actor=request.user, http_request=request
        )
        messages.success(
            request,
            _("حُذفت %(n)s من أجهزة المصادقة. سيُطلب من المستخدم التسجيل من جديد عند أول دخول.")
            % {"n": removed},
        )

    def do_disable(self, request, account: User):
        provisioning.set_active(account, False, actor=request.user, http_request=request)
        messages.success(request, _("عُطّل الحساب."))

    def do_enable(self, request, account: User):
        provisioning.set_active(account, True, actor=request.user, http_request=request)
        messages.success(request, _("فُعّل الحساب."))

    def do_link_merchant(self, request, account: User):
        form = MerchantLinkForm(request.POST, user=account)
        if not form.is_valid():
            raise ProvisioningError(_("اختيار غير صالح."))
        merchant = form.cleaned_data.get("merchant")
        provisioning.link_merchant(
            account,
            merchant if merchant else None,
            actor=request.user,
            http_request=request,
        )
        messages.success(
            request,
            _("رُبط الحساب بسجل التاجر.") if merchant else _("أُلغي ربط سجل التاجر."),
        )
