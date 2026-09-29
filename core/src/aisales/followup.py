"""Scheduled follow-ups.

The timer is `jobs.not_before`. There is no cron, no sleeping worker and no
second scheduler to keep consistent with the first: a follow-up due in two
hours is queued now and simply is not claimable until then.

The constraint that shapes everything here is WhatsApp's service window. Meta
permits free-form messages only within 24 hours of the customer's last inbound
message; outside it, only a pre-approved template may be sent and ours is
refused with error 131047. So a follow-up that would land outside the window is
skipped and recorded, not queued hopefully. That is also why the default delay
is 120 minutes rather than "tomorrow" -- a next-day default would produce
follow-ups Meta silently rejects, and a feature that quietly does nothing is
worse than one that is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg

LAGOS = ZoneInfo("Africa/Lagos")

#: Beyond this, the service window has closed.
SERVICE_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class FollowUpPolicy:
    """Business-controlled timing, frequency and tone -- Requirement 7.2.

    All three of the vision's follow-up controls land in one dataclass read in
    one place, which is what makes them testable as a policy rather than as
    three behaviours scattered through the codebase.
    """

    first_after_minutes: int = 120
    min_gap_minutes: int = 1440
    max_attempts: int = 2
    quiet_start: str = "21:00"
    quiet_end: str = "08:00"
    tone: str = "warm, brief"

    @classmethod
    def from_settings(cls, settings: dict) -> "FollowUpPolicy":
        raw = settings.get("followup") or {}
        base = cls()
        return cls(
            first_after_minutes=int(raw.get("first_after_minutes",
                                            base.first_after_minutes)),
            min_gap_minutes=int(raw.get("min_gap_minutes", base.min_gap_minutes)),
            max_attempts=int(raw.get("max_attempts", base.max_attempts)),
            quiet_start=str(raw.get("quiet_start", base.quiet_start)),
            quiet_end=str(raw.get("quiet_end", base.quiet_end)),
            tone=str(raw.get("tone", base.tone)),
        )


def within_service_window(channel: str, last_inbound_at: datetime | None, *,
                          now: datetime | None = None) -> bool:
    """May we send this conversation free-form text right now?

    The rule lives here rather than beside each caller because it is Meta's,
    not ours: a free-form message more than 24 hours after the customer's last
    inbound is refused with error 131047, so anything that sends must ask.

    Only WhatsApp has the constraint. The simulator has no window, which is
    what makes the whole flow demonstrable offline -- see
    `test_the_window_does_not_apply_to_the_simulator`.
    """
    if channel != "whatsapp":
        return True
    if last_inbound_at is None:
        return False
    return (now or datetime.now(timezone.utc)) <= last_inbound_at + SERVICE_WINDOW


def _at(window: str, on: datetime) -> datetime:
    hour, _, minute = window.partition(":")
    return on.replace(hour=int(hour), minute=int(minute or 0),
                      second=0, microsecond=0)


def in_quiet_hours(policy: FollowUpPolicy, moment: datetime) -> bool:
    """Whether *moment* falls in the business's quiet hours, in Lagos time.

    Computed with a real timezone rather than a hardcoded +1. Lagos has no DST
    today, which is exactly why someone will hardcode it and be wrong the day
    that changes.
    """
    local = moment.astimezone(LAGOS)
    start = _at(policy.quiet_start, local)
    end = _at(policy.quiet_end, local)
    if start <= end:
        return start <= local < end
    # Quiet hours that cross midnight, e.g. 21:00 to 08:00 -- the normal case.
    return local >= start or local < end


def next_open_moment(policy: FollowUpPolicy, moment: datetime) -> datetime:
    """The first moment at or after *moment* that is not quiet."""
    if not in_quiet_hours(policy, moment):
        return moment
    local = moment.astimezone(LAGOS)
    wake = _at(policy.quiet_end, local)
    if wake <= local:
        wake += timedelta(days=1)
    return wake.astimezone(timezone.utc)


class SkipReason:
    WINDOW_CLOSED = "window_closed"
    CAPPED = "max_attempts"
    HUMAN = "human_owns_it"
    CUSTOMER_REPLIED = "customer_replied"
    ALREADY_SCHEDULED = "already_scheduled"
    BUSINESS_SUSPENDED = "business_suspended"


def schedule(conn: psycopg.Connection, queue, *, business_id: str,
             customer_id: str, conversation_id: str, reason: str,
             policy: FollowUpPolicy, last_inbound_at: datetime | None,
             channel: str = "simulator",
             in_minutes: int | None = None) -> tuple[str | None, str | None]:
    """Arrange one follow-up. Returns (follow_up_id, skip_reason).

    Exactly one of the two is set. A skip is a normal outcome and is recorded
    with its reason rather than being dropped, because "why did nothing
    happen?" is the question this feature will be asked.
    """
    attempts = conn.execute(
        """
        select count(*) as n from follow_ups
         where conversation_id = %s and status in ('scheduled', 'sent')
        """,
        (conversation_id,),
    ).fetchone()["n"]
    if attempts >= policy.max_attempts:
        return None, SkipReason.CAPPED

    now = datetime.now(timezone.utc)
    due_at = now + timedelta(minutes=in_minutes or policy.first_after_minutes)

    # Respect the gap since the last one actually sent.
    last_sent = conn.execute(
        """
        select due_at from follow_ups
         where conversation_id = %s and status = 'sent'
         order by due_at desc limit 1
        """,
        (conversation_id,),
    ).fetchone()
    if last_sent:
        earliest = last_sent["due_at"] + timedelta(minutes=policy.min_gap_minutes)
        due_at = max(due_at, earliest)

    due_at = next_open_moment(policy, due_at)

    # The service window is measured from the customer's last inbound message,
    # and only applies to channels that have one.
    if channel == "whatsapp" and last_inbound_at is not None:
        if due_at > last_inbound_at + SERVICE_WINDOW:
            return None, SkipReason.WINDOW_CLOSED

    row = conn.execute(
        """
        insert into follow_ups (business_id, customer_id, conversation_id,
                                reason, due_at)
        values (%s, %s, %s, %s, %s)
        on conflict (conversation_id) where status = 'scheduled' do nothing
        returning id
        """,
        (business_id, customer_id, conversation_id, reason, due_at),
    ).fetchone()
    if row is None:
        return None, SkipReason.ALREADY_SCHEDULED

    follow_up_id = str(row["id"])
    job_id = queue.submit("follow_up", business_id=business_id,
                          conversation_id=conversation_id,
                          payload={"follow_up_id": follow_up_id},
                          not_before=due_at,
                          idempotency_key=f"followup:{follow_up_id}",
                          conn=conn)
    conn.execute("update follow_ups set job_id = %s where id = %s",
                 (job_id, follow_up_id))
    return follow_up_id, None
