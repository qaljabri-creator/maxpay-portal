"""The request lifecycle as a state machine (spec §6).

Every status change in the system goes through :func:`apply_transition`. It is
the only place that writes ``Request.status``, and that is deliberate: a status
change is never *only* a status change. It stamps a timestamp, it writes an
audit entry, and — when a request is rejected — it posts the reason into the
thread, because spec §6 says the client is owed the reason and a field nobody
renders is not telling them anything.

The table below is the whole lifecycle. Reading it answers three questions that
were previously answered by whichever view happened to be doing the work:

* **who** — a permission, not a role, so a ``finance_admin`` can delegate any
  single step to ``finance_staff`` without a code change (spec §3);
* **from where** — the source statuses, so a request cannot skip review, be
  credited before the merchant confirmed, or be reopened after it closed;
* **for which direction** — deposits and withdrawals share statuses but not
  paths, and the type is what decides which.

One move has no human actor at all. ``auto_route`` is the system routing a
deposit to the merchant the client picked, at the moment they submit it, so the
merchant can act without waiting for a desk. It is in this table rather than in
the submission code for the same reason every other move is: it stamps
``assigned_at``, it writes an audit entry, and it must refuse a merchant who
stopped being able to execute the request. See :func:`auto_route`.

The merchant's own transitions (``confirm``, ``pay``) are defined here even
though the merchant panel is build-order step 8. They are part of the lifecycle,
not part of a screen: Finance cannot reach ``credited`` unless the merchant can
reach ``merchant_confirmed``, and the queue's "waiting on the merchant" column
means nothing without them. Step 8 adds the interface, not the rules.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Role
from apps.core.choices import ActorRole, AuditAction
from apps.core.services import record_audit, snapshot

from . import messaging
from .models import Request, RequestStatus, RequestType

logger = logging.getLogger("maxpay.transactions")

#: What an audit entry captures around a transition. Client identity is in it on
#: purpose: the audit log is Finance-only (``core.view_auditlog``, never granted
#: to a merchant), and tracing a request back to a client is the point of it.
AUDIT_FIELDS = [
    "public_ref",
    "type",
    "client",
    "status",
    "merchant_selected",
    "merchant_assigned",
    "amount_usd",
    "amount_iqd",
    "rejection_reason",
]

#: Statuses where the ball sits with Finance — the queue's default view.
#: ``pending`` is in it because that is the whole point of the state: a request
#: a merchant handed back, or one Finance parked, is work waiting on this desk
#: and has to appear in the tab that says so.
AWAITING_FINANCE = frozenset({
    RequestStatus.SUBMITTED,
    RequestStatus.UNDER_REVIEW,
    RequestStatus.PENDING,
    RequestStatus.MERCHANT_CONFIRMED,
    RequestStatus.MERCHANT_PAID,
    RequestStatus.CREDITED,
})

#: Routed and waiting on the merchant. Finance can still reroute or reject.
AWAITING_MERCHANT = frozenset({RequestStatus.ASSIGNED})

#: Everything still in flight, whoever holds it.
OPEN_STATUSES = AWAITING_FINANCE | AWAITING_MERCHANT


class _SystemActor:
    """The system acting on its own behalf, not on anybody's authority.

    Only :func:`auto_route` uses it. It exists so that a move with no operator
    behind it still goes through :func:`apply_transition` — the lock, the
    source-status check, the merchant validation, the timestamp and the audit
    entry are all things an automatic route needs exactly as much as a manual
    one, and writing ``Request.status`` anywhere else would put them in two
    places.

    It is a sentinel rather than a real ``User`` row because there is nobody to
    attribute the move to: the audit entry says ``system`` and means it.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<system>"


#: The one actor that is not a person. See :class:`_SystemActor`.
SYSTEM_ACTOR = _SystemActor()


class TransitionError(Exception):
    """A transition that must not happen, with a message fit to show a user."""

    def __init__(self, message, *, code: str = "not_allowed"):
        super().__init__(str(message))
        self.message = message
        self.code = code


