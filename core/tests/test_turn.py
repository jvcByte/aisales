"""One whole turn, offline: a scripted model, real SQL, the real guard.

This is the test that makes the offline path evidence about production rather
than about a harness. `ScriptedChat` emits a tool call, the *real* dispatcher
runs *real* SQL, and the scripted next turn sees that result -- so the guard's
allowed set is populated exactly as it would be live. Nothing here is mocked
except the two things that cost money or need credentials: the model and the
channel.

The hallucination cases matter most. They are the reason the guard exists, and
they are tested against the same code path a real conversation takes.
"""

from __future__ import annotations

import pytest

from aisales import agent, db, guard, paystack, providers
from aisales.channels import Inbound, Outbound
from aisales.db import TurnContext

PHONE = "+2348031234567"


class RecordingChannel:
    """A channel that keeps what it sent, so a test can assert on the words."""

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


@pytest.fixture
def catalogue(dsn: str, business):
    rows = [("LACE-001", "Swiss Voile Lace", 1_250_000, 8),
            ("KAF-001", "Kaftan (Men)", 1_800_000, 0),
            ("GEL-001", "Gele Head Tie", 320_000, None)]
    with db.connect(dsn) as conn:
        for sku, name, price, stock in rows:
            conn.execute(
                """
                insert into products (business_id, sku, name, price_kobo, stock_qty,
                                      variants, description, search_text)
                values (%s, %s, %s, %s, %s, '[]', %s, %s)
                """,
                (business.id, sku, name, price, stock, f"{name} for sale",
                 f"{name} {sku} for sale".lower()),
            )
    return business


def _start(dsn: str, business) -> tuple[TurnContext, str]:
    q = db.Queue(dsn)
    with db.connect(dsn) as conn:
        result = agent.record_inbound(conn, q, business, Inbound(
            channel="simulator", external_id="t1", from_phone=PHONE,
            text="Abeg how much for the lace?"))
        ctx = TurnContext(business=business, customer_id=result.customer_id,
                          conversation_id=result.conversation_id, channel="simulator")
    return ctx, result.conversation_id


def _turn(dsn: str, ctx, chat, *, paystack_client=None):
    channel = RecordingChannel()
    with db.connect(dsn) as conn:
        result = agent.run_turn(conn, channel, chat, ctx,
                                paystack=paystack_client, queue=db.Queue(dsn))
    return result, channel


def _bodies(dsn: str, conversation_id: str) -> list[str]:
    with db.connect(dsn) as conn:
        return [r["body"] for r in conn.execute(
            "select body from messages where conversation_id = %s order by id",
            (conversation_id,)).fetchall()]


# ------------------------------------------------- the happy path


def test_a_grounded_price_reaches_the_customer(dsn, business, catalogue) -> None:
    """Look the price up, quote it, and be allowed to. The whole point."""
    ctx, conv = _start(dsn, business)
    chat = providers.ScriptedChat(turns=[
        providers.scripted_turn(calls=[("search_products", {"query": "lace"})]),
        providers.scripted_turn(text="The Swiss Voile Lace is ₦12,500. Would you like it?"),
    ])
    result, channel = _turn(dsn, ctx, chat)

    assert not result.blocked
    assert channel.sent == ["The Swiss Voile Lace is ₦12,500. Would you like it?"]
    assert result.tool_calls[0]["name"] == "search_products"
    assert result.tool_calls[0]["ok"]
    assert result.allowed_naira == [12_500]


def test_the_allowed_set_comes_from_the_tool_not_the_prompt(dsn, business, catalogue) -> None:
    """The price the model may quote arrived in a tool result this turn. That
    is the only reason the reply above was permitted."""
    ctx, conv = _start(dsn, business)
    chat = providers.ScriptedChat(turns=[
        providers.scripted_turn(calls=[("search_products", {"query": "lace"})]),
        providers.scripted_turn(text="It is ₦12,500."),
    ])
    result, _ = _turn(dsn, ctx, chat)
    assert result.allowed_naira == [12_500]


# ------------------------------------------------- the hallucination


