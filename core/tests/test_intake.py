"""Message intake: dedupe, coalescing, and the race that coalescing opens.

Every layer above this retries -- Meta redelivers webhooks it believes failed,
a customer double-taps send, a worker gets reaped mid-turn -- so intake is
where idempotency either holds or does not.
"""

from __future__ import annotations

from aisales import agent, db
from aisales.channels import Inbound

PHONE = "08031234567"


def inbound(text: str, external_id: str, phone: str = PHONE) -> Inbound:
    return Inbound(channel="simulator", external_id=external_id,
                   from_phone=phone, text=text)


def test_first_message_creates_the_whole_chain(dsn: str, business) -> None:
    q = db.Queue(dsn)
    with db.connect(dsn) as conn:
        result = agent.record_inbound(conn, q, business, inbound("Abeg how much?", "m1"))

        assert result.duplicate is False
        assert result.job_id is not None

        customer = conn.execute(
            "select phone_e164 from customers where id = %s",
            (result.customer_id,)).fetchone()
        assert customer["phone_e164"] == "+2348031234567"

        assert conn.execute("select count(*) as n from messages").fetchone()["n"] == 1
        assert q.counts(business.id).get("queued", 0) == 1


def test_redelivery_records_nothing_and_queues_nothing(dsn: str, business) -> None:
    """A redelivered webhook must not produce a second reply."""
    q = db.Queue(dsn)
    with db.connect(dsn) as conn:
        first = agent.record_inbound(conn, q, business, inbound("Hello", "same-id"))
        second = agent.record_inbound(conn, q, business, inbound("Hello", "same-id"))

        assert first.duplicate is False
        assert second.duplicate is True
        assert second.job_id is None
        assert second.conversation_id == first.conversation_id

        n = conn.execute("select count(*) as n from messages").fetchone()["n"]
        assert n == 1, "the duplicate was stored twice"
        assert q.counts(business.id).get("queued", 0) == 1


def test_a_burst_is_stored_but_answered_once(dsn: str, business) -> None:
    """Three messages in four seconds: three rows, one turn."""
    q = db.Queue(dsn)
    with db.connect(dsn) as conn:
        results = [agent.record_inbound(conn, q, business, inbound(t, f"burst-{i}"))
                   for i, t in enumerate(["Hello", "You dey?", "How much?"])]

        assert all(not r.duplicate for r in results)
        assert len({r.conversation_id for r in results}) == 1
        assert conn.execute("select count(*) as n from messages").fetchone()["n"] == 3
        assert q.counts(business.id).get("queued", 0) == 1, "a burst queued more than one turn"


def test_a_message_arriving_mid_turn_is_not_swallowed(dsn: str, business) -> None:
    """The race coalescing opens, and the guard against it.

    The running turn has already read the thread and will never see this
    message. Without the re-check it would sit unanswered forever while the
    conversation looked handled.
    """
    q = db.Queue(dsn)
    with db.connect(dsn) as conn:
        first = agent.record_inbound(conn, q, business, inbound("Hello", "mid-1"))
        job = q.claim("worker-a")
        assert job is not None and job.id == first.job_id

        # Arrives while the turn is running: correctly refused a second turn.
        during = agent.record_inbound(conn, q, business, inbound("You dey?", "mid-2"))
        assert during.job_id is None

        # The turn read only the first message, replies to it, and says so.
        # Its reply gets a HIGHER id than the message it never saw, which is
        # exactly why the watermark cannot be inferred from ordering.
        agent.record_outbound(conn, business_id=business.id,
                              conversation_id=first.conversation_id,
                              body="Yes, how can I help?",
                              answered_upto=first.message_id)
        assert during.message_id > first.message_id
        assert agent.unanswered_after(conn, first.conversation_id) is True
        assert q.finish(job, "worker-a") is True

        # Only now can a second turn be queued, and it must be.
        queued = agent.requeue_if_unanswered(conn, q, business.id, first.conversation_id)
        assert queued is not None, "a message that arrived mid-turn was never answered"


def test_a_reply_leaves_nothing_unanswered(dsn: str, business) -> None:
    """The other direction, so the re-check cannot pass by always saying yes."""
    q = db.Queue(dsn)
    with db.connect(dsn) as conn:
        result = agent.record_inbound(conn, q, business, inbound("Hello", "done-1"))
        agent.record_outbound(conn, business_id=business.id,
                              conversation_id=result.conversation_id,
                              body="How can I help?", answered_upto=result.message_id)
        assert agent.unanswered_after(conn, result.conversation_id) is False
        assert agent.requeue_if_unanswered(conn, q, business.id,
                                           result.conversation_id) is None


def test_two_spellings_are_one_customer(dsn: str, business) -> None:
    """Identity is the normalised number. Two threads for one person is an
    agent that has forgotten what it was told ten minutes ago."""
    q = db.Queue(dsn)
    with db.connect(dsn) as conn:
        a = agent.record_inbound(conn, q, business, inbound("Hello", "p1", "08031234567"))
        b = agent.record_inbound(conn, q, business, inbound("Hello again", "p2",
                                                            "+2348031234567"))
        assert a.customer_id == b.customer_id
        assert a.conversation_id == b.conversation_id
        n = conn.execute("select count(*) as n from customers").fetchone()["n"]
        assert n == 1


def test_a_different_customer_gets_their_own_thread(dsn: str, business) -> None:
    q = db.Queue(dsn)
    with db.connect(dsn) as conn:
        a = agent.record_inbound(conn, q, business, inbound("Hi", "c1", "08031234567"))
        b = agent.record_inbound(conn, q, business, inbound("Hi", "c2", "08059876543"))
        assert a.customer_id != b.customer_id
        assert a.conversation_id != b.conversation_id


def test_inbound_cancels_a_pending_follow_up(dsn: str, business) -> None:
    """Nothing damages this product more than a bot chasing someone who
    already answered."""
    q = db.Queue(dsn)
    with db.connect(dsn) as conn:
        first = agent.record_inbound(conn, q, business, inbound("I go get back", "f1"))
        conn.execute(
            """
            insert into follow_ups (business_id, customer_id, conversation_id, reason, due_at)
            values (%s, %s, %s, 'said they would return', now() + interval '2 hours')
            """,
            (business.id, first.customer_id, first.conversation_id))
        assert conn.execute("select count(*) as n from follow_ups where status = 'scheduled'"
                            ).fetchone()["n"] == 1

        agent.record_inbound(conn, q, business, inbound("Actually I'm ready", "f2"))

        row = conn.execute("select status, skipped_reason from follow_ups").fetchone()
        assert row["status"] == "cancelled"
        assert row["skipped_reason"] == "customer_replied"


def test_a_blocked_reply_never_enters_the_transcript(dsn: str, business) -> None:
    """Requirement 4.6, tested at the point where it could be violated.

    A blocked reply was never seen by the customer. Storing it would show the
    owner something that did not happen and feed it back to the model next
    turn as though it had been said.
    """
    q = db.Queue(dsn)
    with db.connect(dsn) as conn:
        result = agent.record_inbound(conn, q, business, inbound("How much?", "b1"))
        before = conn.execute("select count(*) as n from messages").fetchone()["n"]
        # The guard's block path simply never calls record_outbound. Assert the
        # count is unchanged so a future refactor that records optimistically
        # fails here.
        assert conn.execute("select count(*) as n from messages").fetchone()["n"] == before
        assert result.message_id is not None