@dataclass(frozen=True)
class Transition:
    """One legal move through the lifecycle."""

    action: str
    label: object
    target: str
    sources: frozenset
    permission: str
    types: frozenset
    actor_roles: frozenset
    #: The ``Request`` datetime field this move fills in, if any.
    stamps: str | None = None
    #: Free text the operator must supply.
    requires_reason: bool = False
    #: Where that text goes. A rejection or a cancellation is owed to the client
    #: (spec §6), so its reason is posted as an ordinary message they read. A
    #: handback or a park is desk business — "no cash today", "waiting on the
    #: client's bank" — so its reason is posted as an internal note instead.
    #: Both are mandatory; only the audience differs.
    reason_is_note: bool = False
    #: A merchant must be named. Only ``route`` does.
    requires_merchant: bool = False
    #: The opposite: the move gives the request back to nobody. Only
    #: ``hand_back`` does, and it has to — leaving ``merchant_assigned`` set
    #: would keep a returned request in the merchant's queue and keep it
    #: rejectable by them, which is precisely what handing it back undoes.
    #: Who returned it is in the audit entry, and is not lost by clearing it.
    clears_merchant: bool = False
    #: What the operator is told before they commit to it.
    confirm: object = ""
    #: Wording that only holds for one direction, keyed by ``RequestType``.
    #: Two moves need it, and both for the same reason: spec §6 puts the
    #: client's B2CORE debit inside a withdrawal's review and its reversal
    #: inside a withdrawal's rejection, and neither has any counterpart on a
    #: deposit. Telling a Finance user to reverse a debit that was never taken
    #: is how a client gets paid twice.
    confirm_by_type: dict = field(default_factory=dict)
    #: Presentation hint for the panel: primary, plain, or danger.
    tone: str = "plain"
    #: Which side of the desk the move belongs to, for grouping in the UI.
    actor_side: str = "finance"
    extra: dict = field(default_factory=dict)

    def applies_to(self, request_obj: Request) -> bool:
        return request_obj.type in self.types and request_obj.status in self.sources

    def confirm_for(self, request_obj: Request):
        """The confirmation text for *this* request, direction included."""
        return self.confirm_by_type.get(request_obj.type, self.confirm)


FINANCE_ROLES = frozenset({Role.FINANCE_ADMIN, Role.FINANCE_STAFF})
MERCHANT_ROLES = frozenset({Role.MERCHANT})
BOTH_TYPES = frozenset({RequestType.DEPOSIT, RequestType.WITHDRAWAL})


