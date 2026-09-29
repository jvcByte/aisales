"""Human takeover: who may reply, and what happens on the way back.

The two failures this file exists to catch are both invisible in normal use
until a shop loses a customer over them: the AI talking over a person, and the
AI resuming after a handback and contradicting what the person promised.
"""

from __future__ import annotations

import pytest

from aisales import agent, db, providers
from aisales.channels import Inbound
from aisales.db import TurnContext

PHONE = "+2348031234567"


class Chan:
    name = "test"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def verify(self, b, h): return True

    def parse(self, b, h): return []

    def send(self, m):
        self.sent.append(m.text)
        return "x"


@pytest.fixture
def setup(dsn: str, business):
    with db.connect(dsn) as conn:
        result = agent.record_inbound(conn, db.Queue(dsn), business, Inbound(
            channel="simulator", external_id="h-1", from_phone=PHONE,
            text="I want a refund"))
        conn.execute("update jobs set status = 'succeeded', finished_at = now()")
    return {"business": business, "conversation": result.conversation_id,
            "customer": result.customer_id, "queue": db.Queue(dsn)}


def _status(dsn, conversation_id) -> str:
    with db.connect(dsn) as conn:
        return conn.execute("select status from conversations where id = %s",
                            (conversation_id,)).fetchone()["status"]


def _inbound(dsn, setup, text: str, external_id: str):
    with db.connect(dsn) as conn:
        return agent.record_inbound(conn, db.Queue(dsn), setup["business"],
                                    Inbound(channel="simulator", external_id=external_id,
                                            from_phone=PHONE, text=text))


# ---------------------------------------------------------------- takeover


def test_takeover_stops_the_ai(dsn, setup) -> None:
    with db.connect(dsn) as conn:
        agent.take_over(conn, setup["conversation"], "Ada")
    assert _status(dsn, setup["conversation"]) == "human"

    chat = providers.ScriptedChat(turns=[providers.scripted_turn(text="I should not say this")])
    channel = Chan()
    with db.connect(dsn) as conn:
        ctx = TurnContext(business=setup["business"], customer_id=setup["customer"],
                          conversation_id=setup["conversation"], channel="simulator")
        result = agent.run_turn(conn, channel, chat, ctx, queue=db.Queue(dsn))
    assert result.silent and channel.sent == []


def test_takeover_records_who_and_when(dsn, setup) -> None:
    with db.connect(dsn) as conn:
        agent.take_over(conn, setup["conversation"], "Ada")
        row = conn.execute("select assigned_to, takeover_at, needs_attention "
                           "from conversations").fetchone()
        actions = [r["action"] for r in conn.execute("select action from audit_log")]
    assert row["assigned_to"] == "Ada"
    assert row["takeover_at"] is not None
    assert row["needs_attention"] is False, "taking over should clear the flag"
    assert "takeover" in actions


def test_takeover_of_a_missing_conversation_raises(dsn, setup) -> None:
    with db.connect(dsn) as conn:
        with pytest.raises(LookupError):
            agent.take_over(conn, "00000000-0000-0000-0000-000000000000")


def test_a_message_while_a_person_owns_it_is_stored_but_not_answered(dsn, setup) -> None:
    """The customer must not be ignored, and the AI must not answer. Both."""
    with db.connect(dsn) as conn:
        agent.take_over(conn, setup["conversation"], "Ada")
    result = _inbound(dsn, setup, "Hello? Are you there?", "h-2")

    assert result.job_id is None, "a turn was queued while a person owned the thread"
    with db.connect(dsn) as conn:
        bodies = [r["body"] for r in conn.execute("select body from messages order by id")]
        actions = [r["action"] for r in conn.execute("select action from audit_log")]
    assert "Hello? Are you there?" in bodies
    assert "takeover_suppressed_turn" in actions


# ----------------------------------------------------------- a person replying


