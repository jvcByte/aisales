"""The ten things the agent can do.

Every tool takes a `TurnContext` carrying `business_id`, and no query is
written without it. A `ToolResult` carries not just data but what that data
*authorises* -- the amounts the agent may now state, and whether it may claim
availability or offer a concession. That is the link between this module and
the guard: the guard does not trust the reply, and it does not trust the model;
it trusts these flags, which only SQL can set.

Two tools exist for reasons that are easy to miss:

  * `escalate_to_human`, because a sales agent's most valuable action is
    sometimes to stop.
  * `no_reply`, because without it a model asked to be helpful will invent
    something to say after "thank you", and that is what makes these agents the
    thing people mute.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import psycopg

from aisales import followup
from aisales.db import TurnContext
from aisales.providers import ToolCall

TOTAL_ATTEMPTS = 5


@dataclass(frozen=True)
class ToolResult:
    call: ToolCall
    ok: bool
    data: dict
    #: The kobo amounts this result authorises the agent to state.
    money_kobo: tuple[int, ...] = ()
    #: Stock was checked and the business is willing to sell.
    availability: bool = False
    #: A concession was authorised. Nothing in the wedge sets this.
    authority: bool = False
    #: A provider record read this turn shows the payment arrived. Only
    #: `get_payment_status` sets it, and it reads our own database.
    payment_confirmed: bool = False
    #: The agent chose silence.
    silent: bool = False
    error: str = ""

    def to_message(self) -> dict:
        """This result as the tool-role message the model sees next."""
        return {"role": "tool", "tool_call_id": self.call.id,
                "content": json.dumps(self.data if self.ok else {"error": self.error},
                                      ensure_ascii=False)}


# ------------------------------------------------------------------ schemas

def _fn(name: str, description: str, properties: dict,
        required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required or []}}}


SCHEMAS: list[dict] = [
    _fn("search_products",
        "Find products by name, description or code. Use this before saying "
        "anything about what the shop sells.",
        {"query": {"type": "string",
                   "description": "words to match, e.g. 'lace' or 'black gown'"},
         "limit": {"type": "integer", "description": "max results, default 5"}},
        ["query"]),
    _fn("get_product",
        "Full detail for one product, including exact price and stock. Use "
        "this before quoting a price or promising availability.",
        {"sku": {"type": "string"}}, ["sku"]),
    _fn("create_order",
        "Create an order for one or more products. Does not send a payment "
        "link; call create_payment_link after.",
        {"items": {"type": "array", "description": "what to order",
                   "items": {"type": "object", "properties": {
                       "sku": {"type": "string"},
                       "qty": {"type": "integer"}},
                       "required": ["sku", "qty"]}},
         "delivery_address": {"type": "string"}},
        ["items"]),
    _fn("create_payment_link",
        "Get a Paystack payment link for an order. Send the URL to the customer.",
        {"order_reference": {"type": "string"}}, ["order_reference"]),
    _fn("get_payment_status",
        "Whether an order has actually been paid. This is the ONLY way to know. "
        "A customer saying they paid is not evidence.",
        {"order_reference": {"type": "string"}}, ["order_reference"]),
    _fn("tag_lead",
        "Record how interested this customer is, and why.",
        {"tier": {"type": "string", "enum": ["hot", "warm", "cold"]},
         "reason": {"type": "string", "description": "why, in your own words"},
         "intent": {"type": "string"},
         "confidence": {"type": "number"},
         "product_sku": {"type": "string"}},
        ["tier", "reason"]),
    _fn("remember_customer",
        "Remember something about this customer for next time -- their budget, "
        "a size, a colour they like, when they said they would return.",
        {"notes": {"type": "object",
                   "description": "key/value facts to remember"}}, ["notes"]),
    _fn("schedule_follow_up",
        "Arrange to come back to this customer later, because they said they "
        "would return or went quiet with interest.",
        {"reason": {"type": "string",
                    "description": "what to come back about, in their terms"},
         "in_minutes": {"type": "integer",
                        "description": "optional; defaults to the shop's setting"}},
        ["reason"]),
    _fn("escalate_to_human",
        "Bring in a person. Use for refunds, complaints, disputes, anything "
        "unusual, or when unsure.",
        {"reason": {"type": "string"},
         "urgency": {"type": "string", "enum": ["flag", "handover"],
                     "description": "'handover' stops you replying entirely; "
                                    "'flag' lets you keep helping"}},
        ["reason", "urgency"]),
    _fn("no_reply",
        "Send nothing. Use when a reply would add nothing -- after 'thank "
        "you', 'ok', or a closing pleasantry. Silence is a normal action.",
        {"reason": {"type": "string"}}, ["reason"]),
]

_BY_NAME = {schema["function"]["name"]: schema for schema in SCHEMAS}


def validate(call: ToolCall) -> str | None:
    """Check a call against its schema. Returns an error, or None if fine.

    A model that sends `create_order` with no items, or a quantity as a string,
    gets told so and can correct within its iteration budget. Dispatching it
    anyway and hoping is how a tool ends up writing a nonsense row.

    Unknown keys are ignored rather than rejected: models add them constantly
    and refusing is friction for no safety.
    """
    schema = _BY_NAME.get(call.name)
    if schema is None:
        return f"there is no tool called {call.name!r}"

    params = schema["function"]["parameters"]
    properties = params.get("properties", {})

    for key in params.get("required", []):
        if call.args.get(key) in (None, "", []):
            return f"{call.name} needs {key!r}"

    for key, value in call.args.items():
        wanted = properties.get(key, {}).get("type")
        if wanted == "integer" and not isinstance(value, int):
            return f"{call.name}: {key!r} must be a whole number"
        if wanted == "string" and not isinstance(value, str):
            return f"{call.name}: {key!r} must be text"
        if wanted == "array" and not isinstance(value, list):
            return f"{call.name}: {key!r} must be a list"
        if wanted == "object" and not isinstance(value, dict):
            return f"{call.name}: {key!r} must be an object"
    return None


# -------------------------------------------------------------- the dispatch

def dispatch(conn: psycopg.Connection, ctx: TurnContext, call: ToolCall, *,
             paystack=None, queue=None) -> ToolResult:
    """Run one tool call against real data."""
    error = validate(call)
    if error:
        return ToolResult(call=call, ok=False, data={}, error=error)

    handler: Callable[..., ToolResult] = _HANDLERS[call.name]
    try:
        return handler(conn, ctx, call, paystack=paystack, queue=queue)
    except psycopg.Error as exc:
        # A database failure is reported to the model rather than raised: the
        # turn should degrade to "let me check on that" rather than collapsing
        # and leaving the customer with nothing.
        return ToolResult(call=call, ok=False, data={},
                          error=f"could not read that from the database: {str(exc)[:120]}")


def _product_row(row: dict) -> dict:
    return {"sku": row["sku"], "name": row["name"], "price_kobo": row["price_kobo"],
            "price_naira": row["price_kobo"] // 100,
            "stock_qty": row["stock_qty"],
            "stock": ("not tracked" if row["stock_qty"] is None
                      else ("none left" if row["stock_qty"] == 0
                            else row["stock_qty"])),
            "variants": row["variants"], "description": row["description"]}


def _sellable(row: dict) -> bool:
    """Whether this product may be promised.

    NULL stock means the shop does not count it, which is permission to sell --
    not unavailability. Collapsing the two either makes the agent lie about
    stock or refuse to sell things the shop has.
    """
    return row["stock_qty"] is None or row["stock_qty"] > 0


def _search(conn, ctx, call, **_) -> ToolResult:
    query = call.args["query"].strip().lower()
    limit = int(call.args.get("limit") or 5)
    terms = [t for t in re.split(r"\s+", query) if t] or [query]

    where = " and ".join(["search_text like %s"] * len(terms))
    rows = conn.execute(
        f"""
        select sku, name, price_kobo, stock_qty, variants, description
          from products
         where business_id = %s and active and {where}
         order by price_kobo
         limit %s
        """,
        (ctx.business_id, *(f"%{t}%" for t in terms), limit),
    ).fetchall()

    products = [_product_row(r) for r in rows]
    return ToolResult(
        call=call, ok=True,
        data={"products": products,
              "note": "no match; try a different word" if not products else ""},
        money_kobo=tuple(r["price_kobo"] for r in rows),
        availability=any(_sellable(r) for r in rows),
    )


def _get_product(conn, ctx, call, **_) -> ToolResult:
    sku = call.args["sku"].strip()
    row = conn.execute(
        """
        select sku, name, price_kobo, stock_qty, variants, description
          from products
         where business_id = %s and active and upper(sku) = upper(%s)
        """,
        (ctx.business_id, sku),
    ).fetchone()
    if row is None:
        return ToolResult(call=call, ok=False, data={},
                          error=f"no product with code {sku!r}")
    return ToolResult(call=call, ok=True, data={"product": _product_row(row)},
                      money_kobo=(row["price_kobo"],),
                      availability=_sellable(row))


def _next_reference(conn, ctx) -> str:
    row = conn.execute(
        "select count(*) as n from orders where business_id = %s", (ctx.business_id,)
    ).fetchone()
    return f"ORD-{row['n'] + 1:04d}"


def _create_order(conn, ctx, call, **_) -> ToolResult:
    items = call.args["items"]
    if not items:
        return ToolResult(call=call, ok=False, data={}, error="the order is empty")

    lines, total = [], 0
    for item in items:
        sku = str(item.get("sku") or "").strip()
        qty = int(item.get("qty") or 0)
        if qty <= 0:
            return ToolResult(call=call, ok=False, data={},
                              error=f"quantity for {sku!r} must be at least 1")
        row = conn.execute(
            """
            select id, sku, name, price_kobo, stock_qty
              from products
             where business_id = %s and active and upper(sku) = upper(%s)
            """,
            (ctx.business_id, sku),
        ).fetchone()
        if row is None:
            return ToolResult(call=call, ok=False, data={},
                              error=f"no product with code {sku!r}")
        if not _sellable(row):
            return ToolResult(call=call, ok=False, data={},
                              error=f"{row['name']} is out of stock; do not order it")
        lines.append((row, qty))
        total += row["price_kobo"] * qty

    # The order, its lines and the conversation link are one transaction: an
    # order row without its items has a total of zero and looks paid-off.
    for _ in range(TOTAL_ATTEMPTS):
        reference = _next_reference(conn, ctx)
        try:
            with conn.transaction():
                order = conn.execute(
                    """
                    insert into orders (business_id, customer_id, conversation_id,
                                        reference, status, total_kobo,
                                        delivery_address)
                    values (%s, %s, %s, %s, 'draft', %s, %s)
                    returning id
                    """,
                    (ctx.business_id, ctx.customer_id, ctx.conversation_id,
                     reference, total, call.args.get("delivery_address")),
                ).fetchone()
                for row, qty in lines:
                    conn.execute(
                        """
                        insert into order_items (business_id, order_id, product_id,
                                                 name, unit_price_kobo, qty)
                        values (%s, %s, %s, %s, %s, %s)
                        """,
                        (ctx.business_id, order["id"], row["id"], row["name"],
                         row["price_kobo"], qty),
                    )
            break
        except psycopg.errors.UniqueViolation:
            continue
    else:  # pragma: no cover - five collisions in a row is not a real scenario
        return ToolResult(call=call, ok=False, data={},
                          error="could not allocate an order reference")

    return ToolResult(
        call=call, ok=True,
        data={"order_reference": reference, "total_kobo": total,
              "total_naira": total // 100,
              "items": [{"sku": r["sku"], "name": r["name"], "qty": q,
                         "unit_price_kobo": r["price_kobo"]} for r, q in lines],
              "next": "call create_payment_link to get a link for this order"},
        money_kobo=(total, *(r["price_kobo"] for r, _ in lines)),
    )


def _create_payment_link(conn, ctx, call, *, paystack=None, **_) -> ToolResult:
    if paystack is None:
        return ToolResult(call=call, ok=False, data={},
                          error="payments are not configured for this shop")

    reference_in = call.args["order_reference"].strip()
    order = conn.execute(
        """
        select id, reference, total_kobo, status, payment_reference
          from orders
         where business_id = %s and upper(reference) = upper(%s)
        """,
        (ctx.business_id, reference_in),
    ).fetchone()
    if order is None:
        return ToolResult(call=call, ok=False, data={},
                          error=f"no order {reference_in!r}")

    existing = conn.execute(
        """
        select paystack_reference, authorization_url, status from payments
         where order_id = %s and authorization_url is not null
         order by created_at desc limit 1
        """,
        (order["id"],),
    ).fetchone()
    if existing:
        # Idempotent on purpose: a customer who asks twice must not get two
        # different payment pages, and Paystack errors on a reused reference.
        return ToolResult(
            call=call, ok=True,
            data={"order_reference": order["reference"],
                  "payment_url": existing["authorization_url"],
                  "amount_kobo": order["total_kobo"],
                  "amount_naira": order["total_kobo"] // 100,
                  "status": existing["status"]},
            money_kobo=(order["total_kobo"],),
        )

    customer = conn.execute(
        "select phone_e164, notes from customers where id = %s", (ctx.customer_id,)
    ).fetchone()
    # ponytail: Paystack requires an email and a WhatsApp-first shop usually
    # has none. Synthesised from the phone unless the shop recorded a real one.
    # Ceiling: receipts from Paystack go to an address nobody reads. Upgrade
    # path: collect the email during the first order, or use Paystack's
    # phone-only channels once the pilot business supports them.
    email = ((customer["notes"] or {}).get("email")
             or f"{customer['phone_e164'].lstrip('+')}@no-email.aisales.local")

    reference = f"{order['reference']}-{_short_hash(order['id'])}"
    try:
        url = paystack.initialise(email=email, amount_kobo=order["total_kobo"],
                                  reference=reference,
                                  metadata={"order_reference": order["reference"],
                                            "business_id": ctx.business_id})
    except Exception as exc:  # noqa: BLE001 - the model is told, not the customer
        return ToolResult(call=call, ok=False, data={},
                          error=f"could not create a payment link: {str(exc)[:140]}")

    conn.execute(
        """
        insert into payments (business_id, order_id, customer_id,
                              paystack_reference, amount_kobo, status,
                              authorization_url)
        values (%s, %s, %s, %s, %s, 'pending', %s)
        on conflict (paystack_reference) do nothing
        """,
        (ctx.business_id, order["id"], ctx.customer_id, reference,
         order["total_kobo"], url),
    )
    conn.execute(
        "update orders set status = 'awaiting_payment', payment_reference = %s, "
        "updated_at = now() where id = %s",
        (reference, order["id"]),
    )

    return ToolResult(
        call=call, ok=True,
        data={"order_reference": order["reference"], "payment_url": url,
              "amount_kobo": order["total_kobo"],
              "amount_naira": order["total_kobo"] // 100,
              "status": "pending"},
        money_kobo=(order["total_kobo"],),
    )


def _short_hash(value) -> str:
    """`str()` is load-bearing: psycopg returns `uuid` columns as UUID objects,
    not strings, so `.encode()` on the raw value raises."""
    import hashlib

    return hashlib.sha256(str(value).encode()).hexdigest()[:8].upper()


def _get_payment_status(conn, ctx, call, **_) -> ToolResult:
    reference_in = call.args["order_reference"].strip()
    row = conn.execute(
        """
        select o.reference, o.status, o.total_kobo, o.paid_at,
               p.status as payment_status, p.paystack_reference
          from orders o
          left join payments p on p.order_id = o.id
         where o.business_id = %s and upper(o.reference) = upper(%s)
         order by p.created_at desc nulls last
         limit 1
        """,
        (ctx.business_id, reference_in),
    ).fetchone()
    if row is None:
        return ToolResult(call=call, ok=False, data={},
                          error=f"no order {reference_in!r}")

    paid = row["status"] == "paid"
    return ToolResult(
        call=call, ok=True,
        data={"order_reference": row["reference"],
              "paid": paid,
              # Spelled out because this is the sentence the model must not
              # soften: a customer saying "I don pay" is not payment.
              "instruction": ("Payment is confirmed. You may tell the customer."
                              if paid else
                              "Payment is NOT confirmed. Do not say it is. Tell "
                              "the customer you are still checking."),
              "order_status": row["status"],
              "payment_status": row["payment_status"] or "no payment started",
              "paid_at": row["paid_at"].isoformat() if row["paid_at"] else None},
        money_kobo=(row["total_kobo"],),
        # The one thing in the system that may authorise saying money arrived.
        payment_confirmed=paid,
    )


_TIER_RANK = {"cold": 0, "warm": 1, "hot": 2}


def _tag_lead(conn, ctx, call, **_) -> ToolResult:
    tier = str(call.args["tier"]).lower()
    if tier not in _TIER_RANK:
        return ToolResult(call=call, ok=False, data={},
                          error="tier must be hot, warm or cold")

    product_id = None
    sku = call.args.get("product_sku")
    if sku:
        found = conn.execute(
            "select id from products where business_id = %s and upper(sku) = upper(%s)",
            (ctx.business_id, str(sku)),
        ).fetchone()
        product_id = found["id"] if found else None

    existing = conn.execute(
        "select id, tier from leads where business_id = %s and customer_id = %s "
        "and status = 'open'",
        (ctx.business_id, ctx.customer_id),
    ).fetchone()

    if existing:
        # A lead only warms. A customer who asked for the price and then said
        # "just looking" is still the customer who asked for the price, and
        # overwriting hot with cold loses the only signal the owner acts on.
        keep = existing["tier"] if _TIER_RANK[existing["tier"]] > _TIER_RANK[tier] \
            else tier
        conn.execute(
            """
            update leads set tier = %s, reason = %s, intent = %s, confidence = %s,
                             product_id = coalesce(%s, product_id), updated_at = now()
             where id = %s
            """,
            (keep, call.args["reason"], call.args.get("intent"),
             call.args.get("confidence"), product_id, existing["id"]),
        )
        return ToolResult(call=call, ok=True,
                          data={"lead": keep, "updated": True,
                                "note": ("kept the higher tier" if keep != tier else "")})

    conn.execute(
        """
        insert into leads (business_id, customer_id, conversation_id, tier, reason,
                           intent, product_id, confidence)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (ctx.business_id, ctx.customer_id, ctx.conversation_id, tier,
         call.args["reason"], call.args.get("intent"), product_id,
         call.args.get("confidence")),
    )
    return ToolResult(call=call, ok=True, data={"lead": tier, "updated": False})


