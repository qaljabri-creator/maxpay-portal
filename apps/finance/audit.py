"""Turning audit rows into something a person can read — build-order step 12.

:class:`~apps.core.models.AuditLog` is written to be *durable*, not to be
readable: ``target_type`` is a text label so an entry survives the deletion of
whatever it points at, and ``before``/``after`` are raw JSON snapshots of field
values. That is the right trade for a record which must still make sense in two
years. It is the wrong thing to put on a screen.

This module is the translation layer, and it is deliberately read-only — it
resolves, formats and labels, and has no path that writes anything. Spec §11:
"no delete or update path exposed anywhere in the application".

Three jobs:

* :func:`diff_rows` — what actually changed between ``before`` and ``after``,
  field by field, with the model's own Arabic verbose names where the target
  model can still be resolved.
* :func:`resolve_targets` — the label and, where one exists, the Finance panel
  link for each entry's target, resolved in bulk so a page of fifty entries
  costs a handful of queries rather than fifty.
* :func:`target_label` — the model's verbose name, so a filter dropdown reads
  «طلب» rather than ``transactions.Request``.
"""

from django.apps import apps as django_apps
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

#: Rendered for a value that is absent on one side of the change.
MISSING = "···"

#: Target types that have a page in the Finance panel, and how to find it.
#:
#: Everything else still shows — as its label and id — it just does not link
#: anywhere. ``accounts.Client`` is deliberately absent even though it acquired
#: a panel page in Finance review 4.1: resolving one here would put a client's
#: name on a screen whose own permission (``accounts.view_audit_log``) says
#: nothing about client identity, and linking to a page the reader may not be
#: allowed to open is not an improvement on not linking at all.
LINKED_TARGETS = {
    "transactions.Request",
    "merchants.Merchant",
    "merchants.MerchantMethod",
    "merchants.Wallet",
    "merchants.PaymentMethod",
    "rates.ExchangeRate",
    "core.SystemSettings",
}


def get_model(target_type: str):
    """The model a ``target_type`` names, or ``None`` if it no longer exists."""
    try:
        return django_apps.get_model(target_type)
    except (LookupError, ValueError):
        return None


def target_label(target_type: str) -> str:
    """The model's verbose name, falling back to the stored label."""
    model = get_model(target_type)
    if model is None:
        return target_type
    return str(model._meta.verbose_name)


def field_label(model, name: str) -> str:
    """A field's Arabic label, falling back to the key as stored.

    Keys that are not fields turn up legitimately — ``record_audit`` callers add
    things like ``reason`` and ``replaced`` to say *why* a change happened — so
    an unknown key is normal, not an error.
    """
    if model is not None:
        try:
            return str(model._meta.get_field(name).verbose_name)
        except Exception:
            pass
    return name


def format_value(value) -> str:
    """One JSON snapshot value as display text."""
    if value is None or value == "":
        return MISSING
    if isinstance(value, bool):
        return str(_("نعم") if value else _("لا"))
    if isinstance(value, dict):
        # ``snapshot`` writes a related object as {model, pk, repr}.
        if "repr" in value:
            return str(value["repr"])
        return "، ".join(f"{key}: {format_value(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return "، ".join(format_value(item) for item in value) or MISSING
    return str(value)


def diff_rows(entry) -> list[dict]:
    """What changed, one row per key.

    Keys are ordered by the ``after`` snapshot — the state that was written is
    the one worth reading in order — with anything only present in ``before``
    (a field that was cleared, or removed from the snapshot list) after it.

    A ``before`` or ``after`` that is not a mapping is shown as a single
    unnamed row rather than dropped: ``record_audit`` accepts any JSON-able
    value and a few callers pass a bare one.
    """
    before, after = entry.before, entry.after

    if not isinstance(before, dict) and not isinstance(after, dict):
        if before is None and after is None:
            return []
        return [
            {
                "field": "",
                "label": _("القيمة"),
                "before": format_value(before),
                "after": format_value(after),
                "changed": before != after,
            }
        ]

    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}

    model = get_model(entry.target_type)
    keys = list(after) + [key for key in before if key not in after]

    return [
        {
            "field": key,
            "label": field_label(model, key),
            "before": format_value(before.get(key)),
            "after": format_value(after.get(key)),
            # A creation has no "before" at all; marking every one of its rows
            # as changed would be noise, so an absent snapshot is not a change.
            "changed": bool(before) and before.get(key) != after.get(key),
        }
        for key in keys
    ]