TRANSITIONS: dict[str, Transition] = {
    "review": Transition(
        action="review",
        label=_("ابدأ المراجعة"),
        target=RequestStatus.UNDER_REVIEW,
        sources=frozenset({RequestStatus.SUBMITTED}),
        permission="transactions.approve_request",
        types=BOTH_TYPES,
        actor_roles=FINANCE_ROLES,
        confirm=_("ينتقل الطلب إلى قيد المراجعة ويظهر للعميل أنه قيد النظر."),
        confirm_by_type={
            # Spec §6: at this point Finance verifies the client's balance and
            # eligibility and debits their B2CORE wallet. The system holds no
            # B2CORE credentials (spec §13), so the debit is done by hand in the
            # Back Office and this is the step that records it was.
            RequestType.WITHDRAWAL: _(
                "تحقّق من رصيد العميل وأهليته، ثم اخصم المبلغ من محفظته في B2CORE "
                "قبل المتابعة. النظام لا يخصم نيابةً عنك، والخصم هنا يمنع تداول "
                "مبلغ مُلتزَم بالسحب."
            ),
        },
        tone="primary",
    ),
    "route": Transition(
        action="route",
        label=_("إسناد إلى تاجر"),
        # Re-routing is the same move: a request already assigned may be moved
        # to another merchant while the first has not acted (spec §5).
        target=RequestStatus.ASSIGNED,
        # ``pending`` is here for the reassignment the Finance review asked
        # for: a merchant hands a request back, and it goes out again to
        # somebody else without being reviewed from scratch.
        sources=frozenset({
            RequestStatus.UNDER_REVIEW,
            RequestStatus.ASSIGNED,
            RequestStatus.PENDING,
        }),
        permission="transactions.route_request",
        types=BOTH_TYPES,
        actor_roles=FINANCE_ROLES,
        stamps="assigned_at",
        requires_merchant=True,
        confirm=_("يظهر الطلب في لوحة التاجر فورًا، دون أي بيانات تعريف عن العميل."),
        tone="primary",
    ),
    "auto_route": Transition(
        action="auto_route",
        label=_("إسناد تلقائي"),
        # Straight from `submitted`: a deposit no longer waits for a desk
        # before it reaches the merchant the client already chose. The client
        # has transferred the money to that merchant's wallet — routing it
        # anywhere else by default would be routing it away from where the
        # money actually went.
        target=RequestStatus.ASSIGNED,
        sources=frozenset({RequestStatus.SUBMITTED}),
        # Nobody holds this permission; the system path bypasses the check and
        # `actor_roles` being empty is what keeps this move off every panel.
        permission="transactions.route_request",
        # Deposits only. A withdrawal still goes through review first, because
        # that is where the client's B2CORE balance is verified and debited
        # (spec §6) and no merchant should be asked to pay out before it.
        types=frozenset({RequestType.DEPOSIT}),
        actor_roles=frozenset(),
        stamps="assigned_at",
        requires_merchant=True,
        actor_side="system",
    ),
    "confirm": Transition(
        action="confirm",
        label=_("تأكيد استلام المبلغ"),
        target=RequestStatus.MERCHANT_CONFIRMED,
        sources=frozenset({RequestStatus.ASSIGNED}),
        permission="transactions.confirm_request",
        types=frozenset({RequestType.DEPOSIT}),
        actor_roles=MERCHANT_ROLES,
        stamps="merchant_actioned_at",
        actor_side="merchant",
        tone="primary",
    ),
    "pay": Transition(
        action="pay",
        label=_("تأكيد تحويل المبلغ"),
        target=RequestStatus.MERCHANT_PAID,
        sources=frozenset({RequestStatus.ASSIGNED}),
        permission="transactions.confirm_request",
        types=frozenset({RequestType.WITHDRAWAL}),
        actor_roles=MERCHANT_ROLES,
        stamps="merchant_actioned_at",
        actor_side="merchant",
        tone="primary",
    ),
    "hand_back": Transition(
        action="hand_back",
        label=_("إعادة الطلب إلى المالية"),
        # Not a cancellation and not a rejection: the merchant is saying "not
        # me", and somebody else may well execute it. Finance decides which.
        target=RequestStatus.PENDING,
        sources=frozenset({RequestStatus.ASSIGNED}),
        permission="transactions.return_request",
        types=BOTH_TYPES,
        actor_roles=MERCHANT_ROLES,
        requires_reason=True,
        #: The reason is a *note*, not a message: it is why this merchant is
        #: handing the request back, and the client has no business reading
        #: "no cash today" or "I do not trust this receipt". See
        #: :func:`_post_thread_entries`.
        reason_is_note=True,
        clears_merchant=True,
        confirm=_(
            "يعود الطلب إلى المالية لإعادة إسناده أو إلغائه، ويختفي من قائمتك. "
            "السبب يُقرأ من المالية فقط، ولا يراه العميل."
        ),
        actor_side="merchant",
        tone="danger",
    ),
    "park": Transition(
        action="park",
        label=_("تعليق الطلب"),
        target=RequestStatus.PENDING,
        # From anywhere still in flight: a request can stop being actionable at
        # any point, and the desk should not have to reject it to say so.
        sources=frozenset({
            RequestStatus.SUBMITTED,
            RequestStatus.UNDER_REVIEW,
            RequestStatus.ASSIGNED,
            RequestStatus.MERCHANT_CONFIRMED,
            RequestStatus.MERCHANT_PAID,
        }),
        permission="transactions.approve_request",
        types=BOTH_TYPES,
        actor_roles=FINANCE_ROLES,
        requires_reason=True,
        reason_is_note=True,
        confirm=_(
            "الطلب مُعلَّق بانتظار شيء، لم يفشل ولم يُهمل. السبب يُسجَّل للمالية "
            "ولا يراه العميل. يمكن إسناده أو إلغاؤه لاحقًا."
        ),
    ),
    "cancel": Transition(
        action="cancel",
        label=_("إلغاء الطلب"),
        target=RequestStatus.CANCELLED,
        sources=frozenset(OPEN_STATUSES),
        permission="transactions.cancel_request",
        types=BOTH_TYPES,
        actor_roles=FINANCE_ROLES,
        stamps="closed_at",
        requires_reason=True,
        confirm=_(
            "الإلغاء يعني أن الطلب لم يعد مطلوبًا، لا أنه فشل. السبب يُنشر في "
            "محادثة الطلب ويقرؤه العميل. الإلغاء نهائي."
        ),
        confirm_by_type={
            # Same reasoning as the rejection wording: the system cannot move
            # money in B2CORE, so the one thing this screen can do is refuse to
            # let a taken debit be forgotten.
            RequestType.WITHDRAWAL: _(
                "الإلغاء يعني أن الطلب لم يعد مطلوبًا، لا أنه فشل. السبب يُنشر في "
                "محادثة الطلب ويقرؤه العميل. الإلغاء نهائي. إن كنت قد خصمت المبلغ "
                "من محفظة العميل في B2CORE فأعِده إليه الآن."
            ),
        },
        tone="danger",
    ),
    "credit": Transition(
        action="credit",
        label=_("سُجّل في B2CORE"),
        target=RequestStatus.CREDITED,
        sources=frozenset({RequestStatus.MERCHANT_CONFIRMED}),
        permission="transactions.credit_request",
        types=frozenset({RequestType.DEPOSIT}),
        actor_roles=FINANCE_ROLES,
        confirm=_(
            "أكّد أنك سجّلت الإيداع يدويًا في الـ Back Office. النظام لا يقيّد المبلغ نيابةً عنك."
        ),
        tone="primary",
    ),
    "close": Transition(
        action="close",
        label=_("إغلاق الطلب"),
        target=RequestStatus.CLOSED,
        # A deposit closes once credited; a withdrawal once the merchant paid.
        sources=frozenset({RequestStatus.CREDITED, RequestStatus.MERCHANT_PAID}),
        permission="transactions.close_request",
        types=BOTH_TYPES,
        actor_roles=FINANCE_ROLES,
        stamps="closed_at",
        confirm=_("الإغلاق نهائي؛ لا يمكن إعادة فتح الطلب."),
        tone="primary",
    ),
    "reject": Transition(
        action="reject",
        label=_("رفض الطلب"),
        target=RequestStatus.REJECTED,
        # Reachable from anywhere still in flight, by either side (spec §6).
        sources=frozenset(OPEN_STATUSES),
        permission="transactions.reject_request",
        types=BOTH_TYPES,
        actor_roles=FINANCE_ROLES | MERCHANT_ROLES,
        stamps="closed_at",
        requires_reason=True,
        confirm=_("يُنشر السبب في محادثة الطلب ويقرؤه العميل. الرفض نهائي."),
        confirm_by_type={
            # Spec §6: "any debit reversed". Nothing in the system can do it —
            # the reversal is a Back Office action like the debit was — so the
            # one thing this screen can do is refuse to let it be forgotten.
            RequestType.WITHDRAWAL: _(
                "يُنشر السبب في محادثة الطلب ويقرؤه العميل. الرفض نهائي. "
                "إن كنت قد خصمت المبلغ من محفظة العميل في B2CORE فأعِده إليه الآن."
            ),
        },
        tone="danger",
    ),
}


