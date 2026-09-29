# Implementation Plan: AI Sales Employee (V1 Prototype)

## Overview

Build the vertical slice — customer conversation → lead qualification → follow-up → payment →
order → business intelligence — as three Python distributions (`core`, `api`, `worker`) over
PostgreSQL, plus a Next.js dashboard.

Ordering is deliberate: the offline-testable core is built first, and each phase is independently
demonstrable. Phases 0–4 need no API keys, no WhatsApp access and no network. The live model arrives
at phase 5, real payment semantics (fake-signed, real algorithm) at phase 6, and the real WhatsApp
adapter at phase 9 — by which point the abstraction it slots into is already proven.

`_Requirements: X.Y_` gives traceability back to `requirements.md`. Tasks marked `*` are optional
and do not gate the phase.

---

## Tasks

- [x] 1. Schema, database layer and seed
  - [x] 1.1 Create `core/src/aisales/db.py` with `SCHEMA` (the full DDL from `design.md`), an
      idempotent `install()`, and `connect(dsn)` returning a connection with
      `autocommit=True, row_factory=dict_row`. Follow the existing local convention: every
      statement is `create ... if not exists`, and every column added after first install is also
      issued as `alter table ... add column if not exists`.
    - _Requirements: 13.1, 14.1_
  - [x] 1.2 Add row dataclasses for `Business`, `Customer`, `Conversation`, `Message`, `Product`,
      `Order`, `Lead`, `FollowUp`, plus `TurnContext` carrying `business_id`. Flat, in `db.py`.
    - _Requirements: 13.1_
  - [x]* 1.3 Add a `ponytail:` comment at the tenancy boundary naming the RLS upgrade path
      (`enable row level security` + a policy on `current_setting('aisales.business_id')` +
      `set_config` in `connect()`), and the ceiling that a forgotten `WHERE` returns another
      tenant's rows.
    - _Requirements: 13.2_
  - [x] 1.4 Write the seed in `worker/src/aisales_worker/seed.py`: one business (`slug`
      `adafabrics`), `settings` per the documented shape, and 8 products with realistic Nigerian
      names, integer kobo prices, variants and mixed stock (including one `stock_qty` NULL to prove
      "not tracked" differs from zero).
    - _Requirements: 2.1, 2.2_
  - [x] 1.5 Wire `python -m aisales_worker --install --seed` and confirm `\dt` shows the tables.
    - _Requirements: 14.1_

- [x] 2. The queue
  - [x] 2.1 Implement `Queue` in `db.py`: `submit` (with `idempotency_key`), `claim`,
      `heartbeat`, `finish`, `fail`, `reap`. `claim` uses `FOR UPDATE SKIP LOCKED` with a lease and
      **must** filter `not_before <= now()`.
    - _Requirements: 14.1, 14.2, 14.3, 14.6_
  - [x] 2.2 Implement `fail()` to reschedule with increasing delay up to `max_attempts`, then mark
      failed.
    - _Requirements: 14.5_
  - [x] 2.3 Implement `reap()` to return expired leases to `queued`.
    - _Requirements: 14.4_
  - [x] 2.4 Write `core/tests/test_queue.py` covering: two concurrent claims yield different jobs;
      a leased job is not claimable; an expired lease is reaped; a job with
      `not_before = now() + 10s` is **not** claimable early; a duplicate `idempotency_key` is
      rejected. Skip cleanly when no database is reachable.
    - _Requirements: 14.2, 14.4, 14.6, 14.7_

- [x] 3. Channels and the simulator
  - [x] 3.1 Write `core/src/aisales/channels.py`: `Inbound`, `Outbound`, the `Channel` Protocol,
      and `normalise_phone()` raising `ValueError` on anything not a valid Nigerian number.
    - _Requirements: 1.2, 1.5, 1.6_
  - [x] 3.2 Implement `SimulatorChannel` with a signature-free `verify`, `parse` for the local
      shape, and `send` returning a minted id.
    - _Requirements: 1.2, 15.1_
  - [x] 3.3 Write `core/tests/test_channels.py` with the phone table from `design.md` (all
      spellings → `+2348031234567`; malformed and foreign numbers raise), and assert that a
      delivery receipt parses to zero messages.
    - _Requirements: 1.7, 1.3_
  - [x] 3.4 Write `api/src/aisales_api/app.py` with `create_app(dsn)`, `install()` in the lifespan,
      and a `CHANNELS` registry with `POST /webhooks/{name}` returning 404 for an unknown channel.
    - _Requirements: 1.1, 1.2_
  - [x] 3.5 Write `api/src/aisales_api/sim.py`: one HTML page at `GET /sim/{slug}` with a message
      box and a thread view, plus `POST /sim/messages` accepting `wait` to run inline.
    - _Requirements: 15.1, 15.2_
  - [x] 3.6 Implement `record_inbound()` — upsert customer, find or create the open conversation,
      cancel any pending follow-up, insert the message, enqueue a turn. Dedupe on
      `(business_id, provider_message_id)`.
    - _Requirements: 1.4, 7.6_
  - [x] 3.7 Demo: open `/sim/adafabrics`, send a message with an echo agent, confirm one row in
      `psql`; resend the same `external_id` and confirm still one row.
    - _Requirements: 1.4_

