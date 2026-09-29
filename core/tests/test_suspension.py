"""Suspending a business, and what happens when it is switched back on.

Suspension is a commercial lever — a lapsed subscription, a dispute — so the
property that matters is not "the agent stops" but **where** it stops. There
are four places a turn can begin, and a suspension that misses one is worse
than no suspension at all: it looks off and quietly isn't.

    1. intake        a message arrives     -> store it, queue nothing
    2. worker        a job was already queued before the flag flipped
    3. run_turn      the last cheap stop before a model call is paid for
    4. follow-up     the one path that reaches a customer unprompted

The other half is resume. Catch-up is bounded by WhatsApp's 24-hour service
window, which is Meta's rule rather than a preference: a reply outside it is
refused with error 131047, so queueing one there is queueing a guaranteed
failure. That boundary is tested in both directions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from aisales import agent, db, followup, providers
from aisales.channels import Inbound, Outbound

PHONE = "+2348031234567"


class RecordingChannel:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def verify(self, body, headers) -> bool:
        return True

    def parse(self, body, headers):
        return []

    def send(self, message: Outbound) -> str:
        self.sent.append(message.text)
        return f"rec-{len(self.sent)}"


def _inbound(text: str, external: str, phone: str = PHONE) -> Inbound:
    return Inbound(channel="simulator", external_id=external, from_phone=phone, text=text)


def _suspend(dsn: str, business_id: str, *, reason: str = "unpaid") -> dict:
    with db.tenant(dsn, business_id) as conn:
        return agent.suspend_business(conn, business_id, reason=reason)


def _scripted(count: int = 1) -> providers.ScriptedChat:
    return providers.ScriptedChat(turns=[
        providers.Turn(text="Yes, how can I help?", model="scripted", finish_reason="stop")
    ] * count)


# ---------------------------------------------------------------- 1. intake


def test_a_message_while_suspended_is_stored_but_not_answered(dsn, business) -> None:
    """The direct analogue of the takeover test, and for the same reason.

    Dropping the message would lose a customer's words that Meta has already
    been told we accepted. Storing it and staying quiet keeps both promises.
    """
    q = db.Queue(dsn)
    with db.tenant(dsn, business.id) as conn:
        agent.suspend_business(conn, business.id, reason="unpaid")
        result = agent.record_inbound(conn, q, _reload(conn, business.id),
                                      _inbound("Abeg how much?", "sus-1"))

        assert result.message_id is not None, "the message was not stored"
        assert result.job_id is None, "a turn was queued for a suspended business"
        assert conn.execute("select count(*) as n from messages").fetchone()["n"] == 1

        conversation = conn.execute(
            "select needs_attention, attention_reason from conversations").fetchone()
        assert conversation["needs_attention"] is True
        assert "paused" in conversation["attention_reason"]

        audit = conn.execute(
            "select action from audit_log order by id desc limit 1").fetchone()
        assert audit["action"] == "suspended_suppressed_turn"


def test_an_ordinary_message_queues_a_turn_when_not_suspended(dsn, business) -> None:
    """The other direction, so the test above cannot pass by never queueing."""
    q = db.Queue(dsn)
    with db.tenant(dsn, business.id) as conn:
        result = agent.record_inbound(conn, q, _reload(conn, business.id),
                                      _inbound("Hello", "ok-1"))
        assert result.job_id is not None


# ---------------------------------------------------------------- 2. worker


def test_a_job_queued_before_the_flag_flipped_does_not_run(dsn, business) -> None:
    """Suspension is not a race the queue can win.

    The job was already queued when the flag went on, so intake never saw it.
    Without this check the turn runs to completion and the customer is answered
    by a business that is supposed to be off.
    """
    from aisales_worker.runner import Worker  # noqa: PLC0415 - separate package

    q = db.Queue(dsn)
    q.submit("agent_turn", business_id=business.id)
    _suspend(dsn, business.id)

    channel = RecordingChannel()
    worker = Worker(dsn=dsn, channel=channel, chat=_scripted())
    assert worker.run_once() is True, "the job was never claimed"

    assert channel.sent == [], "a suspended business answered a queued turn"
    # Reached a terminal state rather than being retried forever: the job is
    # claimed once, skipped, and finished.
    counts = q.counts(business.id)
    assert counts.get("succeeded", 0) + counts.get("failed", 0) == 1, counts
    assert counts.get("queued", 0) == 0, f"the job is still being retried: {counts}"


# -------------------------------------------------------------- 3. run_turn


def test_run_turn_refuses_even_when_called_directly(dsn, business) -> None:
    """The last stop before a model call is paid for.

    Belt and braces on purpose: the other two checks are at the edges, and
    this is the one that holds if a new caller appears that bypasses both.
    """
    q = db.Queue(dsn)
    with db.tenant(dsn, business.id) as conn:
        result = agent.record_inbound(conn, q, _reload(conn, business.id),
                                      _inbound("Hello", "rt-1"))
        assert result.message_id is not None, "need a stored message to answer"

        agent.suspend_business(conn, business.id)
        channel = RecordingChannel()
        ctx = db.TurnContext(business=_reload(conn, business.id),
                             customer_id=result.customer_id,
                             conversation_id=result.conversation_id,
                             channel="simulator")
        turn = agent.run_turn(conn, channel, _scripted(), ctx, queue=q)
        assert turn.silent and turn.reason == "the business is suspended"
        assert channel.sent == []


# ------------------------------------------------------------- 4. follow-ups


def test_a_scheduled_follow_up_is_cancelled_by_suspension(dsn, business) -> None:
    """The only path that reaches a customer without a message arriving first,
    so the only one where suspending has to reach *forward* in time."""
    q = db.Queue(dsn)
    with db.tenant(dsn, business.id) as conn:
        run = agent.record_inbound(conn, q, _reload(conn, business.id),
                                   _inbound("I go get back", "fu-1"))
        conn.execute(
            """
            insert into follow_ups (business_id, customer_id, conversation_id, reason, due_at)
            values (%s, %s, %s, 'said they would return', now() + interval '2 hours')
            """,
            (business.id, run.customer_id, run.conversation_id))

        outcome = agent.suspend_business(conn, business.id, reason="unpaid")
        assert outcome["cancelled_follow_ups"] == 1

        row = conn.execute("select status, skipped_reason from follow_ups").fetchone()
        assert row["status"] == "cancelled"
        assert row["skipped_reason"] == followup.SkipReason.BUSINESS_SUSPENDED


def test_a_follow_up_that_survives_to_dispatch_is_skipped(dsn, business) -> None:
    """A job already queued when the flag flipped, reaching the dispatcher."""
    q = db.Queue(dsn)
    with db.tenant(dsn, business.id) as conn:
        run = agent.record_inbound(conn, q, _reload(conn, business.id),
                                   _inbound("I go get back", "fu-2"))
        follow_up = conn.execute(
            """
            insert into follow_ups (business_id, customer_id, conversation_id, reason, due_at)
            values (%s, %s, %s, 'said they would return', now()) returning id
            """,
            (business.id, run.customer_id, run.conversation_id)).fetchone()

        # Suspended directly, without going through suspend_business, so the
        # follow-up is still 'scheduled' when the dispatcher sees it.
        conn.execute("update businesses set suspended_at = now() where id = %s",
                     (business.id,))

        job = db.Job(id=str(uuid.uuid4()), business_id=business.id, kind=agent.FOLLOW_UP,
                     conversation_id=run.conversation_id,
                     payload={"follow_up_id": str(follow_up["id"])},
                     attempt=1, max_attempts=4, not_before=None)
        channel = RecordingChannel()
        outcome = agent.dispatch_follow_up(conn, q, job, chat=_scripted(),
                                           channel=channel)

        assert outcome == "business_suspended"
        assert channel.sent == []
        row = conn.execute("select status, skipped_reason from follow_ups").fetchone()
        assert row["status"] == "skipped"
        assert row["skipped_reason"] == followup.SkipReason.BUSINESS_SUSPENDED


# ----------------------------------------------------------------- resume


def test_resume_catches_up_a_thread_still_inside_the_window(dsn, business) -> None:
    """The simulator has no service window, so this is the ordinary case."""
    q = db.Queue(dsn)
    with db.tenant(dsn, business.id) as conn:
        run = agent.record_inbound(conn, q, _reload(conn, business.id),
                                   _inbound("Abeg how much?", "res-1"))
        assert run.job_id is not None
        conn.execute("delete from jobs")           # clear what intake queued
        agent.suspend_business(conn, business.id)

        outcome = agent.resume_business(conn, q, business.id)
        assert outcome["caught_up"] == 1, "the missed message was never picked up"
        assert outcome["outside_window"] == 0

    # Read after the block: `db.tenant` is a transaction, and `counts` opens a
    # connection of its own, so it cannot see rows that have not committed yet.
    assert q.counts(business.id).get("queued", 0) == 1


def test_resume_leaves_a_thread_outside_the_window_for_a_person(dsn, business) -> None:
    """Meta's rule, not a preference: outside 24 hours a free-form reply is
    refused with 131047, so queueing one is queueing a guaranteed failure."""
    q = db.Queue(dsn)
    with db.tenant(dsn, business.id) as conn:
        run = agent.record_inbound(conn, q, _reload(conn, business.id),
                                   _inbound("Abeg how much?", "res-2"))
        assert run.job_id is not None
        conn.execute("delete from jobs")
        agent.suspend_business(conn, business.id)

        # Make the thread a WhatsApp one whose last inbound is two days old.
        conn.execute(
            "update conversations set channel = 'whatsapp', "
            "last_inbound_at = now() - interval '2 days' where id = %s",
            (run.conversation_id,))

        outcome = agent.resume_business(conn, q, business.id)
        assert outcome["caught_up"] == 0
        assert outcome["outside_window"] == 1

    # Outside the block, so this is a real zero rather than an empty read of
    # an uncommitted transaction.
    assert q.counts(business.id).get("queued", 0) == 0


def test_resume_does_not_answer_a_thread_the_customer_already_resolved(dsn, business) -> None:
    """Catch-up keys on the unanswered watermark, not on the clock."""
    q = db.Queue(dsn)
    with db.tenant(dsn, business.id) as conn:
        run = agent.record_inbound(conn, q, _reload(conn, business.id),
                                   _inbound("Thanks!", "res-3"))
        agent.record_outbound(conn, business_id=business.id,
                              conversation_id=run.conversation_id,
                              body="You are welcome.", answered_upto=run.message_id)
        conn.execute("delete from jobs")
        agent.suspend_business(conn, business.id)

        outcome = agent.resume_business(conn, q, business.id)
        assert outcome["caught_up"] == 0


def test_resume_clears_the_flag_and_records_both_transitions(dsn, business) -> None:
    with db.tenant(dsn, business.id) as conn:
        agent.suspend_business(conn, business.id, reason="unpaid")
        assert _reload(conn, business.id).suspended is True

        agent.resume_business(conn, db.Queue(dsn), business.id)
        reloaded = _reload(conn, business.id)
        assert reloaded.suspended is False
        assert reloaded.suspended_reason is None

        actions = [r["action"] for r in conn.execute(
            "select action from audit_log order by id").fetchall()]
    assert "business_suspended" in actions
    assert "business_resumed" in actions


# ------------------------------------------------------------ the window rule


@pytest.mark.parametrize("channel,age_hours,expected", [
    ("whatsapp", 2, True),
    ("whatsapp", 23, True),
    ("whatsapp", 25, False),
    ("whatsapp", 200, False),
    # No window at all: this is what makes the whole flow demonstrable offline.
    ("simulator", 200, True),
])
def test_the_service_window(channel, age_hours, expected) -> None:
    now = datetime.now(timezone.utc)
    assert followup.within_service_window(
        channel, now - timedelta(hours=age_hours), now=now) is expected


def _reload(conn, business_id: str) -> db.Business:
    """Re-read the business, so `suspended` reflects what was just written."""
    return agent.business_by_id(conn, business_id)