def get_transition(action: str) -> Transition:
    try:
        return TRANSITIONS[action]
    except KeyError as exc:
        raise TransitionError(_("إجراء غير معروف."), code="unknown_action") from exc


def actor_role_of(user) -> str:
    """The :class:`ActorRole` an internal user acts as on the thread."""
    role = getattr(user, "role", None)
    if role in ActorRole.values:
        return role
    # A superuser with no business role still has to be attributable.
    return ActorRole.SYSTEM


def merchant_holds(request_obj: Request, user) -> bool:
    """Whether ``request_obj`` is the routed responsibility of ``user``.

    Spec §8 gives a merchant only the requests assigned to them. That is an
    *object-level* rule, so no permission expresses it — which is exactly why it
    is checked here, on the same path every move takes, rather than left to
    whichever queryset a screen happens to use.
    """
    profile_id = getattr(getattr(user, "merchant_profile", None), "pk", None)
    return profile_id is not None and profile_id == request_obj.merchant_assigned_id


def available_transitions(request_obj: Request, user) -> list[Transition]:
    """Every move ``user`` may make on ``request_obj`` right now.

    Three things are checked — the lifecycle allows it, the user holds the
    permission, and (for a merchant) the request is actually theirs — so a
    screen can render exactly the buttons that will work.
    """
    role = getattr(user, "role", None)
    is_super = bool(getattr(user, "is_superuser", False))
    if role in MERCHANT_ROLES and not merchant_holds(request_obj, user):
        return []
    return [
        move
        for move in TRANSITIONS.values()
        if move.applies_to(request_obj)
        and (is_super or role in move.actor_roles)
        and user.has_perm(move.permission)
    ]


