"""Who has seen what, and therefore what is unread (spec §10) — step 13.

Spec §10 asks for an unread badge on the merchant panel and a badge on the
Finance panel that rises when a submission or a merchant confirmation lands.
Both are the same question — *has anything happened on this request since I
last looked at it?* — so both are answered here, once.

**What counts as "something happened" depends on who is asking**, and that is
the whole of the design:

===========  =======================================  ==========================
audience     the event that starts the clock          the messages that count
===========  =======================================  ==========================
merchant     being routed the request (``assigned``)  everyone else's, notes
                                                      excluded
finance      the submission, and the merchant's move  the client's and the
                                                      merchant's
===========  =======================================  ==========================

Neither audience is woken by its own writing. A merchant who replies has not
given themselves something to read, and Finance's own notes are Finance's.

**A read marker is per user, not per role.** Two Finance staff work the same
queue and each has their own idea of what they have looked at; collapsing that
into one desk-wide marker would mean whoever opened a request first silently
cleared the badge for everybody.

The marker is written when someone opens the request's detail screen, which is
the only moment the system can honestly claim they saw it.
"""

from django.db.models import (
    Case,
    DateTimeField,
    F,
    Max,
    OuterRef,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Greatest

from apps.core.choices import ActorRole

from .models import Message, Request, RequestRead

#: Audiences, matching :mod:`apps.transactions.messaging`'s vocabulary.
MERCHANT = "merchant"
FINANCE = "finance"

#: Whose messages wake each audience. Whitelists, like the visibility rule they
#: mirror: a sender role added later is silent until somebody decides otherwise.
INCOMING_SENDERS = {
    MERCHANT: frozenset({
        ActorRole.CLIENT,
        ActorRole.FINANCE_ADMIN,
        ActorRole.FINANCE_STAFF,
        ActorRole.SYSTEM,
    }),
    FINANCE: frozenset({ActorRole.CLIENT, ActorRole.MERCHANT}),
}


def _base_event(audience: str):
    """The lifecycle moment that makes a request unread before anyone writes.

    Never null: ``submitted_at`` is ``auto_now_add`` and both expressions fall
    back to it, which matters because ``Greatest`` is null-propagating on every
    backend we support.
    """
    if audience == MERCHANT:
        # Spec §10: "new assigned request appears ... with an unread badge".
        # The badge is on the arrival itself, before a word has been written.
        return Coalesce("assigned_at", "submitted_at")
    # Spec §10: a new submission and a merchant confirmation both raise it.
    return Coalesce("merchant_actioned_at", "submitted_at")


def annotate_activity(queryset, *, user, audience: str):
    """Add ``last_activity_at``, ``seen_at`` and ``is_unread`` to a queryset.

    Three subqueries' worth of work, done in SQL rather than in Python because
    the Finance queue counts over every open request and the poll runs every ten
    seconds. The comparison itself is a boolean annotation so the caller can
    ``.filter(is_unread=True)`` and let the database count.
    """
    incoming = (
        Message.objects.filter(
            request=OuterRef("pk"),
            is_internal_note=False,
            sender_role__in=INCOMING_SENDERS[audience],
        )
        .order_by("-created_at", "-id")
        .values("created_at")[:1]
    )
    seen = RequestRead.objects.filter(request=OuterRef("pk"), user=user).values(
        "seen_at"
    )[:1]

    base = _base_event(audience)
    return queryset.annotate(
        last_incoming_at=Subquery(incoming, output_field=DateTimeField()),
        seen_at=Subquery(seen, output_field=DateTimeField()),
    ).annotate(
        # Coalesce before Greatest: on both PostgreSQL and SQLite a null
        # argument makes the whole comparison null, which would read as "never
        # active" — the opposite of the truth for a request nobody has written
        # on yet.
        last_activity_at=Greatest(
            base, Coalesce("last_incoming_at", base), output_field=DateTimeField()
        ),
        is_unread=Case(
            # No marker at all means never opened, which is unread by
            # definition — including a request that arrived while the merchant
            # was not looking.
            When(seen_at__isnull=True, then=Value(True)),
            When(seen_at__lt=F("last_activity_at"), then=Value(True)),
            default=Value(False),
        ),
    )


def unread_count(*, user, audience: str, queryset=None) -> int:
    """How many of ``queryset``'s requests have unseen activity for ``user``."""
    queryset = Request.objects.all() if queryset is None else queryset
    return annotate_activity(queryset, user=user, audience=audience).filter(
        is_unread=True
    ).count()


def unread_references(*, user, audience: str, requests) -> set[str]:
    """The ``public_ref`` of every unread request among ``requests``.

    Takes an iterable of rows rather than a queryset, because the caller is
    almost always holding one page of a paginator — and a sliced queryset
    cannot be filtered again. The primary keys are re-queried instead, which is
    one extra statement and the only shape that works for a page, a list, and a
    plain queryset alike.

    Returned as references rather than primary keys because that is what the
    surfaces speak: a merchant never sees a database id (spec §2), and the
    Finance queue is keyed on the reference too.
    """
    ids = [row.pk for row in requests]
    if not ids:
        return set()
    rows = annotate_activity(
        Request.objects.filter(pk__in=ids), user=user, audience=audience
    ).filter(is_unread=True)
    return set(rows.values_list("public_ref", flat=True))


def mark_seen(*, user, request_obj) -> None:
    """Record that ``user`` has now looked at ``request_obj``.

    ``update_or_create`` rather than ``get_or_create``: ``seen_at`` is
    ``auto_now``, so the update has to actually save the row for the timestamp
    to move. Called from the detail screens and nowhere else — the queue lists a
    request, it does not show it.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return
    RequestRead.objects.update_or_create(user=user, request=request_obj)


def latest_activity(*, user, audience: str, queryset) -> str:
    """A cheap change token for the poll.

    The panel asks "has anything moved?" six times a minute. Comparing one
    string is enough to answer it, and re-rendering only when the string
    changes is what keeps a ten-second poll from costing a page render every
    ten seconds.

    Built from the newest activity timestamp *and* the row count, because a
    request leaving the filtered set — closed, or rerouted away — moves no
    timestamp forward but does change what the screen should show.
    """
    rows = annotate_activity(queryset, user=user, audience=audience).aggregate(
        newest=Max("last_activity_at")
    )
    newest = rows["newest"]
    return f"{newest.isoformat() if newest else '-'}:{queryset.count()}"