def _remember_customer(conn, ctx, call, **_) -> ToolResult:
    notes = call.args["notes"]
    if not isinstance(notes, dict) or not notes:
        return ToolResult(call=call, ok=False, data={}, error="nothing to remember")
    conn.execute(
        # ::jsonb because psycopg adapts Json to `json`, and `jsonb || json`
        # has no operator.
        "update customers set notes = notes || %s::jsonb, last_seen_at = now() "
        "where id = %s",
        (psycopg.types.json.Json(notes), ctx.customer_id),
    )
    return ToolResult(call=call, ok=True, data={"remembered": sorted(notes)})


def _schedule_follow_up(conn, ctx, call, *, queue=None, **_) -> ToolResult:
    if queue is None:
        return ToolResult(call=call, ok=False, data={},
                          error="follow-ups are not available in this context")

    conversation = conn.execute(
        "select last_inbound_at, channel from conversations where id = %s",
        (ctx.conversation_id,),
    ).fetchone()
    policy = followup.FollowUpPolicy.from_settings(ctx.business.settings)

    follow_up_id, skip = followup.schedule(
        conn, queue, business_id=ctx.business_id, customer_id=ctx.customer_id,
        conversation_id=ctx.conversation_id, reason=call.args["reason"],
        policy=policy,
        last_inbound_at=conversation["last_inbound_at"] if conversation else None,
        channel=conversation["channel"] if conversation else "simulator",
        in_minutes=call.args.get("in_minutes"),
    )
    if skip:
        # Told to the model so it does not promise a follow-up that will not
        # happen. A customer told "I will check back" and never contacted again
        # is worse off than one told nothing.
        return ToolResult(call=call, ok=True,
                          data={"scheduled": False, "reason": skip,
                                "instruction": "Do NOT tell the customer you will "
                                               "follow up. Say you will be here if "
                                               "they need anything."})
    return ToolResult(call=call, ok=True,
                      data={"scheduled": True, "follow_up_id": follow_up_id,
                            "instruction": "You may tell the customer you will "
                                           "check back with them."})