- [x] 4. Providers
  - [x] 4.1 Write `core/src/aisales/providers.py` porting `ProviderError`, `is_transient`,
      `is_daily_quota`, `retry_after`, `summarise` and the HTTP classification from
      `pycoding/engine/src/pycoding/providers.py`. Add a `reason="unsupported"` branch for a
      permanent "model does not support tools" response.
    - _Requirements: 3.9_
  - [x] 4.2 Define `ToolCall`, `Turn` and the `Chat` Protocol with
      `chat(messages, tools) -> Turn`.
    - _Requirements: 3.2_
  - [x] 4.3 Implement `LiveChat` over `urllib` against an OpenAI-compatible `/chat/completions`
      endpoint, sending `tools` and parsing `tool_calls`.
    - _Requirements: 3.2_
  - [x] 4.4 Implement `ChainChat` (Gemini → Groq → HuggingFace → OpenRouter → Cerebras) honouring
      `TOOL_MODELS` so a non-tool model may serve `classify_intent` but never an agent turn.
    - _Requirements: 3.2, 3.9_
  - [x] 4.5 Implement `ScriptedChat` replaying a list of `Turn`s **including tool calls**, so turn
      two sees turn one's result and the guard's allowed set populates exactly as it would live.
    - _Requirements: 15.6_
  - [x] 4.6 Implement `default_chat()` reading the environment chain, switching to `ScriptedChat`
      when `AISALES_FAKES=1`.
    - _Requirements: 15.5_

- [x] 5. Tools and the agent turn
  - [x] 5.1 Define `ToolResult` (with `money_kobo`) and the tool JSON schemas for all ten tools.
    - _Requirements: 3.2_
  - [x] 5.2 Implement the catalogue tools: `search_products` (ILIKE over `search_text`),
      `get_product`.
    - _Requirements: 2.5, 2.6_
  - [x] 5.3 Implement `create_order` — order plus items in one transaction, copying name and unit
      price, computing the total, minting a per-business unique reference.
    - _Requirements: 9.1, 9.2, 9.3, 9.4_
  - [x] 5.4 Implement `tag_lead` (upserting the single open lead, raising but never lowering the
      tier), `remember_customer`, and `schedule_follow_up`.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_
  - [x] 5.5 Implement `no_reply` and `escalate_to_human`.
    - _Requirements: 3.8, 10.2, 10.3_
  - [x] 5.6 Validate every tool call against its schema before dispatch; return validation errors
      to the model rather than raising.
    - _Requirements: 3.3_
  - [x] 5.7 Implement `state_block()` — open order, reference and total, lead tier, pending
      follow-up, customer notes, and the fenced Approved_Facts — loaded from SQL on every turn.
    - _Requirements: 3.6, 2.3_
  - [x] 5.8 Implement `run_turn()` per the `design.md` sequence, with the ownership check first,
      the bounded tool loop, and `send()` before the outbound row is written.
    - _Requirements: 3.1, 3.4, 3.5, 3.10_
  - [x] 5.9 Implement rolling summarisation into `conversations.summary`, triggered lazily from
      inside the turn when the thread exceeds the window.
    - _Requirements: 3.7_
  - [x] 5.10 Implement `audit()` and call it for every consequential action, with the closed action
      set from `design.md`.
    - _Requirements: 12.1, 12.2, 12.3, 12.5, 12.6_
  - [x] 5.11 Demo: `ScriptedChat` drives a real tool call against real SQL and the seeded price
      comes back. Zero keys.
    - _Requirements: 15.6_