def changed_fields(entry) -> list[str]:
    """The labels of the fields this entry actually changed, for the list view."""
    return [row["label"] for row in diff_rows(entry) if row["changed"]]


# ---------------------------------------------------------------------------
# Resolving targets in bulk
# ---------------------------------------------------------------------------


def _int_ids(ids) -> list[int]:
    """``target_id`` is text; only the numeric ones can address a row."""
    out = []
    for value in ids:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def _requests(ids) -> dict[str, dict]:
    from apps.transactions.models import Request

    rows = Request.objects.filter(pk__in=_int_ids(ids)).values_list("pk", "public_ref")
    return {
        str(pk): {
            "label": ref,
            "url": reverse("finance:request_detail", args=[ref]),
        }
        for pk, ref in rows
    }


def _merchants(ids) -> dict[str, dict]:
    from apps.merchants.models import Merchant

    rows = Merchant.objects.filter(pk__in=_int_ids(ids)).values_list("pk", "name")
    return {
        str(pk): {"label": name, "url": reverse("finance:merchant_detail", args=[pk])}
        for pk, name in rows
    }


def _merchant_methods(ids) -> dict[str, dict]:
    from apps.merchants.models import MerchantMethod

    rows = MerchantMethod.objects.filter(pk__in=_int_ids(ids)).select_related(
        "merchant", "payment_method"
    )
    return {
        str(link.pk): {
            "label": f"{link.merchant.name} · {link.payment_method}",
            "url": reverse("finance:merchant_detail", args=[link.merchant_id]),
        }
        for link in rows
    }


def _wallets(ids) -> dict[str, dict]:
    from apps.merchants.models import Wallet

    rows = Wallet.objects.filter(pk__in=_int_ids(ids)).select_related(
        "merchant_method__merchant"
    )
    return {
        str(wallet.pk): {
            "label": f"{wallet.number} · {wallet.merchant_method.merchant.name}",
            "url": reverse(
                "finance:merchant_detail", args=[wallet.merchant_method.merchant_id]
            ),
        }
        for wallet in rows
    }


def _payment_methods(ids) -> dict[str, dict]:
    from apps.merchants.models import PaymentMethod

    rows = PaymentMethod.objects.filter(pk__in=_int_ids(ids))
    return {
        str(method.pk): {
            "label": str(method),
            "url": reverse("finance:payment_method_update", args=[method.pk]),
        }
        for method in rows
    }


def _rates(ids) -> dict[str, dict]:
    from apps.rates.models import ExchangeRate

    rows = ExchangeRate.objects.filter(pk__in=_int_ids(ids))
    # The history page is the only view of a rate there is — spec §5 forbids
    # editing one, so there is no per-rate screen to link to.
    return {
        str(rate.pk): {"label": str(rate), "url": reverse("finance:rate_list")}
        for rate in rows
    }


def _system_settings(ids) -> dict[str, dict]:
    url = reverse("finance:business_hours")
    return {str(value): {"label": str(_("مواعيد العمل")), "url": url} for value in ids}


#: target_type → the bulk resolver for it.
RESOLVERS = {
    "transactions.Request": _requests,
    "merchants.Merchant": _merchants,
    "merchants.MerchantMethod": _merchant_methods,
    "merchants.Wallet": _wallets,
    "merchants.PaymentMethod": _payment_methods,
    "rates.ExchangeRate": _rates,
    "core.SystemSettings": _system_settings,
}


def resolve_targets(entries) -> dict[tuple[str, str], dict]:
    """``{(target_type, target_id): {"label", "url"}}`` for a page of entries.

    One query per distinct target type, not one per entry. A target that has
    since been deleted simply does not appear: the entry still renders, with the
    id it was written with, which is the whole reason the id is stored as text.
    """
    wanted: dict[str, set[str]] = {}
    for entry in entries:
        if entry.target_type in RESOLVERS:
            wanted.setdefault(entry.target_type, set()).add(entry.target_id)

    resolved: dict[tuple[str, str], dict] = {}
    for target_type, ids in wanted.items():
        for target_id, info in RESOLVERS[target_type](ids).items():
            resolved[(target_type, target_id)] = info
    return resolved
