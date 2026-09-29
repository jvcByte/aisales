"""The money path: signatures, idempotency, and the amount.

Every test here corresponds to a bug found in the existing local Paystack
handler before this one was written. They are not hypothetical: that code
marks an order paid on a callback for the wrong amount, and processes the same
callback twice when two deliveries arrive together.

`FakePaystack` signs with the real algorithm and the real secret, so these
tests exercise verification rather than stepping around it.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from aisales import agent, db, paystack, tools
from aisales.channels import Inbound
from aisales.db import TurnContext

PHONE = "+2348031234567"
FAKE_SECRET = "sk_test_fake"


class FakeChannel:
    name = "test"

    def verify(self, body, headers): return True

    def parse(self, body, headers): return []

    def send(self, message): return "sent"


@pytest.fixture
def paid_setup(dsn: str, business):
    """A customer, an order, and a pending payment link for it."""
    with db.connect(dsn) as conn:
        conn.execute(
            """
            insert into products (business_id, sku, name, price_kobo, stock_qty,
                                  variants, description, search_text)
            values (%s, 'LACE-001', 'Swiss Voile Lace', 1250000, 8, '[]',
                    'lace', 'swiss voile lace lace-001')
            """, (business.id,))
        result = agent.record_inbound(conn, db.Queue(dsn), business, Inbound(
            channel="simulator", external_id="pay-1", from_phone=PHONE,
            text="Send account"))

        ctx = TurnContext(business=business, customer_id=result.customer_id,
                          conversation_id=result.conversation_id, channel="simulator")
        fake = paystack.FakePaystack(FAKE_SECRET)
        order = tools.dispatch(conn, ctx, tools.ToolCall(
            id="1", name="create_order", args={"items": [{"sku": "LACE-001", "qty": 1}]}))
        assert order.ok, order.error
        link = tools.dispatch(conn, ctx, tools.ToolCall(
            id="2", name="create_payment_link",
            args={"order_reference": order.data["order_reference"]}), paystack=fake)
        assert link.ok, link.error

        yield {"ctx": ctx, "fake": fake,
               "reference": link.data["payment_url"].rsplit("/", 1)[-1].upper(),
               "order_reference": order.data["order_reference"],
               "total_kobo": order.data["total_kobo"]}


def _order(dsn: str, reference: str) -> dict:
    with db.connect(dsn) as conn:
        return conn.execute(
            "select id, status, total_kobo, paid_at from orders where reference = %s",
            (reference,)).fetchone()


def _payments(dsn: str, order_reference: str) -> list[dict]:
    with db.connect(dsn) as conn:
        return conn.execute(
            """
            select p.paystack_reference, p.status, p.amount_kobo
              from payments p join orders o on o.id = p.order_id
             where o.reference = %s
            """, (order_reference,)).fetchall()


def _callback(dsn: str, payload: tuple[bytes, str]) -> paystack.CallbackOutcome:
    body, signature = payload
    assert paystack.verify_webhook(FAKE_SECRET, body, signature), "the fake did not sign"
    parsed = json.loads(body)
    with db.connect(dsn) as conn:
        return paystack.apply_callback(conn, dsn, event=parsed["event"], data=parsed["data"])


# ------------------------------------------------------------- signatures


def test_sign_and_verify_round_trip() -> None:
    body = b'{"event":"charge.success"}'
    assert paystack.verify_webhook(FAKE_SECRET, body, paystack.sign(FAKE_SECRET, body))


def test_a_sha256_signature_does_not_verify() -> None:
    """The trap. Meta signs with SHA-256 and Paystack with SHA-512 over the
    same idea, and the two adapters look alike enough that a copy between them
    produces a verifier which either always passes or never does."""
    body = b'{"event":"charge.success"}'
    meta_style = hmac.new(FAKE_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert not paystack.verify_webhook(FAKE_SECRET, body, meta_style)


def test_a_missing_or_wrong_signature_is_refused() -> None:
    body = b'{"event":"charge.success"}'
    assert not paystack.verify_webhook(FAKE_SECRET, body, None)
    assert not paystack.verify_webhook(FAKE_SECRET, body, "deadbeef")
    assert not paystack.verify_webhook("", body, paystack.sign(FAKE_SECRET, body))


# ------------------------------------------------------- the three fixes


def test_a_duplicate_callback_pays_once(dsn, business, paid_setup) -> None:
    """Two concurrent deliveries of the same callback used to both pass the
    'already confirmed?' read and both run the downstream effects."""
    reference = paid_setup["reference"]
    amount = paid_setup["total_kobo"]
    signed = paid_setup["fake"].callback(reference=reference, amount_kobo=amount)

    first = _callback(dsn, signed)
    assert first.handled and not first.idempotent
    paid_at = _order(dsn, paid_setup["order_reference"])["paid_at"]
    assert paid_at is not None

    second = _callback(dsn, signed)
    assert not second.handled and second.idempotent
    assert _order(dsn, paid_setup["order_reference"])["paid_at"] == paid_at
    assert len(_payments(dsn, paid_setup["order_reference"])) == 1


def test_an_amount_mismatch_never_marks_paid(dsn, business, paid_setup) -> None:
    """A callback for ₦500 against a ₦12,500 order. The existing handler reads
    the amount from its own ledger row and never compares, so it pays it."""
    signed = paid_setup["fake"].callback(reference=paid_setup["reference"],
                                         amount_kobo=50_000)
    outcome = _callback(dsn, signed)

    assert not outcome.handled
    assert "amount" in outcome.reason
    order = _order(dsn, paid_setup["order_reference"])
    assert order["status"] != "paid", "a short payment was accepted"
    assert order["paid_at"] is None
    assert _payments(dsn, paid_setup["order_reference"])[0]["status"] == "failed"

    with db.connect(dsn) as conn:
        actions = [r["action"] for r in conn.execute(
            "select action from audit_log order by id").fetchall()]
    assert "payment_amount_mismatch" in actions


def test_an_amount_mismatch_brings_in_a_person(dsn, business, paid_setup) -> None:
    signed = paid_setup["fake"].callback(reference=paid_setup["reference"],
                                         amount_kobo=50_000)
    _callback(dsn, signed)
    with db.connect(dsn) as conn:
        row = conn.execute("select needs_attention, attention_reason from conversations"
                           ).fetchone()
    assert row["needs_attention"] is True
    assert "amount" in row["attention_reason"]


def test_an_unknown_event_touches_nothing(dsn, business, paid_setup) -> None:
    before = _order(dsn, paid_setup["order_reference"])
    signed = paid_setup["fake"].callback(reference=paid_setup["reference"],
                                         amount_kobo=paid_setup["total_kobo"],
                                         event="transfer.success")
    outcome = _callback(dsn, signed)
    assert not outcome.handled and not outcome.idempotent
    assert outcome.reason.startswith("ignored event")
    after = _order(dsn, paid_setup["order_reference"])
    assert after["status"] == before["status"] and after["paid_at"] is None


def test_an_unknown_reference_is_acknowledged_not_recorded(dsn, business) -> None:
    fake = paystack.FakePaystack(FAKE_SECRET)
    outcome = _callback(dsn, fake.callback(reference="NOT-OURS-0001",
                                           amount_kobo=100_000))
    assert not outcome.handled and not outcome.idempotent
    with db.connect(dsn) as conn:
        assert conn.execute("select count(*) as n from payments").fetchone()["n"] == 0


# ------------------------------------------------------------- the money


def test_kobo_is_an_integer_and_never_a_float(dsn, business, paid_setup) -> None:
    """₦12,500 is 1250000 kobo. Paystack transmits kobo, so nothing in the
    path divides by 100 -- which is what keeps 12499.999999999998 out of a
    payment request."""
    with db.connect(dsn) as conn:
        row = conn.execute(
            "select amount_kobo, pg_typeof(amount_kobo) as t from payments").fetchone()
    assert row["amount_kobo"] == 1_250_000
    assert row["t"] == "bigint"
    assert isinstance(row["amount_kobo"], int) and not isinstance(row["amount_kobo"], bool)

    # And the client was asked for an integer, not a rounded float.
    assert all(isinstance(c["amount_kobo"], int) for c in paid_setup["fake"].calls)
    assert paid_setup["fake"].calls[0]["amount_kobo"] == 1_250_000


def test_a_link_sent_twice_is_the_same_link(dsn, business, paid_setup) -> None:
    """A customer who asks again must not get a second payment page, and
    Paystack errors on a reused reference."""
    fake = paid_setup["fake"]
    before = len(fake.calls)
    with db.connect(dsn) as conn:
        again = tools.dispatch(conn, paid_setup["ctx"], tools.ToolCall(
            id="3", name="create_payment_link",
            args={"order_reference": paid_setup["order_reference"]}), paystack=fake)
    assert again.ok
    assert again.data["payment_url"]
    assert len(fake.calls) == before, "a second Paystack transaction was created"


def test_the_status_tool_refuses_to_confirm_before_the_callback(dsn, business,
                                                                paid_setup) -> None:
    with db.connect(dsn) as conn:
        result = tools.dispatch(conn, paid_setup["ctx"], tools.ToolCall(
            id="4", name="get_payment_status",
            args={"order_reference": paid_setup["order_reference"]}))
    assert result.ok and result.data["paid"] is False
    assert result.payment_confirmed is False
    assert "NOT confirmed" in result.data["instruction"]


def test_the_status_tool_confirms_after_the_callback(dsn, business, paid_setup) -> None:
    _callback(dsn, paid_setup["fake"].callback(
        reference=paid_setup["reference"], amount_kobo=paid_setup["total_kobo"]))
    with db.connect(dsn) as conn:
        result = tools.dispatch(conn, paid_setup["ctx"], tools.ToolCall(
            id="5", name="get_payment_status",
            args={"order_reference": paid_setup["order_reference"]}))
    assert result.data["paid"] is True
    assert result.payment_confirmed is True