def test_a_staff_reply_is_delivered_and_recorded_as_staff(dsn, setup) -> None:
    with db.connect(dsn) as conn:
        agent.take_over(conn, setup["conversation"], "Ada")
    channel = Chan()
    with db.connect(dsn) as conn:
        agent.staff_reply(conn, channel, setup["conversation"], "I'll process that refund now.")

    assert channel.sent == ["I'll process that refund now."]
    with db.connect(dsn) as conn:
        row = conn.execute("select role from messages order by id desc limit 1").fetchone()
    assert row["role"] == "staff"


def test_a_staff_reply_is_refused_while_the_ai_owns_the_thread(dsn, setup) -> None:
    """Two voices answering one customer is the failure ownership exists to
    prevent."""
    with db.connect(dsn) as conn:
        with pytest.raises(PermissionError):
            agent.staff_reply(conn, Chan(), setup["conversation"], "hello")


def test_an_empty_staff_reply_is_refused(dsn, setup) -> None:
    with db.connect(dsn) as conn:
        agent.take_over(conn, setup["conversation"], "Ada")
        with pytest.raises(ValueError):
            agent.staff_reply(conn, Chan(), setup["conversation"], "   ")


def test_staff_messages_are_in_the_models_history(dsn, setup) -> None:
    """The contradiction fix. On handback the model must see what the person
    promised, as its own prior words, or it resumes and contradicts them."""
    with db.connect(dsn) as conn:
        agent.take_over(conn, setup["conversation"], "Ada")
        agent.staff_reply(conn, Chan(), setup["conversation"],
                          "I can offer you a full refund.")
        agent.hand_back(conn, setup["queue"], setup["conversation"])
        thread = agent.window(conn, setup["conversation"])

    messages = agent._as_model_messages(thread)  # noqa: SLF001
    staff_turns = [m for m in messages if "full refund" in (m["content"] or "")]
    assert staff_turns, "the staff reply is missing from the model's history"
    assert staff_turns[0]["role"] == "assistant", (
        "a staff message must read as something the agent said, or the model "
        "will contradict the person who said it"
    )


# ---------------------------------------------------------------- handback


def test_handback_returns_control_and_records_it(dsn, setup) -> None:
    with db.connect(dsn) as conn:
        agent.take_over(conn, setup["conversation"], "Ada")
        result = agent.hand_back(conn, setup["queue"], setup["conversation"])
        actions = [r["action"] for r in conn.execute("select action from audit_log")]
    assert result["status"] == "ai"
    assert _status(dsn, setup["conversation"]) == "ai"
    assert "handback" in actions


def test_handback_answers_a_message_that_arrived_during_the_takeover(dsn, setup) -> None:
    """That message was suppressed on purpose. Without the requeue it would
    never be answered, and the thread would look handled."""
    with db.connect(dsn) as conn:
        agent.take_over(conn, setup["conversation"], "Ada")
    _inbound(dsn, setup, "Hello? Anyone?", "h-3")

    with db.connect(dsn) as conn:
        result = agent.hand_back(conn, setup["queue"], setup["conversation"])
        pending = conn.execute(
            "select count(*) as n from jobs where kind = %s and status = 'queued'",
            (agent.TURN,)).fetchone()["n"]
    assert result["turn_queued"] is True
    assert pending == 1, "exactly one turn, not one per suppressed message"


def test_handback_queues_nothing_if_nothing_was_missed(dsn, setup) -> None:
    """The other direction, so the requeue cannot pass by always firing."""
    with db.connect(dsn) as conn:
        agent.take_over(conn, setup["conversation"], "Ada")
        # A person answered the only outstanding message.
        agent.staff_reply(conn, Chan(), setup["conversation"], "All sorted.")
        result = agent.hand_back(conn, setup["queue"], setup["conversation"])
    assert result["turn_queued"] is False


def test_handback_of_a_missing_conversation_raises(dsn, setup) -> None:
    with db.connect(dsn) as conn:
        with pytest.raises(LookupError):
            agent.hand_back(conn, setup["queue"],
                            "00000000-0000-0000-0000-000000000000")