- [x] 6. The guard
  - [x] 6.1 Implement `guard.py` with the four currency patterns (`₦`, `naira`, `k` suffix, bare
      4+ digits) and the escapes for URLs, order references, clock times and quantity/size markers.
    - _Requirements: 4.1, 4.9_
  - [x] 6.2 Implement rule `money` against the per-turn allowed set.
    - _Requirements: 4.2, 4.3_
  - [x] 6.3 Implement rule `authority` — discount, reduction, refund, waiver, free delivery, final
      price.
    - _Requirements: 4.4_
  - [x] 6.4 Implement rule `availability` — a positive availability claim requires a tool result
      this turn with `stock_qty > 0`.
    - _Requirements: 4.5_
  - [x] 6.5 Implement `escalation_decision()` as one function with the ordered rules.
    - _Requirements: 10.2, 10.3_
  - [x] 6.6 Implement the block path: re-prompt once with tool results restated, then escalate;
      write nothing to `messages`; audit the block with the allowed set and offending tokens; send
      the fixed `ESCALATION_LINE` constant.
    - _Requirements: 4.6, 4.7, 4.8, 3.9_
  - [x] 6.7 Write `core/tests/test_guard.py` with **both** a must-block table and a must-allow
      table of real replies, including "sizes 40, 42, 44" and a reply containing only grounded
      amounts.
    - _Requirements: 4.2, 4.3, 4.10_
  - [x] 6.8 Add the `ponytail:` comment naming the false-positive ceiling and the
      placeholder-substitution upgrade path.
    - _Requirements: 4.10_

- [x] 7. Nigerian commerce intelligence
  - [x] 7.1 Write `prompts.py`: the system prompt, the fenced Approved_Facts block, and the
      `NIGERIAN_COMMERCE` section with the intent set and the per-pattern reasoning from
      `design.md`.
    - _Requirements: 5.1–5.6_
  - [x] 7.2 Implement `classify_intent()` — one small `temperature=0` call over the closed intent
      set, degrading to `unknown`/0.0 on any parse failure.
    - _Requirements: 5.7_
  - [x] 7.3 Record the classified intent on the message row.
    - _Requirements: 5.8_
  - [x] 7.4 Write `core/tests/test_nigeria.py` with the golden set of 28 utterances, asserting
      intents hard and the eight slot-carrying utterances softly, and printing a per-intent
      confusion table on failure.
    - _Requirements: 5.2, 5.3, 5.4, 5.5, 5.6, 5.9_
  - [x] 7.5 Write `scripts/record_intent_fixture.py` to run the set live and write
      `core/tests/fixtures/intent.json`, so the offline test replays a real recording.
    - _Requirements: 5.9, 15.6_

- [x] 8. Paystack
  - [x] 8.1 Write `paystack.py`: the `PaystackClient` Protocol, `verify_webhook()` (HMAC-SHA512 over
      the raw body), and `FakePaystack`.
    - _Requirements: 8.1, 8.3_
  - [x] 8.2 Implement `LivePaystack` — initialise transaction, return `authorization_url`.
    - _Requirements: 8.1, 8.2_
  - [x] 8.3 Implement `create_payment_link` and `get_payment_status` tools; payment status is read
      from stored records only.
    - _Requirements: 8.10_
  - [x] 8.4 Implement `POST /webhooks/paystack` with the three corrections: event type checked
      first; a single atomic `insert ... on conflict do nothing returning id` for idempotency; and
      the callback amount verified against the order total.
    - _Requirements: 8.3, 8.4, 8.5, 8.6, 8.7_
  - [x] 8.5 Transition the order to `paid` and audit `payment_confirmed` on success; store the
      provider payload.
    - _Requirements: 8.8, 8.9_
  - [x] 8.6 Implement `POST /dev/paystack/charge` signing with the real algorithm and secret and
      posting at our own webhook, so the verification path is exercised rather than bypassed.
    - _Requirements: 15.7_
  - [x] 8.7 Write `core/tests/test_money.py` asserting integer kobo throughout and that no float
      enters a payment amount.
    - _Requirements: 8.2_
  - [x] 8.8 Test all three corrections: duplicate reference, mismatched amount, unknown event.
    - _Requirements: 8.5, 8.6, 8.7_