def eligible_merchants(request_obj: Request):
    """Merchants that could actually execute ``request_obj`` (spec §9).

    Active, and covering the request's payment method through an active
    ``MerchantMethod``. A deposit additionally needs the merchant to have an
    active wallet, since the client's money has to have somewhere to be checked
    against — the same availability rule the client's own merchant list uses.
    """
    from django.db.models import Exists, OuterRef

    from apps.merchants.models import Merchant, MerchantMethod, Wallet

    links = MerchantMethod.objects.filter(
        merchant=OuterRef("pk"),
        payment_method_id=request_obj.payment_method_id,
        is_active=True,
    )
    if request_obj.is_deposit:
        links = links.filter(
            Exists(
                Wallet.objects.filter(
                    merchant_method=OuterRef("pk"),
                    is_active=True,
                    archived_at__isnull=True,
                )
            )
        )
    return (
        Merchant.objects.filter(is_active=True, archived_at__isnull=True)
        .filter(Exists(links))
        .order_by("name")
    )


def _validate_merchant(request_obj: Request, merchant) -> None:
    """Refuse a route the named merchant could not actually execute."""
    if merchant is None:
        raise TransitionError(_("اختر التاجر المراد الإسناد إليه."), code="merchant_missing")
    if merchant.is_archived:
        # Checked ahead of ``is_active`` so the message says the true reason:
        # archiving clears active too, and "stopped" would send Finance looking
        # for a switch that will not bring them back.
        raise TransitionError(_("هذا التاجر مؤرشف."), code="merchant_archived")
    if not merchant.is_active:
        raise TransitionError(_("هذا التاجر موقوف."), code="merchant_inactive")
    covers = merchant.methods.filter(
        payment_method_id=request_obj.payment_method_id, is_active=True
    ).exists()
    if not covers:
        raise TransitionError(
            _("هذا التاجر لا يغطي طريقة الدفع المستخدمة في هذا الطلب."),
            code="merchant_method_missing",
        )


@transaction.atomic
def apply_transition(
    request_obj: Request,
    action: str,
    *,
    actor,
    merchant=None,
    reason: str = "",
    note: str = "",
    http_request=None,
) -> Request:
    """Move ``request_obj`` along the lifecycle, or raise :class:`TransitionError`.

    The row is re-read and locked first. Two staff opening the same request and
    both pressing "route" is not hypothetical on a shared queue, and the second
    press must be told the request already moved rather than silently overwrite
    the first one's decision.
    """
    move = get_transition(action)

    # The system is not a role and holds no permissions, so the two gates below
    # have nothing to check it against. Everything after them still applies:
    # it takes the same lock, obeys the same source statuses, and is validated
    # and audited identically.
    is_system = actor is SYSTEM_ACTOR

    role = None if is_system else getattr(actor, "role", None)
    if not is_system:
        if not getattr(actor, "is_superuser", False) and role not in move.actor_roles:
            raise TransitionError(_("هذا الإجراء ليس من صلاحيات دورك."), code="wrong_role")
        if not actor.has_perm(move.permission):
            raise TransitionError(_("لا تملك صلاحية هذا الإجراء."), code="no_permission")

    # Re-read under a lock: everything below decides on current state, not on
    # whatever the operator's page happened to be rendered from.
    locked = (
        Request.objects.select_for_update()
        .select_related("client", "payment_method", "merchant_selected", "merchant_assigned")
        .get(pk=request_obj.pk)
    )

    if locked.type not in move.types:
        raise TransitionError(
            _("هذا الإجراء لا ينطبق على هذا النوع من الطلبات."), code="wrong_type"
        )
    if role in MERCHANT_ROLES and not merchant_holds(locked, actor):
        # Checked against the locked row, not the one the caller passed: a
        # reroute between page render and POST must take the request away from
        # the merchant who no longer holds it.
        raise TransitionError(
            _("هذا الطلب ليس مُسندًا إليك."), code="not_assigned"
        )
    if locked.status not in move.sources:
        raise TransitionError(
            _("تغيّرت حالة الطلب إلى «%(status)s» قبل تنفيذ الإجراء. حدّث الصفحة.")
            % {"status": locked.get_status_display()},
            code="stale",
        )

    reason = (reason or "").strip()
    if move.requires_reason and not reason:
        raise TransitionError(_("اكتب سبب الرفض."), code="reason_missing")

    before = snapshot(locked, AUDIT_FIELDS)
    changed = ["status", "updated_at"]

    if move.requires_merchant:
        _validate_merchant(locked, merchant)
        locked.merchant_assigned = merchant
        changed.append("merchant_assigned")
    elif move.clears_merchant:
        locked.merchant_assigned = None
        changed.append("merchant_assigned")

    _stamp_operator(locked, actor, changed)

    locked.status = move.target
    if move.stamps:
        setattr(locked, move.stamps, timezone.now())
        changed.append(move.stamps)
    # Only a reason the *client* is owed is stored on the row. `rejection_reason`
    # is the column that says why a request ended, and the client's screen
    # renders it as such — so a handback note ("no cash today") or a park note
    # must not land in it. Those live in the thread as notes and nowhere else.
    if move.requires_reason and not move.reason_is_note:
        locked.rejection_reason = reason
        changed.append("rejection_reason")

    locked.save(update_fields=sorted(set(changed)))

    _post_thread_entries(locked, move, actor=actor, reason=reason, note=note)

    record_audit(
        action=AuditAction.STATUS_CHANGE,
        target=locked,
        actor=None if is_system else actor,
        actor_label="system" if is_system else "",
        request=None if is_system else http_request,
        before=before,
        after={**snapshot(locked, AUDIT_FIELDS), "action": move.action},
    )
    logger.info(
        "Request %s: %s to %s by user %s",
        locked.public_ref,
        before["status"],
        locked.status,
        getattr(actor, "pk", None),
    )
    return locked