def _escalate_to_human(conn, ctx, call, **_) -> ToolResult:
    urgency = str(call.args["urgency"]).lower()
    handover = urgency == "handover"
    conn.execute(
        """
        update conversations
           set needs_attention = true, attention_reason = %s,
               status = case when %s then 'human'::conversation_status else status end
         where id = %s
        """,
        (call.args["reason"], handover, ctx.conversation_id),
    )
    # Not marked silent even on handover: the customer is owed one sentence
    # telling them a person is coming, and the turn sends a fixed line rather
    # than the model's own words. Only `no_reply` means nothing at all.
    return ToolResult(
        call=call, ok=True,
        data={"handed_over": handover, "reason": call.args["reason"],
              "instruction": ("A person is taking over. Say one short sentence "
                              "telling the customer someone is coming."
                              if handover else
                              "Someone will look at this. Carry on helping if you can.")},
    )


def _no_reply(conn, ctx, call, **_) -> ToolResult:
    return ToolResult(call=call, ok=True, silent=True,
                      data={"silent": True, "reason": call.args["reason"]})


_HANDLERS: dict[str, Callable] = {
    "search_products": _search,
    "get_product": _get_product,
    "create_order": _create_order,
    "create_payment_link": _create_payment_link,
    "get_payment_status": _get_payment_status,
    "tag_lead": _tag_lead,
    "remember_customer": _remember_customer,
    "schedule_follow_up": _schedule_follow_up,
    "escalate_to_human": _escalate_to_human,
    "no_reply": _no_reply,
}
