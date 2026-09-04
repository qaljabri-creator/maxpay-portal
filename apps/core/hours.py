"""Business hours — whether the desk is open, and when that next changes.

Build-order step 11. Spec §5 gives :class:`~apps.core.models.SystemSettings`
its four fields; spec §7 says what they are *for*: "outside business hours all
submission screens are replaced by a closed notice with a live countdown to
opening".

Everything about that sentence except the notice itself is decided here, once,
so the portal, the finance panel and the submission guard can never disagree
about whether the desk is open.

Three things are worth knowing about the shape of the answer:

* **The countdown is measured by the server's clock, not the browser's.**
  :meth:`Hours.payload` sends ``seconds_until_change`` alongside the timestamps.
  A device with a clock an hour out would otherwise count down to the wrong
  moment, and the one thing a countdown must not do is disagree with the door.
* **The window may cross midnight.** ``open_time`` later than ``close_time``
  means an overnight shift — 20:00 to 02:00 — and is a normal configuration for
  a desk that follows the evening, not an error.
* **The override does not expire.** ``is_open_override`` is a switch a human
  throws and a human has to throw back. That is deliberate: an override with a
  timer is a second, invisible schedule, and the whole point of the switch is
  that it is the answer regardless of the schedule.
"""

import logging
import zoneinfo
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.utils import timezone as django_timezone

from .models import SystemSettings

logger = logging.getLogger("maxpay.audit")

#: Why the desk is in the state it is in. Sent to the client so the notice can
#: say "we are closed until 09:00" rather than "we are closed" when it knows.
REASON_OVERRIDE_OPEN = "override_open"
REASON_OVERRIDE_CLOSED = "override_closed"
REASON_ALWAYS_OPEN = "always_open"
REASON_SCHEDULE = "schedule"


@dataclass(frozen=True)
class Hours:
    """The state of the door at one instant, and when it next changes.

    ``opens_at`` and ``closes_at`` are deliberately not both filled in. Only the
    *next* change is ever known to a caller, because only the next change is
    what a countdown counts to; the other one is ``None``. When neither is set
    the state has no scheduled end — an override, or a round-the-clock window.
    """

    is_open: bool
    reason: str
    now: datetime
    opens_at: datetime | None
    closes_at: datetime | None
    open_time: time
    close_time: time
    timezone_name: str
    closed_message: str

    @property
    def changes_at(self) -> datetime | None:
        """When the current state ends, or ``None`` if it does not."""
        return self.closes_at if self.is_open else self.opens_at

    @property
    def seconds_until_change(self) -> int | None:
        moment = self.changes_at
        if moment is None:
            return None
        return max(0, int((moment - self.now).total_seconds()))

    @property
    def is_overridden(self) -> bool:
        return self.reason in {REASON_OVERRIDE_OPEN, REASON_OVERRIDE_CLOSED}

    def local(self, moment: datetime | None) -> datetime | None:
        """``moment`` in the configured business timezone, for display."""
        if moment is None:
            return None
        return moment.astimezone(zone_for(self.timezone_name))

    def payload(self) -> dict:
        """What the embed is told (spec §7).

        ``message`` only travels when the desk is shut. An open portal has no
        closed notice to render, and sending one invites a screen that shows it.
        """
        body = {
            "open": self.is_open,
            "reason": self.reason,
            "server_time": self.now.isoformat(),
            "opens_at": self.opens_at.isoformat() if self.opens_at else None,
            "closes_at": self.closes_at.isoformat() if self.closes_at else None,
            "seconds_until_change": self.seconds_until_change,
            "open_time": self.open_time.strftime("%H:%M"),
            "close_time": self.close_time.strftime("%H:%M"),
            "timezone": self.timezone_name,
            "always_open": self.reason in {REASON_ALWAYS_OPEN, REASON_OVERRIDE_OPEN},
        }
        if not self.is_open:
            body["message"] = self.closed_message
        return body


def zone_for(name: str):
    """The configured timezone, falling back to Django's rather than failing.

    A bad name is stopped at the form by ``SystemSettings.clean``, so reaching
    the fallback means the row was written around the form — by a fixture or a
    shell. Refusing to answer at all would take the whole portal down over a
    typo, so the desk stays open on the server's own clock and says so loudly
    in the log.
    """
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:
        logger.error(
            "SystemSettings.timezone=%r is not a known timezone; falling back to %s.",
            name,
            django_timezone.get_current_timezone_name(),
        )
        return django_timezone.get_current_timezone()


def _at(day: date, moment: time, zone) -> datetime:
    """A local wall-clock time on a given day, as an aware datetime."""
    return datetime.combine(day, moment, tzinfo=zone)


def evaluate(settings: SystemSettings | None = None, now: datetime | None = None) -> Hours:
    """Resolve the door's state. Pass ``now`` to ask about another instant."""
    settings = settings or SystemSettings.load()
    now = now or django_timezone.now()
    zone = zone_for(settings.timezone)
    opens, closes = settings.open_time, settings.close_time

    def state(is_open, reason, *, opens_at=None, closes_at=None) -> Hours:
        return Hours(
            is_open=is_open,
            reason=reason,
            now=now,
            opens_at=opens_at,
            closes_at=closes_at,
            open_time=opens,
            close_time=closes,
            timezone_name=settings.timezone,
            closed_message=settings.closed_message_ar,
        )

    if settings.is_open_override is True:
        return state(True, REASON_OVERRIDE_OPEN)
    if settings.is_open_override is False:
        # No opening time to count down to: it reopens when someone says so.
        return state(False, REASON_OVERRIDE_CLOSED)
    if opens == closes:
        # Equal times read as "no closing time", not "a zero-length window".
        # A desk configured 00:00–00:00 is a desk that never shuts.
        return state(True, REASON_ALWAYS_OPEN)

    local = now.astimezone(zone)
    today = local.date()
    clock = local.time()
    overnight = opens > closes

    if overnight:
        is_open = clock >= opens or clock < closes
    else:
        is_open = opens <= clock < closes

    if is_open:
        # The window that is running now ends at the next `close_time`. For an
        # overnight shift entered before midnight, that is tomorrow's.
        end_day = today if (not overnight or clock < closes) else today + timedelta(days=1)
        return state(True, REASON_SCHEDULE, closes_at=_guard(_at(end_day, closes, zone), now))

    # Closed. An overnight window can only be shut between close and open on the
    # same day, so its next opening is always today's; a daytime window's is
    # today's if the morning has not arrived yet and tomorrow's if it has passed.
    start_day = today if (overnight or clock < opens) else today + timedelta(days=1)
    return state(False, REASON_SCHEDULE, opens_at=_guard(_at(start_day, opens, zone), now))


def _guard(moment: datetime, now: datetime) -> datetime:
    """Keep the next change in the future.

    The arithmetic above works in wall-clock time, which a daylight-saving shift
    can move under it. Asia/Baghdad has had no DST since 2008, so this is only
    reachable if Finance points the desk at a zone that does — and a countdown
    that runs backwards is worse than one that is an hour long.
    """
    return moment if moment > now else moment + timedelta(days=1)


def is_open(now: datetime | None = None) -> bool:
    """Whether submissions are being accepted right now (spec §7)."""
    return evaluate(now=now).is_open