#: Where an amount may still be corrected. Everything live except ``credited``:
#: once Finance has recorded the deposit in the B2CORE Back Office, the figure
#: here and the figure there have to agree, and this system cannot change the
#: one over there. A correction after that is a Back Office correction first.
AMOUNT_EDITABLE_STATUSES = frozenset(OPEN_STATUSES - {RequestStatus.CREDITED})

#: What an amount correction records, before and after.
AMOUNT_FIELDS = [
    "public_ref",
    "amount_usd",
    "amount_iqd",
    "commission_applied",
    "rate_applied",
    "submitted_amount_usd",
    "submitted_amount_iqd",
    "title",
]


@transaction.atomic
def correct_amount(
    request_obj: Request,
    new_amount_usd,
    *,
    actor,
    reason: str = "",
    http_request=None,
) -> Request:
    """Correct a request to the amount that actually arrived (Finance review).

    The real case is a client who asks for $100 and transfers $60. The request
    becomes $60, because that is what a merchant confirmed and what Finance
    will credit; ``submitted_amount_usd`` keeps the $100, because "what was
    asked for" and "what turned up" are two different questions.

    **The rate does not move.** Finance was asked whether an edit re-prices the
    request at today's rate and said no: the client transferred against a
    quoted figure, and re-pricing after the fact changes a settled agreement.
    So the dinar figures are recomputed from ``rate_applied`` — the very
    revision this request was quoted on, whatever the desk is quoting now — and
    the commission is re-prorated from the same row, because a commission
    charged on $100 when $60 arrived is not the commission that rate defines.

    Who may do it: Finance, and the merchant the request is routed to. Both
    were asked for by name. Every edit is audited with the old value, the new
    value, who and when, and the request carries the last of those on its face.
    """
    from apps.portal import pricing

    role = getattr(actor, "role", None)
    is_super = bool(getattr(actor, "is_superuser", False))
    if not is_super and role not in (FINANCE_ROLES | MERCHANT_ROLES):
        raise TransitionError(_("تعديل المبلغ ليس من صلاحيات دورك."), code="wrong_role")
    if not actor.has_perm("transactions.change_request_amount"):
        raise TransitionError(_("لا تملك صلاحية تعديل المبلغ."), code="no_permission")

    locked = (
        Request.objects.select_for_update()
        .select_related("client", "payment_method", "merchant_assigned")
        .get(pk=request_obj.pk)
    )

    if role in MERCHANT_ROLES and not merchant_holds(locked, actor):
        raise TransitionError(_("هذا الطلب ليس مُسندًا إليك."), code="not_assigned")
    if locked.status not in AMOUNT_EDITABLE_STATUSES:
        raise TransitionError(
            _("لا يمكن تعديل المبلغ بعد أن أصبح الطلب «%(status)s».")
            % {"status": locked.get_status_display()},
            code="not_editable",
        )

    try:
        amount = pricing.parse_amount_usd(new_amount_usd, locked.type)
    except pricing.PricingError as exc:
        raise TransitionError(exc.message, code=exc.code) from exc

    if amount == locked.amount_usd:
        raise TransitionError(_("المبلغ الجديد مطابق للحالي."), code="amount_unchanged")

    # Priced at the rate this request was quoted on, not at today's. Finance
    # was asked and said so: the client transferred against a quoted figure,
    # and re-pricing after the fact changes a settled agreement. The commission
    # *is* re-prorated, because it is defined per 100 dollars and 5,000 dinars
    # of fee on a request that turned out to be $60 is not what that rate says.
    commission_rate = locked.commission_rate_applied
    if commission_rate is None:
        # Written before the rule was stored beside its answer. Recover it the
        # way the migration does, from the pair the request is holding.
        commission_rate = (
            (locked.commission_applied * 100 / locked.amount_usd)
            if locked.amount_usd
            else Decimal("0")
        )
    try:
        # The rounding is deliberately dropped here: it is recoverable from the
        # stored trio at any time (see apps.portal.payloads.rounding_iqd) and
        # the request has no column of its own to keep it in.
        _converted, commission_iqd, _rounding, total_iqd = pricing.price(
            locked.rate_applied, commission_rate, amount, locked.type
        )
    except pricing.PricingError as exc:
        raise TransitionError(exc.message, code=exc.code) from exc

    before = snapshot(locked, AMOUNT_FIELDS)

    # Filled in on the way past for anything created before the column existed,
    # so "what was asked for" is never silently the corrected figure.
    changed = ["amount_usd", "amount_iqd", "commission_applied", "updated_at"]
    if locked.submitted_amount_usd is None:
        locked.submitted_amount_usd = locked.amount_usd
        locked.submitted_amount_iqd = locked.amount_iqd
        changed += ["submitted_amount_usd", "submitted_amount_iqd"]

    locked.amount_usd = amount
    locked.amount_iqd = total_iqd
    locked.commission_applied = commission_iqd
    _stamp_operator(locked, actor, changed)
    locked.save(update_fields=sorted(set(changed)))

    sender_role = actor_role_of(actor)
    messaging.post(
        locked,
        sender_role=sender_role,
        sender_id=getattr(actor, "pk", None),
        body=_("صُحّح المبلغ من %(old)s إلى %(new)s دولار.%(why)s")
        % {
            "old": f"{before['amount_usd']}",
            "new": f"{amount:.2f}",
            "why": f" {reason.strip()}" if reason and reason.strip() else "",
        },
        # Desk business, and not a promise to the client about their money: the
        # client's own screen already shows the amount, and it has changed.
        is_internal_note=True,
    )

    record_audit(
        action=AuditAction.AMOUNT_CHANGE,
        target=locked,
        actor=actor,
        request=http_request,
        before=before,
        after={**snapshot(locked, AMOUNT_FIELDS), "reason": (reason or "").strip()},
    )
    logger.info(
        "Request %s amount corrected %s -> %s by user %s",
        locked.public_ref,
        before["amount_usd"],
        locked.amount_usd,
        getattr(actor, "pk", None),
    )
    return locked


