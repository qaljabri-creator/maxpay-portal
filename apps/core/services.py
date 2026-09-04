"""Helpers for writing audit entries.

Every status change, wallet change, rate change and permission change goes
through :func:`record_audit` (spec §5). Keeping it in one place means the
serialisation of ``before``/``after`` is consistent and the log stays queryable.
"""

import logging
from decimal import Decimal
from typing import Any

from django.db import models
from django.utils import timezone

from .models import AuditLog

logger = logging.getLogger("maxpay.audit")


def _jsonify(value: Any) -> Any:
    """Coerce a value into something ``JSONField`` will accept losslessly."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, models.Model):
        return {"model": value._meta.label, "pk": _jsonify(value.pk), "repr": str(value)}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def snapshot(instance: models.Model, fields: list[str] | None = None) -> dict:
    """Return a JSON-safe dict of ``instance``'s field values.

    Pass ``fields`` to limit what is captured — never snapshot a field holding
    client identity into a record a merchant could read.
    """
    names = fields or [
        f.name for f in instance._meta.concrete_fields if not f.primary_key
    ]
    data = {}
    for name in names:
        field = instance._meta.get_field(name)
        if isinstance(field, models.ForeignKey):
            data[name] = _jsonify(getattr(instance, f"{name}_id"))
        else:
            data[name] = _jsonify(getattr(instance, name, None))
    return data


def client_ip(request) -> str | None:
    """Best-effort client IP.

    ``X-Forwarded-For`` is only consulted because the app runs behind a known
    reverse proxy; the left-most entry is taken.
    """
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        candidate = forwarded.split(",")[0].strip()
        if candidate:
            return candidate
    return request.META.get("REMOTE_ADDR") or None


def record_audit(
    *,
    action: str,
    target: models.Model | None = None,
    target_type: str | None = None,
    target_id: Any = None,
    actor=None,
    actor_label: str = "",
    before: Any = None,
    after: Any = None,
    request=None,
    ip: str | None = None,
) -> AuditLog:
    """Write one audit entry.

    Either pass ``target`` (a model instance) or both ``target_type`` and
    ``target_id``. ``actor`` and ``ip`` are inferred from ``request`` when it is
    supplied and they were not given explicitly.
    """
    if target is not None:
        target_type = target_type or target._meta.label
        target_id = target.pk if target_id is None else target_id
    if not target_type:
        raise ValueError("record_audit needs either target or target_type.")

    if actor is None and request is not None:
        candidate = getattr(request, "user", None)
        if candidate is not None and getattr(candidate, "is_authenticated", False):
            actor = candidate

    if not actor_label and actor is not None:
        actor_label = str(actor)[:150]

    entry = AuditLog(
        actor=actor,
        actor_label=actor_label,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else "",
        before=_jsonify(before),
        after=_jsonify(after),
        ip=ip if ip is not None else client_ip(request),
    )
    entry.save()

    logger.info(
        "audit action=%s target=%s#%s actor=%s at=%s",
        action,
        target_type,
        entry.target_id,
        actor_label or "system",
        timezone.now().isoformat(),
    )
    return entry
