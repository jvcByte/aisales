"""Handling one customer message, from arrival to reply.

`record_inbound` is the front door: it turns a normalised `Inbound` into rows
and a queued turn. `run_turn` -- the model, the tools and the guard -- is
added with the tool set; intake is separable and comes first because the
de-duplication and coalescing rules are what keep everything downstream sane.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg

from aisales import db, followup as followup_mod, guard, prompts, tools
from aisales.channels import Inbound, Outbound
from aisales.providers import Chat, ProviderError, ToolCall, Turn

TURN = "agent_turn"
FOLLOW_UP = "follow_up"

#: How many times the model may call tools before the turn is abandoned. Four
#: is enough for look-up, quote, order, link; beyond it the model is looping,
#: and a loop that keeps calling tools costs money and never ends.
MAX_TOOL_ROUNDS = 4

#: The prompt window. Facts that still matter live in the state block, which is
#: regenerated from SQL every turn, so a short window loses conversation and
#: not state.
WINDOW_MESSAGES = 20
WINDOW_CHARS = 8000
SUMMARISE_AFTER = 40

FLAG = "flag"          # needs_attention; the AI keeps answering
HANDOVER = "handover"  # needs_attention AND the AI goes silent

#: Sent when the agent is blocked or handing over. A module constant and NOT
#: model output: a model asked to apologise for an unsafe reply can produce
#: another unsafe reply, which would defeat the block it is apologising for.
ESCALATION_LINE = "Let me get someone to confirm that for you — one moment."

#: Audit actions for tools, and a closed set for the same reason the table's
#: comment gives: /insights counts these, and free text counts nothing. Reads
#: fall back to `tool_called` rather than each needing its own row.
_TOOL_ACTIONS = {
    "create_order": "order_created",
    "create_payment_link": "payment_link_sent",
    "get_payment_status": "payment_checked",
    "tag_lead": "lead_tagged",
    "remember_customer": "customer_noted",
    "schedule_follow_up": "follow_up_scheduled",
    "no_reply": "no_reply",
}


@dataclass(frozen=True)
class Intent:
    name: str = "unknown"
    confidence: float = 0.0
    slots: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TurnResult:
    reply: str = ""
    silent: bool = False
    blocked: bool = False
    severity: str | None = None
    reason: str = ""
    model: str = ""
    intent: Intent = field(default_factory=Intent)
    tool_calls: list[dict] = field(default_factory=list)
    allowed_naira: list[int] = field(default_factory=list)
    message_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class IntakeResult:
    conversation_id: str
    customer_id: str
    business_id: str
    #: None when the delivery was a duplicate and nothing was recorded.
    message_id: int | None = None
    #: None when no turn was queued -- a duplicate, or one already pending.
    job_id: str | None = None
    duplicate: bool = False


def business_by_slug(conn: psycopg.Connection, slug: str) -> db.Business | None:
    row = conn.execute(
        "select id, slug, name, settings, suspended_at, suspended_reason from businesses where slug = %s", (slug,)
    ).fetchone()
    return db.Business.from_row(row) if row else None


def business_by_id(conn: psycopg.Connection, business_id: str) -> db.Business | None:
    row = conn.execute(
        "select id, slug, name, settings, suspended_at, suspended_reason from businesses where id = %s", (business_id,)
    ).fetchone()
    return db.Business.from_row(row) if row else None


def record_inbound(conn: psycopg.Connection, queue: db.Queue, business: db.Business,
                   message: Inbound) -> IntakeResult:
    """Record one customer message and arrange for it to be answered.

    Everything here is idempotent, because every layer above it retries: Meta
    redelivers webhooks it thinks failed, a customer double-taps send, and a
    worker can be reaped mid-turn. The three idempotency points are the
    message's provider id, the conversation's one-open-thread index, and the
    job's idempotency key.

    The turn is queued, not run. The webhook must answer the platform before
    any model call happens, or the platform retries a slow endpoint and the
    number's quality rating suffers.
    """
    phone = message.phone_e164

    customer = conn.execute(
        """
        insert into customers (business_id, phone_e164, phone_raw, last_seen_at)
        values (%s, %s, %s, now())
        on conflict (business_id, phone_e164) do update
           set last_seen_at = now()
        returning id
        """,
        (business.id, phone, message.from_phone),
    ).fetchone()
    customer_id = str(customer["id"])

    conversation_id = _open_conversation(conn, business.id, customer_id, message.channel)

    # A customer who has just spoken is not a customer to chase. Cancelling
    # before recording means a follow-up can never race the reply to it.
    conn.execute(
        """
        update follow_ups set status = 'cancelled', skipped_reason = 'customer_replied'
         where conversation_id = %s and status = 'scheduled'
        """,
        (conversation_id,),
    )

    stored = conn.execute(
        """
        insert into messages (business_id, conversation_id, role, body,
                              meta, provider_message_id)
        values (%s, %s, 'customer', %s, %s, %s)
        on conflict (business_id, provider_message_id) do nothing
        returning id
        """,
        (business.id, conversation_id, message.text,
         psycopg.types.json.Json({"kind": message.kind,
                                  "mime_type": message.mime_type}),
         message.external_id),
    ).fetchone()

    if stored is None:
        # This exact delivery is already recorded. Nothing else may happen:
        # storing it twice is harmless, but answering it twice is not.
        return IntakeResult(conversation_id=conversation_id, customer_id=customer_id,
                            business_id=business.id, duplicate=True)

    message_id = stored["id"]
    owned = conn.execute(
        "update conversations set last_message_at = now(), last_inbound_at = now(), "
        "needs_attention = false where id = %s returning status",
        (conversation_id,),
    ).fetchone()["status"]

    # Suspension and takeover are the same decision -- do not answer -- reached
    # from different directions, so they take the same shape: store what the
    # customer said, queue nothing, and leave a record saying why.
    if business.suspended:
        conn.execute(
            """
            update conversations
               set needs_attention = true,
                   attention_reason = 'the agent is paused for this business'
             where id = %s
            """,
            (conversation_id,),
        )
        audit(conn, business_id=business.id, conversation_id=conversation_id,
              action="suspended_suppressed_turn",
              detail={"reason": "the business is suspended", "message_id": message_id},
              actor="system")
        return IntakeResult(conversation_id=conversation_id, customer_id=customer_id,
                            business_id=business.id, message_id=message_id)

    if owned == "human":
        # A person owns this thread. The message is stored so they can see it,
        # and no turn is queued -- running one would spend a model call to reach
        # the same decision in run_turn, and the record that the AI stayed
        # quiet belongs here rather than there.
        audit(conn, business_id=business.id, conversation_id=conversation_id,
              action="takeover_suppressed_turn",
              detail={"reason": "a person owns this conversation",
                      "message_id": message_id}, actor="system")
        return IntakeResult(conversation_id=conversation_id, customer_id=customer_id,
                            business_id=business.id, message_id=message_id)

    # None means a turn is already queued or running for this conversation --
    # the customer's second and third messages in a burst. They are not lost:
    # the running turn re-reads the thread before it replies, so it answers
    # everything that arrived while it was thinking.
    job_id = queue.submit(
        TURN,
        business_id=business.id,
        conversation_id=conversation_id,
        payload={"message_id": message_id},
        idempotency_key=f"agent:{message_id}",
        conn=conn,
    )

    return IntakeResult(conversation_id=conversation_id, customer_id=customer_id,
                        business_id=business.id, message_id=message_id, job_id=job_id)


def _open_conversation(conn: psycopg.Connection, business_id: str, customer_id: str,
                       channel: str) -> str:
    """The customer's live thread on this channel, created if absent.

    A second inbound message must join the thread the agent is already
    answering. A parallel thread would give the guard and the summariser two
    halves of one conversation.
    """
    found = conn.execute(
        """
        select id from conversations
         where business_id = %s and customer_id = %s and channel = %s::conversation_channel
           and status <> 'closed'
        """,
        (business_id, customer_id, channel),
    ).fetchone()
    if found:
        return str(found["id"])

    try:
        row = conn.execute(
            """
            insert into conversations (business_id, customer_id, channel)
            values (%s, %s, %s::conversation_channel)
            returning id
            """,
            (business_id, customer_id, channel),
        ).fetchone()
        return str(row["id"])
    except psycopg.errors.UniqueViolation:
        # Another delivery created it between the select and the insert. Its
        # row is the live one; ours lost the race and is discarded.
        row = conn.execute(
            """
            select id from conversations
             where business_id = %s and customer_id = %s and channel = %s::conversation_channel
               and status <> 'closed'
            """,
            (business_id, customer_id, channel),
        ).fetchone()
        if row is None:  # pragma: no cover - the row cannot vanish underneath us
            raise
        return str(row["id"])


def unanswered_after(conn: psycopg.Connection, conversation_id: str) -> bool:
    """Has the customer spoken since we last replied?

    This exists to close a race that coalescing opens. A turn reads the thread,
    thinks for a few seconds, then replies. A message arriving in that window
    is correctly refused a second turn -- it must fold into the running one --
    but the running one has already read the thread and will never see it. The
    message is not lost (it is stored), but it is never answered.

    The comparison is against `answered_upto_message_id`, which the turn writes
    itself, and not against the newest reply. A mid-turn message has a *lower*
    id than the reply that ignored it, so any ordering-based test calls it
    answered. Only the turn knows what the turn read.

    The worker asks this after every turn and queues another if the answer is
    yes. One cheap query per turn buys the guarantee that no customer message
    is silently ignored.
    """
    row = conn.execute(
        """
        select 1
          from messages m
          join conversations cv on cv.id = m.conversation_id
         where m.conversation_id = %s
           and m.role = 'customer'
           and m.id > coalesce(cv.answered_upto_message_id, 0)
         limit 1
        """,
        (conversation_id,),
    ).fetchone()
    return row is not None


def requeue_if_unanswered(conn: psycopg.Connection, queue: db.Queue,
                          business_id: str, conversation_id: str) -> str | None:
    """Queue a follow-up turn if the customer spoke while we were replying.

    Call this *after* finishing the job that just ran. While that job is still
    leased the one-turn index will refuse this, which is correct but useless.
    """
    if not unanswered_after(conn, conversation_id):
        return None
    return queue.submit(TURN, business_id=business_id, conversation_id=conversation_id,
                        payload={}, idempotency_key=None, conn=conn)


def record_outbound(conn: psycopg.Connection, *, business_id: str, conversation_id: str,
                    body: str, role: str = "ai", provider_message_id: str | None = None,
                    meta: dict | None = None, cost_kobo: int = 0,
                    answered_upto: int | None = None) -> int:
    """Record a message we sent.

    Called *after* the channel reports success, never before: a send that
    failed must leave no message behind, or the transcript claims the customer
    was told something they never received.

    `answered_upto` is the newest message id the turn read before composing
    this reply, and advancing it is what tells the worker that anything newer
    still needs an answer. It is the turn's own account of what it saw, which
    is the only reliable source for that -- see `unanswered_after`.
    """
    row = conn.execute(
        """
        insert into messages (business_id, conversation_id, role, body, meta,
                              provider_message_id, cost_kobo)
        values (%s, %s, %s::message_role, %s, %s, %s, %s)
        returning id
        """,
        (business_id, conversation_id, role, body,
         psycopg.types.json.Json(meta or {}), provider_message_id, cost_kobo),
    ).fetchone()
    conn.execute(
        """
        update conversations
           set last_message_at = now(),
               answered_upto_message_id =
                   greatest(coalesce(answered_upto_message_id, 0), coalesce(%s, 0))
         where id = %s
        """,
        (answered_upto, conversation_id),
    )
    return row["id"]


def window(conn: psycopg.Connection, conversation_id: str,
           limit: int = WINDOW_MESSAGES, max_chars: int = WINDOW_CHARS) -> list[dict]:
    """The messages the model is shown, oldest first.

    Trimmed by count and by length. The newest turn is always kept whole even
    if it alone exceeds the budget, because dropping it means answering a
    question the model cannot see.
    """
    rows = conn.execute(
        """
        select id, role, body from messages
         where conversation_id = %s order by id desc limit %s
        """,
        (conversation_id, limit),
    ).fetchall()
    rows = list(reversed(rows))

    kept, budget = [], max_chars
    for index, row in enumerate(rows):
        cost = len(row["body"])
        if budget - cost < 0 and kept:
            break
        budget -= cost
        kept.append(row)
        if index == len(rows) - 1:
            break
    return kept


def state_block(conn: psycopg.Connection, ctx: db.TurnContext,
                conversation: dict) -> tuple[str, tuple[int, ...]]:
    """What the agent knows about this customer, generated from SQL.

    Returns the text and the currency figures it contains, which join the
    allowed set. This is the reason the window can be short: an order, a
    reference and a note are *rows*, so they are present every turn no matter
    how long the conversation has run. Anything discussed sixty messages ago
    that still matters is here because it is data, not because it was said.

    The corollary is the safety property: prose cannot put a number here, so a
    price quoted from the rolling summary is not quotable.
    """
    money: list[int] = []
    lines = [f"Today is {conn.execute('select now() as t').fetchone()['t']:%A %d %B %Y}.",
             f"The customer's number is {conversation['phone_e164']}."]

    if conversation.get("name"):
        lines.append(f"They gave their name as {conversation['name']}.")

    notes = conversation.get("notes") or {}
    if notes:
        lines.append("You previously noted: "
                     + "; ".join(f"{k}: {v}" for k, v in sorted(notes.items())) + ".")

    order = conn.execute(
        """
        select reference, status, total_kobo from orders
         where conversation_id = %s and status not in ('cancelled', 'fulfilled')
         order by created_at desc limit 1
        """,
        (ctx.conversation_id,),
    ).fetchone()
    if order:
        money.append(order["total_kobo"])
        lines.append(f"Open order {order['reference']}: {order['status']}, "
                     f"₦{order['total_kobo'] // 100:,}.")
    else:
        lines.append("This customer has no open order.")

    lead = conn.execute(
        "select tier, reason from leads where business_id = %s and customer_id = %s "
        "and status = 'open'",
        (ctx.business_id, ctx.customer_id),
    ).fetchone()
    if lead:
        lines.append(f"Lead: {lead['tier']} — {lead['reason']}.")

    pending = conn.execute(
        "select due_at from follow_ups where conversation_id = %s and status = 'scheduled'",
        (ctx.conversation_id,),
    ).fetchone()
    if pending:
        lines.append(f"A follow-up is already scheduled for {pending['due_at']:%d %B %H:%M}.")

    if conversation["status"] == "human":
        lines.append("A member of staff has taken over this conversation.")
    elif conversation["needs_attention"]:
        lines.append("Someone has been asked to look at this conversation.")

    return "\n".join(f"- {line}" for line in lines), tuple(money)


def classify_intent(chat: Chat, text: str) -> Intent:
    """One inbound message into one member of the closed intent set.

    A real product function, not test scaffolding: it drives escalation (a
    discount request must never receive an invented discount) and the daily
    digest ("12 people asked for a discount this week").

    Degrades rather than raises. A classifier that fails a turn because it
    could not parse its own output has turned a labelling problem into a
    lost sale.
    """
    if not text.strip():
        return Intent()
    try:
        turn = chat.chat(
            [{"role": "system", "content": prompts.intent_prompt()},
             {"role": "user", "content": text}],
            [],
        )
    except ProviderError:
        return Intent()

    raw = turn.text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):] if "{" in raw else raw
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return Intent()

    name = str(parsed.get("intent") or "unknown")
    if name not in prompts.INTENTS:
        name = "unknown"
    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    slots = parsed.get("slots")
    return Intent(name=name, confidence=confidence,
                  slots=slots if isinstance(slots, dict) else {})


def escalation_decision(*, verdict: guard.Verdict, intent: Intent,
                        tool_escalation: tuple[str, str] | None,
                        business: db.Business, reply: str,
                        unknown_streak: int = 0) -> tuple[str, str] | None:
    """(severity, reason), or None to just send it.

    One function rather than checks scattered through the tools, so the
    escalation policy can be read and tested as a single thing. Ordered, first
    match wins.
    """
    if verdict.blocked:
        return HANDOVER, "guard_blocked"
    if intent.name in business.escalate_on:
        return HANDOVER, intent.name
    if intent.name in business.flag_on:
        return FLAG, intent.name
    if tool_escalation is not None:
        return tool_escalation
    if unknown_streak >= 2:
        return FLAG, "unclassified"
    # Low confidence is only worth a human when the reply committed to
    # something. An unsure "let me check" needs no intervention.
    if (intent.confidence < business.confidence_threshold
            and guard.extract_money(reply)):
        return FLAG, "low_confidence"
    return None


def _as_model_messages(thread: list[dict]) -> list[dict]:
    """Conversation rows as chat messages.

    Staff messages map to `assistant`, so on handback the model reads a
    colleague's promises as things it said itself and does not contradict
    them. That is the single most likely way this feature loses a shop's
    trust, and it is fixed here rather than in the prompt.
    """
    mapping = {"customer": "user", "ai": "assistant", "staff": "assistant"}
    return [{"role": mapping.get(row["role"], "user"), "content": row["body"]}
            for row in thread]


def summarise_old(conn: psycopg.Connection, ctx: db.TurnContext, chat: Chat) -> None:
    """Fold the part of the thread older than the window into a rolling summary.

    Lazy, from inside a turn, rather than on a schedule: the summariser is only
    needed when a conversation has grown, and a cron that summarises quiet
    threads spends money on conversations nobody is having.
    """
    total = conn.execute(
        "select count(*) as n from messages where conversation_id = %s",
        (ctx.conversation_id,),
    ).fetchone()["n"]
    if total <= SUMMARISE_AFTER:
        return

    cutoff = conn.execute(
        """
        select id from messages where conversation_id = %s
         order by id desc offset %s limit 1
        """,
        (ctx.conversation_id, WINDOW_MESSAGES),
    ).fetchone()
    if cutoff is None:
        return

    older = conn.execute(
        """
        select role, body from messages
         where conversation_id = %s and id <= %s order by id
        """,
        (ctx.conversation_id, cutoff["id"]),
    ).fetchall()
    if not older:
        return

    transcript = "\n".join(f"{row['role']}: {row['body']}" for row in older)[:6000]
    try:
        turn = chat.chat([
            {"role": "system", "content":
             "Summarise this shop's WhatsApp conversation in under 120 words. "
             "Keep what a shop assistant would need next: what the customer "
             "wanted, sizes, colours, budget, anything agreed. Do not invent "
             "prices or promises. No preamble."},
            {"role": "user", "content": transcript},
        ], [])
    except ProviderError:
        return
    if not turn.text.strip():
        return

    conn.execute(
        "update conversations set summary = %s, summarized_upto_message_id = %s "
        "where id = %s",
        (turn.text.strip(), cutoff["id"], ctx.conversation_id),
    )


def audit(conn: psycopg.Connection, *, business_id: str, action: str,
          conversation_id: str | None = None, actor: str = "ai",
          detail: dict | None = None, model: str | None = None) -> None:
    """Append to the audit log.

    `action` is a closed set -- see the comment on the table -- because
    /insights counts these and free text counts nothing.
    """
    conn.execute(
        """
        insert into audit_log (business_id, conversation_id, actor, action, detail, model)
        values (%s, %s, %s, %s, %s, %s)
        """,
        (business_id, conversation_id, actor, action,
         psycopg.types.json.Json(detail or {}), model),
    )


def _load_conversation(conn: psycopg.Connection, conversation_id: str,
                       business_id: str | None = None) -> dict | None:
    """The thread, its customer and its notes.

    `business_id` is optional only because the tests call this with a known
    conversation. Every production caller has it -- the job carries it -- and
    passing it makes the read correct on a connection where the tenant policy
    is not in force, rather than merely protected by it.
    """
    return conn.execute(
        """
        select cv.id, cv.status, cv.channel, cv.needs_attention, cv.summary,
               cv.last_inbound_at,
               cu.id as customer_id, cu.phone_e164, cu.name, cu.notes
          from conversations cv
          join customers cu on cu.id = cv.customer_id
         where cv.id = %s
           and (%s::uuid is null or cv.business_id = %s::uuid)
        """,
        (conversation_id, business_id, business_id),
    ).fetchone()


def _escalate(conn: psycopg.Connection, ctx: db.TurnContext, severity: str,
              reason: str) -> None:
    handover = severity == HANDOVER
    conn.execute(
        """
        update conversations
           set needs_attention = true, attention_reason = %s,
               status = case when %s then 'human'::conversation_status else status end
         where id = %s
        """,
        (reason, handover, ctx.conversation_id),
    )
    audit(conn, business_id=ctx.business_id, conversation_id=ctx.conversation_id,
          action="escalated", detail={"severity": severity, "reason": reason})


def _restate_after_block(messages: list[dict], verdict: guard.Verdict) -> list[dict]:
    """Ask again, telling the model exactly what it may not say.

    One retry, because a blocked reply is usually a slip rather than a
    misunderstanding -- the right figure was in the tool results and the model
    rounded it. A second block is a pattern, and a pattern is for a person.
    """
    allowed = ", ".join(f"₦{k // 100:,}" for k in sorted(verdict.detail.get("allowed_kobo", [])))
    offence = ", ".join(verdict.detail.get("tokens", [])) or verdict.detail.get("phrase", "")
    # Telling the model it may call a tool again matters: the allowed set is
    # per turn, so a price quoted earlier in the conversation is not quotable
    # now even though the model remembers it. Without this sentence the model
    # tends to restate the remembered figure and be blocked a second time.
    return [*messages, {
        "role": "user",
        "content": (
            f"Your reply was not sent. It contained {offence}, which you cannot "
            f"say: {verdict.reason}. The only amounts you may state right now "
            f"are: {allowed or 'none'}. If you need to quote a price, look it up "
            "with a tool first -- a figure from earlier in the conversation is "
            "not enough. Otherwise rewrite without any amount, or say you will "
            "confirm."
        ),
    }]


def _converse(conn: psycopg.Connection, ctx: db.TurnContext, chat: Chat,
              messages: list[dict], allowed: guard.Allowed, *,
              paystack=None, queue=None) -> tuple:
    """Run the model-and-tools loop until it concludes.

    Factored out because it runs twice: once normally, and again after the
    guard blocks a reply. The second run must be a *turn*, not just another
    text generation -- a model told to look the price up again will call a
    tool, and a retry path that only reads `text` silently discards that call
    and sends nothing at all.
    """
    calls_made: list[dict] = []
    tool_escalation: tuple[str, str] | None = None
    reply, model, silent = "", "", False

    for _ in range(MAX_TOOL_ROUNDS):
        turn: Turn = chat.chat(messages, tools.SCHEMAS)
        model = turn.model or model

        if not turn.calls:
            reply = turn.text
            break

        messages.append({"role": "assistant", "content": turn.text or None,
                         "tool_calls": [c.as_message() for c in turn.calls]})
        for call in turn.calls:
            result = tools.dispatch(conn, ctx, call, paystack=paystack, queue=queue)
            messages.append(result.to_message())
            allowed = guard.Allowed(
                money_kobo=allowed.money_kobo | frozenset(result.money_kobo),
                availability=allowed.availability or result.availability,
                authority=allowed.authority or result.authority,
                payment_confirmed=(allowed.payment_confirmed
                                   or result.payment_confirmed),
            )
            calls_made.append({"name": call.name, "args": call.args, "ok": result.ok,
                               "data": result.data if result.ok
                                       else {"error": result.error}})
            if result.silent:
                silent = True
            if call.name == "escalate_to_human":
                tool_escalation = (str(call.args.get("urgency") or FLAG).lower(),
                                   str(call.args.get("reason") or "the model asked"))
            # EVERY tool call, not only the ones that change something -- the
            # reads are what answer "why did it say ₦12,500?". Auditing only
            # the writes leaves a quote with no visible cause, which is the
            # exact question the trail exists for. `escalate_to_human` is
            # excluded because `_escalate` writes its own, richer entry.
            if call.name != "escalate_to_human":
                audit(conn, business_id=ctx.business_id,
                      conversation_id=ctx.conversation_id, actor="ai",
                      action=_TOOL_ACTIONS.get(call.name, "tool_called"),
                      detail={"tool": call.name, "args": call.args,
                              "ok": result.ok,
                              **({} if result.ok else {"error": result.error})},
                      model=model)
        if silent:
            break
    else:
        # Every round called tools and none concluded. A model looping is not
        # going to stop on its own, and it costs money to watch it try.
        tool_escalation = tool_escalation or (HANDOVER, "tool_loop")

    return reply, allowed, calls_made, tool_escalation, silent, model


def run_turn(conn: psycopg.Connection, channel, chat: Chat, ctx: db.TurnContext, *,
             paystack=None, queue=None) -> TurnResult:
    """One turn: read, think, check, and either send or escalate.

    The order of the last few steps is the whole design. `channel.send` happens
    *before* the outbound row is written, so a failed send leaves no message
    and the transcript never claims the customer was told something they were
    not. And the guard runs before either, so nothing reaches a customer
    without having been checked.
    """
    conv = _load_conversation(conn, ctx.conversation_id)
    if conv is None:
        return TurnResult(silent=True, reason="no such conversation")

    # Suspension is checked here as well as at intake, because a job queued
    # before the flag flipped is already claimed and running. This is the last
    # point before a model call is paid for; the truly last is the send below,
    # and nothing is added past that because nothing above it is recoverable.
    if ctx.business.suspended:
        audit(conn, business_id=ctx.business_id, conversation_id=ctx.conversation_id,
              action="suspended_suppressed_turn",
              detail={"reason": "the business is suspended"})
        return TurnResult(silent=True, reason="the business is suspended")

    # Ownership is checked here as well as at intake: a staff member can take
    # over between the message arriving and the worker picking it up.
    if conv["status"] == "human":
        audit(conn, business_id=ctx.business_id, conversation_id=ctx.conversation_id,
              action="takeover_suppressed_turn",
              detail={"reason": "a person owns this conversation"})
        return TurnResult(silent=True, reason="a person owns this conversation")

    thread = window(conn, ctx.conversation_id)
    if not thread:
        return TurnResult(silent=True, reason="nothing to answer")
    answered_upto = max(row["id"] for row in thread)

    summarise_old(conn, ctx, chat)

    state, state_money = state_block(conn, ctx, conv)
    newest = thread[-1]["body"] if thread[-1]["role"] == "customer" else ""
    intent = classify_intent(chat, newest)

    messages: list[dict] = [
        {"role": "system", "content": prompts.system_prompt(ctx.business, state)},
        *_as_model_messages(thread),
    ]
    allowed = guard.Allowed(money_kobo=frozenset(state_money))
    try:
        reply, allowed, calls_made, tool_escalation, silent, model = _converse(
            conn, ctx, chat, messages, allowed, paystack=paystack, queue=queue)
    except ProviderError as exc:
        return _providers_exhausted(conn, ctx, channel, exc)

    if silent:
        audit(conn, business_id=ctx.business_id, conversation_id=ctx.conversation_id,
              action="no_reply", detail={"calls": calls_made}, model=model)
        return TurnResult(silent=True, reason="the agent chose silence", model=model,
                          intent=intent, tool_calls=calls_made,
                          allowed_naira=sorted(allowed.naira))

    verdict = guard.check(reply, allowed)
    if verdict.blocked:
        # Recorded here rather than after the retry, because the retry often
        # succeeds -- and a block the owner cannot see is a block they cannot
        # judge. Requirement 4.8 is about the block happening, not about the
        # reply being sent.
        audit(conn, business_id=ctx.business_id, conversation_id=ctx.conversation_id,
              action="guard_blocked",
              detail={"rule": verdict.rule, "reason": verdict.reason,
                      **verdict.detail}, model=model)
        # One retry, and it is a full turn rather than a re-generation: the
        # model is told to look the figure up, so it will call a tool, and the
        # call has to be dispatched and its amounts added to the allowed set.
        try:
            retry_messages = _restate_after_block(messages, verdict)
            reply, allowed, more_calls, more_escalation, silent, model = _converse(
                conn, ctx, chat, retry_messages, allowed,
                paystack=paystack, queue=queue)
            calls_made = [*calls_made, *more_calls]
            tool_escalation = tool_escalation or more_escalation
            verdict = guard.check(reply, allowed)
        except ProviderError:
            pass

    decision = escalation_decision(
        verdict=verdict, intent=intent, tool_escalation=tool_escalation,
        business=ctx.business, reply=reply,
    )

    if decision is not None:
        severity, why = decision
        _escalate(conn, ctx, severity, why)
        outbound = ESCALATION_LINE if severity == HANDOVER else reply
    else:
        outbound = reply

    if not outbound.strip():
        return TurnResult(silent=True, reason="the model produced nothing", model=model,
                          intent=intent, tool_calls=calls_made,
                          allowed_naira=sorted(allowed.naira))

    # Send first, record second. A failed send must leave no message behind, or
    # the transcript says the customer was told something they never received
    # and the next turn reasons from a conversation that did not happen.
    channel.send(Outbound(to_phone=conv["phone_e164"], text=outbound))
    record_outbound(conn, business_id=ctx.business_id,
                    conversation_id=ctx.conversation_id, body=outbound,
                    answered_upto=answered_upto, meta={"intent": intent.name})

    if guard.extract_money(outbound):
        audit(conn, business_id=ctx.business_id, conversation_id=ctx.conversation_id,
              action="price_quoted", detail={"body": outbound}, model=model)
    if newest:
        conn.execute(
            # The ::jsonb cast is required, not cosmetic: psycopg adapts Json to
            # `json`, and `jsonb || json` has no operator in Postgres.
            "update messages set meta = meta || %s::jsonb where conversation_id = %s "
            "and body = %s and role = 'customer'",
            (psycopg.types.json.Json({"intent": intent.name}),
             ctx.conversation_id, newest),
        )

    return TurnResult(reply=outbound, blocked=verdict.blocked,
                      severity=decision[0] if decision else None,
                      reason=decision[1] if decision else "", model=model,
                      intent=intent, tool_calls=calls_made,
                      allowed_naira=sorted(allowed.naira),
                      message_ids=[row["id"] for row in thread])


def dispatch_follow_up(conn: psycopg.Connection, queue: db.Queue, job: db.Job, *,
                       chat: Chat, channel, paystack=None) -> str:
    """Send one scheduled follow-up. Returns an outcome for the job log.

    Deliberately re-reads everything from SQL. The job payload may be hours
    old, and every fact that decides whether to send has probably changed: the
    customer may have replied, a person may have taken over, a turn may be
    running right now.
    """
    follow_up = conn.execute(
        """
        select f.id, f.conversation_id, f.customer_id, f.business_id, f.reason,
               f.due_at, f.created_at, f.status, f.attempt
          from follow_ups f where f.id = %s
        """,
        (job.payload.get("follow_up_id"),),
    ).fetchone()
    if follow_up is None or follow_up["status"] != "scheduled":
        return "not_scheduled"

    conversation_id = str(follow_up["conversation_id"])
    business = business_by_id(conn, str(follow_up["business_id"]))
    if business is None:
        return "no_business"

    # Skipped rather than deferred: a suspended business has no date at which
    # it becomes answerable, so rescheduling would only produce a job that
    # fails the same way later. Resume is what schedules new work.
    if business.suspended:
        _close_follow_up(conn, str(follow_up["id"]), "skipped",
                         followup_mod.SkipReason.BUSINESS_SUSPENDED)
        return "business_suspended"

    # A live turn on this conversation would produce two messages answering
    # different things, moments apart. Checked rather than locked: an advisory
    # lock held across a model call is a long transaction, and a follow-up can
    # always go later -- so a failed check is a requeue, not a wait.
    live = conn.execute(
        """
        select 1 from jobs
         where conversation_id = %s and kind = %s
           and status in ('queued', 'leased', 'running')
         limit 1
        """,
        (conversation_id, TURN),
    ).fetchone()
    if live:
        return "turn_in_flight"

    conversation = _load_conversation(conn, conversation_id)
    if conversation is None or conversation["status"] == "human":
        _close_follow_up(conn, follow_up["id"], "skipped", followup_mod.SkipReason.HUMAN)
        return "human_owns_it"

    # The customer answering is the whole point of cancelling: nothing damages
    # this product more than a bot chasing someone who already replied.
    #
    # Measured from when the follow-up was *scheduled*, not when it is due. A
    # customer who replies an hour into a two-hour wait has replied, and
    # comparing against `due_at` would miss exactly that -- the common case.
    # `record_inbound` cancels these already; this is the backstop for a
    # message that arrived by some other route.
    replied = conn.execute(
        """
        select 1 from messages
         where conversation_id = %s and role = 'customer' and created_at > %s
         limit 1
        """,
        (conversation_id, follow_up["created_at"]),
    ).fetchone()
    if replied:
        _close_follow_up(conn, follow_up["id"], "cancelled",
                         followup_mod.SkipReason.CUSTOMER_REPLIED)
        return "customer_replied"

    policy = followup_mod.FollowUpPolicy.from_settings(business.settings)
    now = datetime.now(timezone.utc)
    if followup_mod.in_quiet_hours(policy, now):
        # Requeued rather than skipped: quiet hours end, and the customer is
        # asleep rather than uninterested.
        moved = followup_mod.next_open_moment(policy, now)
        conn.execute("update follow_ups set due_at = %s where id = %s",
                     (moved, follow_up["id"]))
        queue.submit(FOLLOW_UP, business_id=business.id,
                     conversation_id=conversation_id,
                     payload={"follow_up_id": str(follow_up["id"])},
                     not_before=moved,
                     idempotency_key=f"followup:{follow_up['id']}:{moved.isoformat()}",
                     conn=conn)
        return "quiet_hours"

    ctx = db.TurnContext(business=business, customer_id=str(follow_up["customer_id"]),
                         conversation_id=conversation_id, channel=conversation["channel"])
    state, state_money = state_block(conn, ctx, conversation)
    messages = [
        {"role": "system", "content": prompts.system_prompt(business, state)},
        {"role": "user", "content": (
            f"This customer went quiet. Come back to them about: {follow_up['reason']}. "
            f"Write ONE short message, at most two sentences, in this tone: "
            f"{policy.tone}. Do not invent a price -- look it up if you need it. "
            "If they have already been answered, use no_reply."
        )},
    ]
    allowed = guard.Allowed(money_kobo=frozenset(state_money))
    try:
        reply, allowed, calls, tool_escalation, silent, model = _converse(
            conn, ctx, chat, messages, allowed, paystack=paystack, queue=queue)
    except ProviderError as exc:
        # Not a failure of the follow-up: the allowance is spent. Requeued for
        # later, because a nudge that arrives tomorrow beats one that never does.
        conn.execute(
            "update follow_ups set due_at = now() + interval '4 hours' where id = %s",
            (follow_up["id"],))
        return f"deferred_no_model:{exc.reason}"

    if silent or not reply.strip():
        _close_follow_up(conn, follow_up["id"], "skipped", "nothing_to_say")
        return "silent"

    # The same guard as any other turn. A follow-up is not exempt from the
    # rules just because a person asked for it on a timer.
    verdict = guard.check(reply, allowed)
    if verdict.blocked:
        _close_follow_up(conn, follow_up["id"], "skipped", "guard_blocked")
        audit(conn, business_id=business.id, conversation_id=conversation_id,
              action="guard_blocked",
              detail={"rule": verdict.rule, "reason": verdict.reason,
                      "during": "follow_up", **verdict.detail}, model=model)
        return "guard_blocked"

    channel.send(Outbound(to_phone=conversation["phone_e164"], text=reply))
    record_outbound(conn, business_id=business.id, conversation_id=conversation_id,
                    body=reply, meta={"follow_up": str(follow_up["id"])})
    _close_follow_up(conn, follow_up["id"], "sent", None)
    audit(conn, business_id=business.id, conversation_id=conversation_id,
          action="follow_up_sent",
          detail={"reason": follow_up["reason"], "attempt": follow_up["attempt"]},
          model=model)
    return "sent"


def take_over(conn: psycopg.Connection, conversation_id: str, who: str = "staff") -> dict:
    """A person takes the thread. The AI goes silent from here.

    Lives here rather than in the route so the state machine can be tested
    without an HTTP server -- the route is a thin wrapper, and the interesting
    behaviour is entirely in the transitions.
    """
    conversation = conn.execute(
        "select business_id, status from conversations where id = %s", (conversation_id,)
    ).fetchone()
    if conversation is None:
        raise LookupError("no such conversation")

    conn.execute(
        """
        update conversations
           set status = 'human', assigned_to = %s, takeover_at = now(),
               needs_attention = false
         where id = %s
        """,
        (who, conversation_id),
    )
    audit(conn, business_id=str(conversation["business_id"]),
          conversation_id=conversation_id, actor=who, action="takeover",
          detail={"was": conversation["status"]})
    return {"conversation_id": conversation_id, "status": "human", "assigned_to": who}


def hand_back(conn: psycopg.Connection, queue: db.Queue, conversation_id: str) -> dict:
    """Return the thread to the AI.

    If the customer said anything while a person owned it, exactly one turn is
    queued now. Those messages were deliberately not answered at the time --
    `takeover_suppressed_turn` -- so without this they never would be, and the
    customer would be left talking into a void that looked handled.
    """
    conversation = conn.execute(
        "select business_id, status from conversations where id = %s", (conversation_id,)
    ).fetchone()
    if conversation is None:
        raise LookupError("no such conversation")

    conn.execute(
        "update conversations set status = 'ai', assigned_to = null, takeover_at = null "
        "where id = %s",
        (conversation_id,),
    )
    audit(conn, business_id=str(conversation["business_id"]),
          conversation_id=conversation_id, actor="staff", action="handback", detail={})
    queued = requeue_if_unanswered(conn, queue, str(conversation["business_id"]),
                                   conversation_id)
    return {"conversation_id": conversation_id, "status": "ai",
            "turn_queued": queued is not None}


def staff_reply(conn: psycopg.Connection, channel, conversation_id: str,
                text: str) -> dict:
    """A person replies. Delivered, then recorded, like any other message.

    Recorded as `role='staff'`, which is the point: on handback the model reads
    a colleague's messages as its own prior turns, so it does not resume and
    contradict what the person just promised. That is the single most likely
    way this feature loses a shop's trust, and it is fixed by the role rather
    than by asking the prompt nicely.
    """
    if not text.strip():
        raise ValueError("the message is empty")

    conversation = conn.execute(
        "select business_id, status, customer_id from conversations where id = %s",
        (conversation_id,),
    ).fetchone()
    if conversation is None:
        raise LookupError("no such conversation")
    if conversation["status"] != "human":
        # Refused rather than allowed: two voices answering one customer is
        # precisely what the ownership flag exists to prevent.
        raise PermissionError("the AI owns this thread; take it over first")

    customer = conn.execute("select phone_e164 from customers where id = %s",
                            (conversation["customer_id"],)).fetchone()
    channel.send(Outbound(to_phone=customer["phone_e164"], text=text))

    newest = conn.execute(
        "select coalesce(max(id), 0) as m from messages where conversation_id = %s",
        (conversation_id,),
    ).fetchone()["m"]
    # Advancing the watermark matters: without it a handback would re-answer a
    # question a person already answered.
    record_outbound(conn, business_id=str(conversation["business_id"]),
                    conversation_id=conversation_id, body=text, role="staff",
                    answered_upto=newest)
    return {"conversation_id": conversation_id, "sent": True}


def _close_follow_up(conn: psycopg.Connection, follow_up_id, status: str,
                     reason: str | None) -> None:
    conn.execute(
        "update follow_ups set status = %s::follow_up_status, skipped_reason = %s "
        "where id = %s",
        (status, reason, follow_up_id),
    )


def _providers_exhausted(conn: psycopg.Connection, ctx: db.TurnContext, channel,
                         exc: ProviderError) -> TurnResult:
    """What happens when every model in the chain has failed.

    A person takes over and the customer is told so, in a fixed sentence that
    needs no model. Deliberately not a retry on a long backoff: the dominant
    cause is a spent free-tier allowance, which is per model per day and does
    not clear while we wait, so a retry would only delay the human by hours.
    The property that matters is that the customer is never in silence, and a
    person is always reachable.
    """
    _escalate(conn, ctx, HANDOVER, f"no model available ({exc.reason})")
    audit(conn, business_id=ctx.business_id, conversation_id=ctx.conversation_id,
          action="providers_exhausted",
          detail={"reason": exc.reason, "message": str(exc)[:300]})

    conv = _load_conversation(conn, ctx.conversation_id)
    if conv is None:
        return TurnResult(silent=True, reason="providers_exhausted")
    channel.send(Outbound(to_phone=conv["phone_e164"], text=ESCALATION_LINE))
    record_outbound(conn, business_id=ctx.business_id,
                    conversation_id=ctx.conversation_id, body=ESCALATION_LINE,
                    role="system", meta={"reason": "providers_exhausted"})
    return TurnResult(reply=ESCALATION_LINE, severity=HANDOVER,
                      reason="providers_exhausted")


def thread(conn: psycopg.Connection, conversation_id: str, limit: int = 50) -> list[dict]:
    """The most recent messages, oldest first. What the model is shown."""
    rows = conn.execute(
        """
        select id, role, body, meta, cost_kobo, created_at
          from messages where conversation_id = %s
         order by id desc limit %s
        """,
        (conversation_id, limit),
    ).fetchall()
    return list(reversed(rows))


# ---------------------------------------------------- the business lifecycle


def suspend_business(conn: psycopg.Connection, business_id: str, *,
                     reason: str = "") -> dict:
    """Stop the agent answering for this business.

    Two writes, and no more. The flag stops new turns at intake; cancelling
    the scheduled follow-ups stops the ones already waiting, because a
    follow-up is the one thing that reaches a customer without a message
    arriving first.

    Queued turns are deliberately left alone. They are claimed once, skipped
    by the worker, and reach a terminal state -- there is no loop to break,
    and cancelling them would throw away work that resume is about to want
    back anyway.

    Runs on a tenant connection; the caller opens it.
    """
    conn.execute(
        "update businesses set suspended_at = now(), suspended_reason = %s where id = %s",
        (reason or None, business_id),
    )
    cancelled = conn.execute(
        """
        update follow_ups
           set status = 'cancelled', skipped_reason = %s
         where business_id = %s and status = 'scheduled'
        returning id
        """,
        (followup_mod.SkipReason.BUSINESS_SUSPENDED, business_id),
    ).fetchall()
    audit(conn, business_id=business_id, action="business_suspended",
          detail={"reason": reason, "cancelled_follow_ups": len(cancelled)},
          actor="operator")
    return {"cancelled_follow_ups": len(cancelled)}


def resume_business(conn: psycopg.Connection, queue: db.Queue, business_id: str) -> dict:
    """Start answering again, and pick up what was missed.

    Catch-up is limited to threads the customer spoke in *recently enough that
    a reply may still be sent* -- `within_service_window`, which is Meta's
    rule and not a preference. Outside it the message would be refused with
    error 131047, so queueing a turn there is queueing a guaranteed failure.

    Everything older is left flagged for a person. A backlog answered
    automatically the moment a switch is flipped is a backlog nobody read.
    """
    conn.execute(
        "update businesses set suspended_at = null, suspended_reason = null where id = %s",
        (business_id,),
    )

    conversations = conn.execute(
        """
        select id, channel, last_inbound_at from conversations
         where business_id = %s and status <> 'closed'
        """,
        (business_id,),
    ).fetchall()

    queued, stale = [], 0
    for conversation in conversations:
        conversation_id = str(conversation["id"])
        if not unanswered_after(conn, conversation_id):
            continue
        if not followup_mod.within_service_window(conversation["channel"],
                                                  conversation["last_inbound_at"]):
            stale += 1
            continue
        job_id = requeue_if_unanswered(conn, queue, business_id, conversation_id)
        if job_id:
            queued.append(job_id)

    audit(conn, business_id=business_id, action="business_resumed",
          detail={"caught_up": len(queued), "outside_window": stale}, actor="operator")
    return {"caught_up": len(queued), "outside_window": stale}