def _stamp_operator(locked: Request, actor, changed: list) -> None:
    """Record who last moved this request, for the review Finance asked for.

    Only a real internal account. The system's own auto-route has no operator
    behind it, and attributing it to somebody would make a performance report
    say a person did something nobody did.
    """
    if actor is SYSTEM_ACTOR or getattr(actor, "pk", None) is None:
        return
    locked.handled_by = actor
    locked.handled_at = timezone.now()
    changed.extend(["handled_by", "handled_at"])


def auto_route(request_obj: Request, *, http_request=None) -> Request:
    """Route a freshly submitted deposit to the merchant the client chose.

    Spec §6 had every request reviewed before it reached anyone. Deposits no
    longer wait: the client has already transferred the money into a specific
    merchant's wallet and uploaded the receipt, so the useful next step is that
    merchant checking whether it arrived — not a desk reading a receipt it
    cannot verify either. Finance sees the request the moment it lands, watches
    it in the queue, and still holds every lever: it can reroute to another
    merchant at any time, reject at any time, and nothing reaches ``credited``
    without Finance's own approval after the merchant confirms.

    Withdrawals are untouched. Review is where the client's balance is checked
    and their B2CORE wallet debited (spec §6), and asking a merchant to pay out
    before that is how money leaves twice.

    **Best effort, and deliberately so.** The merchant the client picked was
    offerable when the wizard showed them and may not be by the time the
    submission lands — deactivated, method switched off, wallet retired. That
    is not a reason to lose the client's request. The refusal is swallowed, the
    deposit stays ``submitted``, and it appears in Finance's queue as work
    waiting on the desk, which is what it now is.

    Returns the request as it stands afterwards, routed or not.
    """
    if not request_obj.is_deposit:
        return request_obj
    try:
        return apply_transition(
            request_obj,
            "auto_route",
            actor=SYSTEM_ACTOR,
            merchant=request_obj.merchant_selected,
            http_request=http_request,
        )
    except TransitionError as exc:
        # Nested `atomic` means this rolled back to its own savepoint; the
        # submission that called us is intact.
        logger.warning(
            "Request %s could not be auto-routed to merchant %s (%s); left with Finance.",
            request_obj.public_ref,
            request_obj.merchant_selected_id,
            exc.code,
        )
        return request_obj