def test_an_invented_price_is_blocked_and_never_stored(dsn, business, catalogue) -> None:
    """The failure this whole product is arranged around.

    The model looked the lace up, saw ₦12,500, and said ₦9,999 anyway. The
    customer must not receive it, the owner must be able to see why, and the
    invented figure must not enter the transcript -- it was never said, and
    keeping it would feed it back to the model next turn as though it had been.
    """
    ctx, conv = _start(dsn, business)
    chat = providers.ScriptedChat(turns=[
        providers.scripted_turn(calls=[("search_products", {"query": "lace"})]),
        providers.scripted_turn(text="I can do it for ₦9,999."),
        # The retry prompt, still insisting.
        providers.scripted_turn(text="Fine, ₦9,999 is my best price."),
    ])
    result, channel = _turn(dsn, ctx, chat)

    assert result.blocked
    assert result.severity == agent.HANDOVER
    assert "9,999" not in " ".join(channel.sent), "the invented price was sent"
    assert channel.sent == [agent.ESCALATION_LINE]
    assert "9,999" not in " ".join(_bodies(dsn, conv))


def test_a_discount_the_agent_cannot_grant_is_blocked(dsn, business, catalogue) -> None:
    """'Reduce am' is the most common utterance in the corpus. The reply to it
    must never contain a concession."""
    ctx, conv = _start(dsn, business)
    chat = providers.ScriptedChat(turns=[
        providers.scripted_turn(text="Sure, I'll reduce it to ₦10,000 for you."),
        providers.scripted_turn(text="Okay, I'll give you a discount."),
    ])
    result, channel = _turn(dsn, ctx, chat)

    assert result.blocked
    assert result.reason == "guard_blocked"
    assert channel.sent == [agent.ESCALATION_LINE]


def test_the_block_is_auditable(dsn, business, catalogue) -> None:
    """Requirement 4.8. The owner has to be able to answer 'why did it say
    nothing?' without re-running the model."""
    ctx, conv = _start(dsn, business)
    chat = providers.ScriptedChat(turns=[
        providers.scripted_turn(text="It is ₦9,999."),
        providers.scripted_turn(text="It is ₦9,999, final."),
    ])
    _turn(dsn, ctx, chat)
    with db.connect(dsn) as conn:
        row = conn.execute(
            "select detail from audit_log where action = 'guard_blocked' "
            "order by id desc limit 1").fetchone()
    assert row is not None
    assert "9,999" in str(row["detail"]["tokens"])
    assert row["detail"]["allowed_kobo"] == []


# ------------------------------------------------- the rest of the loop


def test_no_reply_really_sends_nothing(dsn, business, catalogue) -> None:
    """Silence is a feature. An agent that answers 'thank you' is one people
    mute."""
    ctx, conv = _start(dsn, business)
    chat = providers.ScriptedChat(turns=[
        providers.scripted_turn(calls=[("no_reply", {"reason": "just a thank you"})]),
    ])
    result, channel = _turn(dsn, ctx, chat)
    assert result.silent and channel.sent == []


def test_a_handover_tells_the_customer_and_stops(dsn, business, catalogue) -> None:
    ctx, conv = _start(dsn, business)
    chat = providers.ScriptedChat(turns=[
        providers.scripted_turn(calls=[
            ("escalate_to_human", {"reason": "customer wants a refund", "urgency": "handover"})]),
    ])
    result, channel = _turn(dsn, ctx, chat)

    assert result.severity == agent.HANDOVER
    assert channel.sent == [agent.ESCALATION_LINE]
    with db.connect(dsn) as conn:
        assert conn.execute("select status from conversations where id = %s",
                            (conv,)).fetchone()["status"] == "human"


