"""The whole vertical slice, in one runnable check.

    python scripts/e2e.py

No API keys, no WhatsApp, no network. The model is a script, the channel
records what it was asked to send, and Paystack's callbacks are signed with the
real algorithm -- so this exercises the production code paths rather than
mocks of them.

Every step asserts, and the first failure names itself and exits non-zero.
This is the ponytail rule's "one runnable check" applied to the product:
`pytest` covers the units, and this covers whether they add up to a shop that
can take an order and get paid for it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core" / "src"))
sys.path.insert(0, str(ROOT / "worker" / "src"))

from aisales import agent, db, followup, guard, paystack, providers  # noqa: E402
from aisales.channels import Inbound, Outbound  # noqa: E402
from aisales.db import TurnContext  # noqa: E402

DSN = os.environ.get("AISALES_E2E_DSN", "postgresql:///aisales_e2e")
SLUG = "e2e-shop"
PHONE = "08031234567"

STEPS = 0


class Failed(AssertionError):
    pass


class RecordingChannel:
    name = "e2e"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def verify(self, body, headers) -> bool: return True

    def parse(self, body, headers): return []

    def send(self, message: Outbound) -> str:
        self.sent.append(message.text)
        return f"e2e-{len(self.sent)}"


def step(label: str) -> None:
    global STEPS
    STEPS += 1
    print(f"\n{STEPS:2}. {label}")


def check(condition: bool, detail: str = "") -> None:
    if not condition:
        raise Failed(detail or "assertion failed")
    print(f"    ok  {detail}" if detail else "    ok")


# --------------------------------------------------------------------- setup


def reset() -> None:
    db.install(DSN)
    with db.connect(DSN) as conn:
        for table in ("audit_log", "order_items", "payments", "orders", "follow_ups",
                      "leads", "messages", "conversations", "customers", "jobs"):
            conn.execute(f"delete from {table}")
        conn.execute("delete from businesses")
        conn.execute("delete from products")
        conn.execute(
            """
            insert into businesses (slug, name, settings) values (%s, %s, %s)
            """,
            (SLUG, "E2E Fabrics", '{"followup": {"first_after_minutes": 1, '
                                  '"max_attempts": 2, "quiet_start": "00:00", '
                                  '"quiet_end": "00:00"}}'),
        )
        conn.execute(
            """
            insert into products (business_id, sku, name, price_kobo, stock_qty,
                                  variants, description, search_text)
            select id, 'LACE-001', 'Swiss Voile Lace', 1250000, 8,
                   '[{"colour": "black"}]', 'lace', 'swiss voile lace lace-001'
              from businesses where slug = %s
            """, (SLUG,))
        conn.execute(
            """
            insert into products (business_id, sku, name, price_kobo, stock_qty,
                                  variants, description, search_text)
            select id, 'GEL-001', 'Gele Head Tie', 320000, null,
                   '[]', 'gele', 'gele head tie gel-001'
              from businesses where slug = %s
            """, (SLUG,))


def business():
    with db.connect(DSN) as conn:
        return agent.business_by_slug(conn, SLUG)


def rpc(text: str, script: list, *, intent: str = "unknown", external: str = "",
        phone: str | None = None) -> dict:
    """Record a customer message and run the turn that answers it.

    `phone` defaults to one derived from the step number, which gives each step
    its own customer. That matters because a step that escalates hands the
    thread to a human -- and every later step on the same thread would then be
    silently suppressed, passing while asserting nothing.
    """
    channel = RecordingChannel()
    external = external or f"e2e-{abs(hash(text)) % 10**8}"
    phone = phone or f"080312345{STEPS:02d}"[-11:]
    with db.connect(DSN) as conn:
        biz = business()
        result = agent.record_inbound(conn, db.Queue(DSN), biz, Inbound(
            channel="simulator", external_id=external, from_phone=phone, text=text))
        ctx = TurnContext(business=biz, customer_id=result.customer_id,
                          conversation_id=result.conversation_id, channel="simulator")
        turn = agent.run_turn(conn, channel, providers.ScriptedChat(
            turns=script, intent=intent, intent_confidence=0.9), ctx,
            paystack=paystack.FakePaystack("sk_test_e2e"), queue=db.Queue(DSN))
        # The turn just ran inline, so the job it queued is spent. Left
        # queued it would be claimed by a later step expecting a different
        # kind of job -- and that step would then fail for a reason that has
        # nothing to do with what it was testing.
        if result.job_id:
            conn.execute("update jobs set status = 'succeeded', finished_at = now() "
                         "where id = %s", (result.job_id,))
        conversation = conn.execute(
            "select status, needs_attention from conversations where id = %s",
            (result.conversation_id,)).fetchone()
    return {"turn": turn, "sent": channel.sent, "conversation": result.conversation_id,
            "status": conversation["status"], "attention": conversation["needs_attention"]}


# --------------------------------------------------------------------- steps


def main() -> int:
    try:
        step("Seed one business, a catalogue, and settings")
        reset()
        check(business() is not None, f"{SLUG} exists with 2 products")

        step("A price is looked up and quoted")
        out = rpc("Abeg how much for the lace?", [
            providers.scripted_turn(calls=[("search_products", {"query": "lace"})]),
            providers.scripted_turn(text="The Swiss Voile Lace is ₦12,500.")],
            intent="price_enquiry")
        check(not out["turn"].blocked, "the reply was sent")
        check("₦12,500" in " ".join(out["sent"]), "it carried the real price")
        with db.connect(DSN) as conn:
            actions = [r["action"] for r in conn.execute("select action from audit_log")]
        check("price_quoted" in actions, "the quote is in the audit trail")

        step("An invented price is blocked and never reaches the customer")
        out = rpc("How much again?", [
            providers.scripted_turn(calls=[("search_products", {"query": "lace"})]),
            providers.scripted_turn(text="I can do ₦9,999 for you."),
            providers.scripted_turn(text="Fine — ₦9,999, final.")], intent="discount_request")
        check(out["turn"].blocked, "the reply was blocked")
        check("9,999" not in " ".join(out["sent"]), "the invented figure was not sent")
        check(out["sent"] == [agent.ESCALATION_LINE], "a fixed line went instead")

        step("An untracked-stock item can still be sold")
        out = rpc("You get gele?", [
            providers.scripted_turn(calls=[("get_product", {"sku": "GEL-001"})]),
            providers.scripted_turn(text="Yes, the Gele Head Tie is ₦3,200 and available.")],
            intent="availability_check")
        check(not out["turn"].blocked and out["sent"],
              "stock_qty NULL means not counted, not unavailable")
        check("₦3,200" in " ".join(out["sent"]), "it quoted the real price")

        # Steps 6 and 7 continue one customer's story, so they share a number.
        buyer = "08031234599"

        step("An order and a payment link")
        out = rpc("Send account", [
            providers.scripted_turn(calls=[("create_order", {"items": [
                {"sku": "LACE-001", "qty": 2}]})]),
            providers.scripted_turn(calls=[("create_payment_link", {
                "order_reference": "ORD-0001"})]),
            providers.scripted_turn(text="2 lace is ₦25,000. Pay here: https://pay.test/x")],
            intent="payment_intent", phone=buyer)
        check(not out["turn"].blocked, "the link was sent")
        with db.connect(DSN) as conn:
            order = conn.execute("select reference, status, total_kobo from orders").fetchone()
            payment = conn.execute(
                "select paystack_reference, status from payments").fetchone()
        check(order is not None, "an order exists")
        check(order["total_kobo"] == 2_500_000, "₦25,000 in kobo, exactly")
        check(order["status"] == "awaiting_payment", "awaiting payment")
        check(payment["status"] == "pending", "the payment row is pending")
        reference = payment["paystack_reference"]

        step("A customer saying they paid is not payment")
        out = rpc("I don pay", [
            providers.scripted_turn(calls=[("get_payment_status", {
                "order_reference": "ORD-0001"})]),
            providers.scripted_turn(text="Thank you, payment received!")],
            intent="payment_claim", phone=buyer)
        check(out["turn"].blocked, "the claim was refused")
        check("received" not in " ".join(out["sent"]).lower(),
              "no confirmation went out on the customer's word alone")

        fake = paystack.FakePaystack("sk_test_e2e")

        step("A signed callback for the wrong amount is rejected")
        body, signature = fake.callback(reference=reference, amount_kobo=50_000)
        check(paystack.verify_webhook("sk_test_e2e", body, signature),
              "the callback signature verifies")
        import json
        with db.connect(DSN) as conn:
            outcome = paystack.apply_callback(conn, DSN, event="charge.success",
                                              data=json.loads(body)["data"])
            status = conn.execute("select status from orders").fetchone()["status"]
        check(not outcome.handled and status != "paid", "a ₦500 callback did not settle ₦25,000")

        step("A signed callback for the right amount settles the order")
        with db.connect(DSN) as conn:
            conn.execute("update payments set status = 'pending'")
        body, signature = fake.callback(reference=reference, amount_kobo=2_500_000)
        with db.connect(DSN) as conn:
            outcome = paystack.apply_callback(conn, DSN, event="charge.success",
                                              data=json.loads(body)["data"])
            paid_at = conn.execute("select paid_at from orders").fetchone()["paid_at"]
        check(outcome.handled and paid_at is not None, "the order is paid")

        step("The same callback again changes nothing")
        body, signature = fake.callback(reference=reference, amount_kobo=2_500_000)
        with db.connect(DSN) as conn:
            again = paystack.apply_callback(conn, DSN, event="charge.success",
                                            data=json.loads(body)["data"])
            paid_again = conn.execute("select paid_at from orders").fetchone()["paid_at"]
            rows = conn.execute("select count(*) as n from payments").fetchone()["n"]
        check(again.idempotent and paid_again == paid_at, "idempotent")
        check(rows == 1, "exactly one payment row")

        step("A follow-up fires, and a reply before it cancels one")
        with db.connect(DSN) as conn:
            biz = business()
            conversation = conn.execute("select id, customer_id from conversations").fetchone()
            policy = followup.FollowUpPolicy.from_settings(biz.settings)
            follow_up_id, skip = followup.schedule(
                conn, db.Queue(DSN), business_id=biz.id,
                customer_id=str(conversation["customer_id"]),
                conversation_id=str(conversation["id"]),
                reason="said they would return", policy=policy,
                last_inbound_at=conn.execute(
                    "select last_inbound_at from conversations").fetchone()["last_inbound_at"],
                channel="simulator")
        check(follow_up_id is not None and skip is None, "a follow-up was scheduled")
        with db.connect(DSN) as conn:
            conn.execute("update follow_ups set due_at = now() - interval '1 minute'")
            conn.execute("update jobs set not_before = now() - interval '1 minute'")
        follow_channel = RecordingChannel()
        with db.connect(DSN) as conn:
            job = db.Queue(DSN).claim("e2e")
            outcome = agent.dispatch_follow_up(conn, db.Queue(DSN), job,
                                              chat=providers.ScriptedChat(turns=[
                                                  providers.scripted_turn(
                                                      text="Still thinking about the lace?")]),
                                              channel=follow_channel)
        check(outcome == "sent", f"the follow-up was sent (got {outcome!r})")
        check(follow_channel.sent, "and it actually went out")

        step("The digest reports what actually happened")
        from aisales_api.insights import daily  # noqa: PLC0415
        with db.connect(DSN) as conn:
            report = daily(conn, business().id)
        check(report["guard_blocked"] >= 1, "the guard blocks are reported")
        check(report["most_asked"], "intents were counted")
        check(isinstance(report["collected_kobo"], int), "money is an integer")
        with db.connect(DSN) as conn:
            expected = conn.execute(
                "select coalesce(sum(amount_kobo), 0) as k from payments "
                "where status = 'success'").fetchone()["k"]
        check(report["collected_kobo"] == expected,
              f"collected ₦{expected // 100:,} matches the ledger")
    except Failed as exc:
        print(f"\nFAILED at step {STEPS}: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
        import traceback
        print(f"\nERROR at step {STEPS}: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1

    print(f"\nAll {STEPS} steps passed, with no API keys and no network.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