def _post_thread_entries(request_obj, move: Transition, *, actor, reason: str, note: str) -> None:
    """Write whatever this move owes the thread.

    Two different things, kept apart on purpose:

    * a **rejection reason**, which spec §6 requires to be posted in the thread
      — the client reads it, so it is not an internal note;
    * an optional **internal note**, which is how Finance records why it did
      something without telling the client or the merchant. It carries
      ``is_internal_note`` and every client-facing serialiser filters on that.
    """
    sender_role = actor_role_of(actor)
    sender_id = getattr(actor, "pk", None)
    if actor is SYSTEM_ACTOR:
        sender_role, sender_id = ActorRole.SYSTEM, None

    if move.requires_reason and reason:
        messaging.post(
            request_obj,
            sender_role=sender_role,
            sender_id=sender_id,
            body=reason,
            is_internal_note=move.reason_is_note,
        )

    note = (note or "").strip()
    if note:
        messaging.post(
            request_obj,
            sender_role=sender_role,
            sender_id=sender_id,
            body=note,
            is_internal_note=True,
        )


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

#: The lifecycle in order, per direction, with the timestamp each step stamps.
#: Unlike the client's timeline in :mod:`apps.portal.payloads`, this one names
#: the internal statuses, because Finance works in them.
#: ``under_review`` is not on the deposit track any more: a deposit is routed
#: to its merchant on submission and never passes through it in the ordinary
#: case. It is still a reachable status — Finance reviews a deposit by hand
#: when auto-routing could not place it — and ``track()`` handles that through
#: the same fallback it uses for any status it does not recognise, showing the
#: request as still at submission, which is where it actually is.
DEPOSIT_TRACK = [
    (RequestStatus.SUBMITTED, "submitted_at"),
    (RequestStatus.ASSIGNED, "assigned_at"),
    (RequestStatus.MERCHANT_CONFIRMED, "merchant_actioned_at"),
    (RequestStatus.CREDITED, None),
    (RequestStatus.CLOSED, "closed_at"),
]

WITHDRAWAL_TRACK = [
    (RequestStatus.SUBMITTED, "submitted_at"),
    (RequestStatus.UNDER_REVIEW, None),
    (RequestStatus.ASSIGNED, "assigned_at"),
    (RequestStatus.MERCHANT_PAID, "merchant_actioned_at"),
    (RequestStatus.CLOSED, "closed_at"),
]


#: Statuses that are not a point on the track but a thing that happened *to*
#: it. Each is shown as a step of its own appended to the end, with the state
#: the screens style it by.
OFF_TRACK_STATES = {
    RequestStatus.PENDING: "current",
    RequestStatus.REJECTED: "rejected",
    RequestStatus.CANCELLED: "rejected",
}


def track(request_obj) -> list[dict]:
    """The lifecycle as ``done`` / ``current`` / ``pending`` / ``cancelled``.

    A request that left the track keeps nothing beyond submission as done: it
    stopped somewhere, and claiming to know exactly where would be a guess. The
    audit log is where the actual sequence of events is read. That was already
    true of a rejection; the Finance review added two more ways off the track
    and neither is any more knowable than the first.
    """
    steps = DEPOSIT_TRACK if request_obj.is_deposit else WITHDRAWAL_TRACK
    order = [status for status, _stamp in steps]
    off_track = request_obj.status in OFF_TRACK_STATES
    rejected = off_track
    reached = 0 if off_track else (
        order.index(request_obj.status) if request_obj.status in order else 0
    )

    entries = []
    for index, (status, stamp) in enumerate(steps):
        if rejected:
            state = "done" if index == 0 else "cancelled"
        elif index < reached:
            state = "done"
        elif index == reached:
            state = "done" if status == RequestStatus.CLOSED else "current"
        else:
            state = "pending"
        entries.append({
            "key": status,
            "label": RequestStatus(status).label,
            "state": state,
            "at": getattr(request_obj, stamp, None) if stamp else None,
        })

    if off_track:
        status = RequestStatus(request_obj.status)
        entries.append({
            "key": status,
            "label": status.label,
            "state": OFF_TRACK_STATES[request_obj.status],
            # A parked request has not ended, so it has no `closed_at` to show.
            "at": request_obj.closed_at,
        })
    return entries