def test_a_customer_saying_they_paid_is_not_payment(dsn, business, catalogue) -> None:
    """The vision's own example of what must never be believed."""
    ctx, conv = _start(dsn, business)
    pay = paystack.FakePaystack()
    chat = providers.ScriptedChat(turns=[
        providers.scripted_turn(calls=[("create_order", {"items": [
            {"sku": "LACE-001", "qty": 1}]})]),
        providers.scripted_turn(calls=[("create_payment_link", {
            "order_reference": "ORD-0001"})]),
        providers.scripted_turn(calls=[("get_payment_status", {
            "order_reference": "ORD-0001"})]),
        providers.scripted_turn(
            text="Thank you! Your payment is confirmed and we will ship it."),
    ])
    result, channel = _turn(dsn, ctx, chat, paystack_client=pay)

    # Nothing was actually paid, so the guard's money rule cannot be the thing
    # that catches this -- it is the payment-status tool's own instruction, and
    # the fact that a claim alone never sets `paid`.
    with db.connect(dsn) as conn:
        order = conn.execute(
            "select status, total_kobo from orders where conversation_id = %s",
            (conv,)).fetchone()
    assert order["status"] == "awaiting_payment"
    assert order["total_kobo"] == 1_250_000
    assert any(c["name"] == "get_payment_status" for c in result.tool_calls)


def test_every_provider_failing_never_leaves_silence(dsn, business, catalogue) -> None:
    """The failure mode that must not happen. A customer waiting, every model
    spent, and a person always reachable."""
    ctx, conv = _start(dsn, business)

    class Dead:
        name = "dead"

        def chat(self, messages, tools):
            raise providers.ProviderError("all free tiers are spent", reason="quota")

    result, channel = _turn(dsn, ctx, Dead())

    assert channel.sent == [agent.ESCALATION_LINE], "the customer was left in silence"
    assert result.severity == agent.HANDOVER
    with db.connect(dsn) as conn:
        assert conn.execute("select status from conversations where id = %s",
                            (conv,)).fetchone()["status"] == "human"


def test_a_person_owning_the_thread_silences_the_agent(dsn, business, catalogue) -> None:
    ctx, conv = _start(dsn, business)
    with db.connect(dsn) as conn:
        conn.execute("update conversations set status = 'human' where id = %s", (conv,))
    chat = providers.ScriptedChat(turns=[providers.scripted_turn(text="I should not say this")])
    result, channel = _turn(dsn, ctx, chat)

    assert result.silent and channel.sent == []
    with db.connect(dsn) as conn:
        assert conn.execute(
            "select count(*) as n from audit_log where action = 'takeover_suppressed_turn'"
        ).fetchone()["n"] >= 1


def test_a_tool_loop_ends_in_a_person(dsn, business, catalogue) -> None:
    """A model calling tools forever costs money and never concludes."""
    ctx, conv = _start(dsn, business)
    chat = providers.ScriptedChat(turns=[
        providers.scripted_turn(calls=[("search_products", {"query": "lace"})])
        for _ in range(agent.MAX_TOOL_ROUNDS)
    ])
    result, channel = _turn(dsn, ctx, chat)
    assert result.severity == agent.HANDOVER
    assert result.reason == "tool_loop"


def test_a_bad_tool_call_is_reported_not_dispatched(dsn, business, catalogue) -> None:
    """A malformed call must never write a nonsense row."""
    ctx, conv = _start(dsn, business)
    chat = providers.ScriptedChat(turns=[
        providers.scripted_turn(calls=[("create_order", {"items": []})]),
        providers.scripted_turn(text="Sorry, what would you like to order?"),
    ])
    result, _ = _turn(dsn, ctx, chat)
    assert result.tool_calls[0]["ok"] is False
    assert "items" in result.tool_calls[0]["data"]["error"]
    with db.connect(dsn) as conn:
        assert conn.execute("select count(*) as n from orders").fetchone()["n"] == 0


def test_the_reply_is_stored_only_after_it_is_sent(dsn, business, catalogue) -> None:
    """A failed send must leave no message, or the transcript claims the
    customer was told something they never received."""
    ctx, conv = _start(dsn, business)

    class Broken:
        name = "broken"

        def verify(self, b, h): return True

        def parse(self, b, h): return []

        def send(self, message):
            raise RuntimeError("the channel is down")

    chat = providers.ScriptedChat(turns=[providers.scripted_turn(text="Hello there.")])
    with db.connect(dsn) as conn:
        before = len(_bodies(dsn, conv))
        with pytest.raises(RuntimeError):
            agent.run_turn(conn, Broken(), chat, ctx, queue=db.Queue(dsn))
        assert not any("Hello there" in b for b in _bodies(dsn, conv))
        del before