- [x] 9. Follow-up and takeover
  - [x] 9.1 Implement `FollowUpPolicy.from_settings()` reading timing, frequency and tone.
    - _Requirements: 7.2_
  - [x] 9.2 Implement `schedule()` — attempt cap, `due_at` defaulting to 120 minutes, `min_gap`
      clamp, quiet-hours shift in `ZoneInfo("Africa/Lagos")`, Service_Window skip with
      `window_closed`, and enqueue with `not_before = due_at`.
    - _Requirements: 7.1, 7.3, 7.4, 7.5, 7.8, 7.11_
  - [x] 9.3 Implement `dispatch()` — re-read all state from SQL, take
      `pg_try_advisory_xact_lock` and requeue if a turn is live, skip on human ownership, cancel on
      customer reply, generate one short nudge through the same prompts **and the same guard**.
    - _Requirements: 7.9, 7.10_
  - [x] 9.4 Write `core/tests/test_followup.py` for the Service_Window and quiet-hours boundaries.
    - _Requirements: 7.4, 7.5_
  - [x] 9.5 Implement `POST /conversations/{id}/takeover`, `/handback` and `/messages`, with staff
      messages recorded as `role='staff'` and included in the model's history.
    - _Requirements: 10.4, 10.7, 10.8_
  - [x] 9.6 Implement handback enqueueing exactly one turn when a customer message arrived during
      the takeover, and the `takeover_suppressed_turn` audit entry while owned by a human.
    - _Requirements: 10.5, 10.6, 10.9, 10.10_
  - [x] 9.7 Implement the all-providers-failed path: leave the job queued, escalate, send the fixed
      acknowledgement.
    - _Requirements: 3.9_

- [x] 10. Insights
  - [x] 10.1 Implement `GET /insights/daily` — collected, conversations, orders, new leads, plus the
      comparison period named explicitly.
    - _Requirements: 11.1, 11.6_
  - [x] 10.2 Implement most-asked intents and un-converted leads grouped by reason.
    - _Requirements: 11.2, 11.3_
  - [x] 10.3 Implement the `guard_blocked` count as a first-class figure.
    - _Requirements: 11.4_
  - [x]* 10.4 Add the `ponytail:` comment naming the ceiling (it cannot notice a pattern nobody
      thought to count) and the summarisation upgrade path.
    - _Requirements: 11.5_

- [x] 11. Dashboard
  - [x] 11.1 Scaffold `web/` — Next.js 16 App Router, React 19, Tailwind 4, shadcn/ui, pnpm.
      Add `pnpm-workspace.yaml`.
    - _Requirements: —_
  - [x] 11.2 Build `/` Inbox: two panes, attention rows sorted first in the single accent colour,
      takeover and handback controls, composer disabled while the AI owns the thread, polling with
      stale-while-revalidate.
    - _Requirements: 10.1, 10.4_
  - [x] 11.3 Build `/leads`, `/orders` and `/insights` as server components.
    - _Requirements: 6.1, 9.5, 11.1_
  - [x] 11.4 Apply the UI rules: one accent colour used for exactly three things, no gradients,
      hairline borders instead of shadows, radius varying with size, `tabular-nums` on every number,
      skeleton on first load only, a boundary per pane.
    - _Requirements: —_

- [x] 12. End-to-end verification
  - [x] 12.1 Write `scripts/e2e.py` with the eleven steps from `design.md`, `assert`-based, exiting
      non-zero and naming the failing step.
    - _Requirements: 15.9_
  - [x] 12.2 Add the `assert_not_mixed()` interlock refusing a live send while any dependency is a
      fake.
    - _Requirements: 15.8_
  - [x] 12.3 Confirm the whole of `scripts/e2e.py` passes with no API keys present.
    - _Requirements: 15.5_
  - [x] 12.4 Add `docker-compose.yml` (postgres + api + worker) and `.env.example` listing every
      key with none required for the offline path.
    - _Requirements: —_

- [x] 13. Meta adapter
  - [x] 13.1 Implement `MetaCloudChannel` — `verify` with HMAC-**SHA256** over the raw body,
      `parse` over `entry[].changes[].value.messages[]`, `send` to the Graph API, and the two-hop
      `download`.
    - _Requirements: 1.1, 1.2_
  - [x] 13.2 Add the `GET /webhooks/whatsapp` verification handshake.
    - _Requirements: 1.9_
  - [x] 13.3 Test that the SHA-256 and SHA-512 verifiers each reject the other's signatures.
    - _Requirements: 1.1_
  - [x] 13.4 Confirm the swap requires **zero** changes to `agent.py`, `tools.py`, `guard.py` or
      `prompts.py` — the acceptance test for the abstraction.
    - _Requirements: 1.2_
  - [x]* 13.5 Record per-message channel cost on outbound messages once Meta's rate card is
      confirmed.
    - _Requirements: 13.5_

- [x] 14. Deployment and compliance (deferred, not V1)
  - [x]* 14.1 Add `db.purge_conversations_before(date)` so retention is a choice rather than a
      default.
    - _Requirements: 13.4_
  - [x]* 14.2 Write down the lawful basis and add a privacy notice the agent can send on request.
    - _Requirements: 13.4_
  - [x]* 14.3 Add adversarial prompt-injection tests before live traffic.
    - _Requirements: 4.1_
