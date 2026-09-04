"""Access control and audit plumbing shared by every Finance panel view.

Two separate gates:

* :class:`FinancePanelMixin` decides who may *see* the panel at all — the two
  Finance roles. A merchant must never land here even though nothing in steps 3
  and 4 exposes client identity: the panel is Finance's surface, and merchants
  get their own in build-order step 8.
* Write access is gated on *permissions*, not on the role, using Django's
  ``PermissionRequiredMixin``. Spec §3 says a ``finance_admin`` grants and
  revokes staff permissions, so a permission a ``finance_admin`` holds by
  default can be delegated to a ``finance_staff`` account without a code change.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import AccessMixin, PermissionRequiredMixin
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import IDENTITY_AWARE_ROLES
from apps.core import hours as business_hours
from apps.core.services import record_audit, snapshot

#: Write permissions, named once so views and templates agree.
PERM_MANAGE_MERCHANTS = "merchants.manage_merchants"
PERM_ADD_PAYMENT_METHOD = "merchants.add_paymentmethod"
PERM_CHANGE_PAYMENT_METHOD = "merchants.change_paymentmethod"
PERM_ADD_RATE = "rates.add_exchangerate"
#: Business hours live on the ``SystemSettings`` singleton (spec §5, §9).
PERM_SET_HOURS = "core.change_systemsettings"
#: Spec §9 gives Finance an audit-log viewer; spec §2 forbids it to a merchant,
#: which :data:`apps.accounts.permissions.MERCHANT_FORBIDDEN` enforces.
PERM_VIEW_AUDIT = "accounts.view_audit_log"
#: Spec §3, §9: user and role management, `finance_admin` only by default.
PERM_MANAGE_USERS = "accounts.manage_internal_users"


def panel_poll_ms() -> int:
    """The poll interval, in milliseconds. Spec §8, §10: ten seconds."""
    return int(getattr(settings, "PANEL_POLL_SECONDS", 10)) * 1000


def waiting_on_finance() -> int:
    """How many requests currently sit with Finance (spec §10)."""
    from apps.transactions.models import Request
    from apps.transactions.services import AWAITING_FINANCE

    return Request.objects.filter(status__in=AWAITING_FINANCE).count()


class FinancePanelMixin(AccessMixin):
    """Login, a Finance role, and nothing else gets past."""

    def dispatch(self, request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return self.handle_no_permission()
        if getattr(user, "role", None) not in IDENTITY_AWARE_ROLES and not user.is_superuser:
            raise PermissionDenied(_("لوحة المالية متاحة لفريق المالية فقط."))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.setdefault("nav_section", getattr(self, "nav_section", ""))
        context["can_manage_merchants"] = user.has_perm(PERM_MANAGE_MERCHANTS)
        context["can_manage_payment_methods"] = user.has_perm(PERM_CHANGE_PAYMENT_METHOD)
        context["can_set_rates"] = user.has_perm(PERM_ADD_RATE)
        context["can_set_hours"] = user.has_perm(PERM_SET_HOURS)
        context["can_view_audit"] = user.has_perm(PERM_VIEW_AUDIT)
        context["can_manage_users"] = user.has_perm(PERM_MANAGE_USERS)
        # The rail badge (spec §10). Server-rendered on load and then kept
        # current by the ten-second poll in static/js/panel.js — so the number
        # is right on arrival even with scripting off, and right afterwards
        # with it on.
        context["queue_waiting"] = waiting_on_finance()
        context["poll"] = {
            "pulseUrl": reverse("finance:pulse"),
            "intervalMs": panel_poll_ms(),
        }
        # Step 11. On every screen, not only the hours page: a queue that has
        # stopped filling up because the portal is shut looks exactly like a
        # quiet morning, and the difference matters to whoever is working it.
        context["portal_closed"] = not business_hours.is_open()
        return context


class FinanceWriteMixin(FinancePanelMixin, PermissionRequiredMixin):
    """A Finance view that changes something. Set ``permission_required``."""

    raise_exception = True


class AuditedFormMixin:
    """Records a before/after audit entry around a successful form save.

    Set ``audit_action``; optionally narrow what is captured with
    ``audit_fields``. Subclasses of ``UpdateView`` get a genuine "before"
    because the row is re-read from the database prior to the save.
    """

    audit_action: str = ""
    audit_fields: list[str] | None = None

    def _audit_before(self):
        obj = getattr(self, "object", None)
        if obj is None or obj.pk is None:
            return None
        fresh = type(obj)._default_manager.filter(pk=obj.pk).first()
        return snapshot(fresh, self.audit_fields) if fresh else None

    def form_valid(self, form):
        before = self._audit_before()
        response = super().form_valid(form)
        record_audit(
            action=self.audit_action,
            target=self.object,
            request=self.request,
            before=before,
            after=snapshot(self.object, self.audit_fields),
        )
        return response


class ToastMixin:
    """Adds a success message after a form save."""

    success_message = ""

    def form_valid(self, form):
        response = super().form_valid(form)
        if self.success_message:
            messages.success(self.request, self.success_message % {"object": self.object})
        return response
