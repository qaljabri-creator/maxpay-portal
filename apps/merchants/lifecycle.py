"""Retiring a merchant, and getting rid of a wallet (Finance review, 25 Aug 2026).

Finance could deactivate either of these and never be rid of them. A merchant
who stopped working with the desk two years ago still sat in every dropdown; a
wallet created by a typo sat under its method forever. Deactivation is a state
a thing can come back from, and a list of things nobody intends to come back to
is a list that stops being read.

**Nothing here loosens spec §11.** The audit trail has to keep resolving, so a
merchant is *archived*, never deleted: their name on a request from last March
is part of that request, and a dangling foreign key is not a tidier database.
The one deletion this module performs is a wallet **no request was ever
submitted against**, which is not a business record at all — it is a row that
was created and never used, and there is no history in it to keep.

The two are one module because they answer the same question in two ways:

* a merchant always has history, so archiving is the only option;
* a wallet may or may not, so the question is asked and the answer decides.

Both write an audit entry (spec §11). The deletion writes a fuller one on
purpose: the row it describes will not be there to look at afterwards, so the
entry has to carry enough of it to stand on its own.
"""

from dataclasses import dataclass

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.choices import AuditAction
from apps.core.services import record_audit, snapshot

from .models import Merchant, Wallet

#: The permission both operations need. Separate from ``manage_merchants``,
#: which is delegable to a ``finance_staff`` account: adding a wallet and
#: retiring a merchant are not the same size of act.
PERM_ARCHIVE = "merchants.archive_merchants"

MERCHANT_FIELDS = ["name", "is_active", "archived_at"]
WALLET_FIELDS = ["number", "label", "is_active", "daily_cap", "archived_at"]


class LifecycleError(Exception):
    """A retirement that must not happen, with something to show the operator."""

    def __init__(self, message, *, code: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code


def _require(actor):
    if actor is None or not actor.has_perm(PERM_ARCHIVE):
        raise PermissionDenied(_("لا تملك صلاحية الأرشفة والحذف."))


# ---------------------------------------------------------------- merchants --


@transaction.atomic
def archive_merchant(merchant: Merchant, *, actor, http_request=None) -> Merchant:
    """Retire a merchant from every list without touching their history.

    Deactivation comes with it rather than instead of it. Archiving alone
    would leave a merchant who is invisible everywhere and still routable by
    anything that only asked ``is_active`` — and there are several such places,
    which is exactly the sort of gap that gets found in production.

    Requests already with them are deliberately left alone. They are somebody's
    money in flight; ending them is `cancel`'s job and it is a different
    decision, taken per request with a reason the client reads.
    """
    _require(actor)
    locked = Merchant.objects.select_for_update().get(pk=merchant.pk)
    if locked.is_archived:
        raise LifecycleError(_("هذا التاجر مؤرشف بالفعل."), code="already_archived")

    before = snapshot(locked, MERCHANT_FIELDS)
    locked.archived_at = timezone.now()
    locked.archived_by = actor
    locked.is_active = False
    locked.save(update_fields=["archived_at", "archived_by", "is_active", "updated_at"])

    record_audit(
        action=AuditAction.MERCHANT_CHANGE,
        target=locked,
        actor=actor,
        request=http_request,
        before=before,
        after={**snapshot(locked, MERCHANT_FIELDS), "event": "merchant_archived"},
    )
    return locked


@transaction.atomic
def restore_merchant(merchant: Merchant, *, actor, http_request=None) -> Merchant:
    """Bring an archived merchant back into the lists.

    It does **not** reactivate them. Coming out of the archive and being open
    for business are two decisions, and doing the second silently as part of the
    first would put a merchant back in front of clients on the strength of
    somebody undoing a mis-click.
    """
    _require(actor)
    locked = Merchant.objects.select_for_update().get(pk=merchant.pk)
    if not locked.is_archived:
        raise LifecycleError(_("هذا التاجر غير مؤرشف."), code="not_archived")

    before = snapshot(locked, MERCHANT_FIELDS)
    locked.archived_at = None
    locked.archived_by = None
    locked.save(update_fields=["archived_at", "archived_by", "updated_at"])

    record_audit(
        action=AuditAction.MERCHANT_CHANGE,
        target=locked,
        actor=actor,
        request=http_request,
        before=before,
        after={**snapshot(locked, MERCHANT_FIELDS), "event": "merchant_restored"},
    )
    return locked


# ------------------------------------------------------------------ wallets --


@dataclass(frozen=True)
class Removal:
    """What happened to a wallet, so the screen can say which and why."""

    deleted: bool
    number: str
    label: str
    merchant_id: int

    @property
    def archived(self) -> bool:
        return not self.deleted


@transaction.atomic
def remove_wallet(wallet: Wallet, *, actor, http_request=None) -> Removal:
    """Delete the wallet if nothing was ever submitted against it; else archive.

    The test is ``Wallet.was_used``, and it is asked here rather than left to
    the caller so the two outcomes cannot drift apart between screens. Inside
    the transaction and after a row lock, because a request submitted between
    the question and the delete is precisely the race this is guarding.

    ``Request.wallet`` is ``PROTECT``, so the database refuses the delete even
    if this ever gets the answer wrong. That is the belt; this is the braces,
    and the braces are what produce a sentence the operator can read.
    """
    _require(actor)
    locked = Wallet.objects.select_for_update().select_related(
        "merchant_method__merchant", "merchant_method__payment_method"
    ).get(pk=wallet.pk)

    if locked.is_archived:
        raise LifecycleError(_("هذه المحفظة مؤرشفة بالفعل."), code="already_archived")

    merchant = locked.merchant_method.merchant
    details = {
        "number": locked.number,
        "label": locked.label,
        "merchant": merchant.name,
        "payment_method": str(locked.merchant_method.payment_method),
        "had_qr": bool(locked.qr_image),
    }
    removal_number = locked.number
    removal_label = locked.label

    if locked.was_used:
        before = snapshot(locked, WALLET_FIELDS)
        locked.archived_at = timezone.now()
        locked.archived_by = actor
        locked.is_active = False
        locked.deactivated_at = locked.deactivated_at or timezone.now()
        locked.save(
            update_fields=[
                "archived_at", "archived_by", "is_active", "deactivated_at", "updated_at",
            ]
        )
        record_audit(
            action=AuditAction.WALLET_CHANGE,
            target=locked,
            actor=actor,
            request=http_request,
            before=before,
            after={
                **snapshot(locked, WALLET_FIELDS),
                "event": "wallet_archived",
                "reason": "has_requests",
            },
        )
        return Removal(
            deleted=False,
            number=removal_number,
            label=removal_label,
            merchant_id=merchant.pk,
        )

    # Nothing was ever submitted against it. The audit entry is written first
    # and carries the whole row, because in a moment there will be nothing left
    # to look the row up in — and it is recorded against the merchant, whose
    # page survives, rather than against a primary key that is about to stop
    # existing.
    record_audit(
        action=AuditAction.WALLET_CHANGE,
        target=merchant,
        actor=actor,
        request=http_request,
        before=snapshot(locked, WALLET_FIELDS),
        after={"event": "wallet_deleted", "reason": "never_used", **details},
    )
    locked.delete()
    return Removal(
        deleted=True,
        number=removal_number,
        label=removal_label,
        merchant_id=merchant.pk,
    )
