"""The audit log viewer (spec §9) — build-order step 12.

Two screens: a filtered list, and one entry with its before/after laid out
field by field.

**Both are read-only, and that is a requirement rather than a simplification.**
Spec §11: "audit log is append-only, with no delete or update path exposed
anywhere in the application". So there is no form, no action endpoint and no
POST handler here — ``ListView`` and ``DetailView`` answer ``GET`` and reject
everything else with a 405, the model refuses a second ``save`` or any
``delete`` (:class:`apps.core.models.AppendOnlyModel`), and the ``change`` and
``delete`` permissions do not exist to be granted.

Access needs ``accounts.view_audit_log``, which
:data:`apps.accounts.permissions.MERCHANT_FORBIDDEN` lists — a merchant account
cannot hold it even if someone edits the matrix to try (spec §2).
"""

from django.contrib.auth.mixins import PermissionRequiredMixin
from django.db.models import Q
from django.views.generic import DetailView, ListView

from apps.core.models import AuditLog

from . import audit
from .audit_forms import AuditFilterForm
from .mixins import PERM_VIEW_AUDIT, FinancePanelMixin


class AuditAccessMixin(FinancePanelMixin, PermissionRequiredMixin):
    """A Finance role *and* the audit permission. Read-only by construction."""

    permission_required = PERM_VIEW_AUDIT
    nav_section = "audit"

    def handle_no_permission(self):
        # Two different refusals behind one hook. Someone not signed in needs
        # the login page; someone signed in who simply lacks the permission
        # needs to be told no, not sent to a form they have already filled in.
        self.raise_exception = self.request.user.is_authenticated
        return super().handle_no_permission()


class AuditLogView(AuditAccessMixin, ListView):
    """The log, newest first, with the filters spec §9's viewer needs."""

    model = AuditLog
    template_name = "finance/audit_list.html"
    context_object_name = "entries"
    paginate_by = 50

    def get_filter_form(self) -> AuditFilterForm:
        if not hasattr(self, "_filter_form"):
            form = AuditFilterForm(self.request.GET or None)
            form.is_valid()  # populates cleaned_data; every field is optional
            self._filter_form = form
        return self._filter_form

    def get_queryset(self):
        cleaned = getattr(self.get_filter_form(), "cleaned_data", {}) or {}
        queryset = AuditLog.objects.select_related("actor")

        if cleaned.get("action"):
            queryset = queryset.filter(action=cleaned["action"])
        if cleaned.get("target_type"):
            queryset = queryset.filter(target_type=cleaned["target_type"])
        if cleaned.get("actor"):
            queryset = queryset.filter(actor=cleaned["actor"])
        if cleaned.get("date_from"):
            queryset = queryset.filter(created_at__date__gte=cleaned["date_from"])
        if cleaned.get("date_to"):
            queryset = queryset.filter(created_at__date__lte=cleaned["date_to"])

        search = (cleaned.get("q") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(actor_label__icontains=search)
                | Q(target_id__iexact=search)
                | Q(ip__icontains=search)
            )
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        entries = list(context["entries"])
        # One lookup for the whole page, not one per row (see audit.py).
        targets = audit.resolve_targets(entries)

        context["rows"] = [
            {
                "entry": entry,
                "target": targets.get((entry.target_type, entry.target_id)),
                "type_label": audit.target_label(entry.target_type),
                "changed": audit.changed_fields(entry),
            }
            for entry in entries
        ]
        context["filter_form"] = self.get_filter_form()
        context["has_filters"] = any(
            self.request.GET.get(name)
            for name in ("q", "action", "target_type", "actor", "date_from", "date_to")
        )
        params = self.request.GET.copy()
        params.pop("page", None)
        context["querystring"] = params.urlencode()
        return context


class AuditEntryView(AuditAccessMixin, DetailView):
    """One entry, with its snapshot laid out as a before/after table."""

    model = AuditLog
    template_name = "finance/audit_detail.html"
    context_object_name = "entry"

    def get_queryset(self):
        return AuditLog.objects.select_related("actor")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        entry = self.object
        context["rows"] = audit.diff_rows(entry)
        context["target"] = audit.resolve_targets([entry]).get(
            (entry.target_type, entry.target_id)
        )
        context["type_label"] = audit.target_label(entry.target_type)
        # Everything else written about the same object, so an entry can be read
        # in the context of what came before and after it — which is most of
        # what an investigation is.
        context["neighbours"] = (
            AuditLog.objects.filter(
                target_type=entry.target_type, target_id=entry.target_id
            )
            .exclude(pk=entry.pk)
            .select_related("actor")[:10]
        )
        return context
