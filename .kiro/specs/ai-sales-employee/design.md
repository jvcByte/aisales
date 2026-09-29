# Design Document

## Overview

The AI Sales Employee is three Python distributions and one Next.js app over a single PostgreSQL
database:

- **`aisales`** (`core/`) — all logic, no server. The agent turn, tools, guard, channels, provider
  chain, queue, follow-up policy, Paystack client. Everything here is exercisable offline.
- **`aisales-api`** (`api/`) — FastAPI. Channel webhooks, the dashboard API, and the developer
  simulator.
- **`aisales-worker`** (`worker/`) — the process that claims jobs and runs turns.
- **`web/`** — the business owner's dashboard.

The design is a deliberate application of one idea: **the correctness-critical rules live in
mechanism, not in prompt text.** A language model is asked to sell; it is not trusted to decide what
may be said about money. Requirement 4 is therefore implemented as a guard that compares every
currency token in a drafted reply against the set of amounts the database actually produced during
that turn. The prompt is written as though the guard did not exist, and the guard is written as
though the model were adversarial.

### Key design decisions

| Decision | Rationale |
|---|---|
| Postgres-backed queue; no Redis | `redis-server` is absent from the target environment, and the existing local convention is a `FOR UPDATE SKIP LOCKED` + lease queue. A second datastore buys nothing at this volume. |
| Integer kobo everywhere | Paystack transmits kobo. Storing or computing in Naira introduces float rounding into payment amounts. No divide-by-100 exists anywhere in the payment path. |
| `jobs.not_before` as the follow-up timer | A scheduled follow-up is simply a job that is not claimable yet. No cron, no sleeping worker, no second scheduler to keep consistent. |
| The guard compares against *this turn's* amounts only | A price from six turns ago is stale. Requiring a fresh lookup on every quote is exactly the "never invent prices" promise, enforced. |
| `needs_attention` and `status` as separate columns | "The AI asked for help" and "a person is now answering" are different facts. One column would make the inbox unable to distinguish a flagged-but-handled thread from an unanswered one. |
| Turn coalescing by unique index, not by lock | A customer sending three messages in four seconds must produce one reply. A partial unique index on live `agent_turn` jobs achieves this without holding a lock across a multi-second network call. |
| Channel, provider and payment behind `Protocol`s | Requirement 15. Without WhatsApp credentials the entire product must still be buildable and testable. |
| Money is authorised by SQL, never by prose | The rolling summary is text and therefore cannot authorise a figure. This falls out of the design rather than needing its own rule. |

---

## Architecture

```
        WhatsApp (later)                     Simulator (now)
                │                                   │
                └───────────────┬───────────────────┘
                                ▼
                    POST /webhooks/{channel}   POST /sim/messages
                                │                    │
                    verify signature                 │
                    parse → Inbound[]                │
                    dedupe on message id             │
                                │                    │
                                ▼                    ▼
                         ┌──────────────────────────────┐
                         │  record_inbound()            │
                         │   • upsert customer          │
                         │   • find/create conversation │
                         │   • cancel pending follow-up │
                         │   • insert message           │
                         │   • enqueue agent_turn       │──┐
                         └──────────────────────────────┘  │
                                                           │
   ┌───────────────────────────────────────────────────────┘
   │
   │   PostgreSQL                          ┌──────────────┐
   │   ┌──────────────┐   claim (SKIP LOCKED│   worker     │
   │   │    jobs      │◀───────+ lease)─────│              │
   │   └──────────────┘                     │  run_turn()  │
   │                                        └──────┬───────┘
   │                                               │
   │                       ┌───────────────────────▼────────────────────┐
   │                       │  1 owner == 'human' ? → record, send nothing│
   │                       │  2 load config, customer, window, summary   │
   │                       │  3 state_block() ← FROM SQL, every turn      │
   │                       │  4 classify_intent()                         │
   │                       │  5 model ⇄ tools (≤4 rounds)                 │
   │                       │  6 guard.check(reply, allowed_set)           │
   │                       │  7 escalation_decision()                     │
   │                       │  8 send → THEN record → audit                │
   │                       └───────────────────────┬────────────────────┘
   │                                               │
   └───────────────► dashboard API ◀───────────────┘
                        │
                        ▼
                    Next.js web/
```

**Why the webhook responds before the model runs.** Meta retries a webhook it believes failed, and
repeated slow responses degrade the sending number's quality rating. The gateway therefore returns
success immediately after the message is durably stored and a job is enqueued. All model work
happens in the worker.

---

## Components and Interfaces

### `providers.py` — the model, behind a Protocol

Ported from the existing local convention. `Writer.write(prompt) -> str` becomes
`Chat.chat(messages, tools) -> Turn`; everything about retry classification, quota handling and
offline scriptability is retained.

```python
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict

@dataclass(frozen=True)
class Turn:
    text: str                        # '' when the model only called tools
    calls: tuple[ToolCall, ...]      # empty when the model only spoke
    model: str
    finish_reason: str               # recorded so an ignored `tools` param is visible
    raw: dict

class Chat(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> Turn: ...
```

Three implementations:

- `ChainChat` — tries each configured provider in order. `is_transient`/`is_daily_quota`/
  `retry_after` from the existing convention are reused unchanged. Gains a `reason="unsupported"`
  branch: a permanent "this model does not support tools" response skips that model for agent turns
  rather than retrying it four times.
- `ScriptedChat` — replays a list of `Turn`s, including tool calls. This is what makes the offline
  path the real path: turn two sees turn one's tool result, so the guard's allowed set is populated
  exactly as it would be live.
- `LiveChat` — raw HTTP against an OpenAI-compatible `/chat/completions` endpoint, via `urllib`.

**Model capability flag.** `TOOL_MODELS: dict[str, bool]` records which chain members can be trusted
with tools. A model that cannot is still permitted to serve `classify_intent`, which needs none. An
agent turn may only be dispatched to a tool-capable model.

### `channels.py` — the messaging boundary

```python
@dataclass(frozen=True)
class Inbound:
    channel: str
    external_id: str
    from_phone: str
    kind: Literal["text", "image", "voice", "document", "location", "other"]
    text: str
    media_id: str | None = None
    mime_type: str | None = None
    received_at: datetime | None = None
    raw: dict = field(default_factory=dict)

@dataclass(frozen=True)
class Outbound:
    to_phone: str
    text: str
    kind: Literal["text"] = "text"

class Channel(Protocol):
    name: str
    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool: ...
    def parse(self, body: bytes, headers: Mapping[str, str]) -> list[Inbound]: ...
    def send(self, message: Outbound) -> str: ...
    def download(self, media_id: str) -> tuple[bytes, str]: ...
```

Responsibilities are split deliberately: `verify` and `download` are the riskiest and most
platform-specific parts (signature algorithms, two-hop media fetches), so they stay inside the
adapter where a test can pin them. `parse` returns zero messages for a delivery receipt, because a
delivery receipt carries no message and must not create one.

`normalise_phone(raw) -> str` raises `ValueError` rather than assuming a country code. Messaging a
stranger because of a guessed prefix is worse than dropping a malformed number.

**The signature trap.** Meta signs with HMAC-SHA256 over the raw body; Paystack signs with
HMAC-SHA512. The two adapters look similar and are not interchangeable; a copy-paste between them
produces a verifier that either always fails or always passes. Both directions are tested.

### `guard.py` — the safety property

```python
@dataclass(frozen=True)
class Verdict:
    ok: bool
    rule: str | None      # 'money' | 'authority' | 'availability'
    detail: dict          # offending tokens, and the allowed set, for the audit row

def check(reply: str, allowed_kobo: Iterable[int], *, ctx: TurnContext) -> Verdict: ...
```

The allowed set is built per turn from two sources only: `ToolResult.money_kobo` for each tool
dispatched this turn, and the currency figures `state_block()` loaded from SQL this turn. Three
rules run in order, first failure wins (see Requirement 4 for the normative behaviour).

Extraction must handle all four ways Nigerians write prices — `₦12,500`, `12,500 naira`, `12.5k`,
and a bare `12500`. A guard that recognises only the Naira symbol recognises almost nothing in
practice.

```
ponytail: rules 1 and 3 are threshold heuristics over regex, not a parser. Ceiling: a reply
listing "sizes 40, 42, 44" can false-positive rule 1 and escalate a correct reply. Upgrade path:
have the model emit ₦{price:SKU} placeholders that the renderer substitutes from tool results —
making the wrong thing unrepresentable rather than merely detected — and keep this guard as the
backstop. Do that if guard_blocked on /insights is anything but near zero.
```

### `agent.py` — one turn

```python
def run_turn(conn, channel: Channel, chat: Chat, inbound: Inbound) -> TurnResult: ...
def classify_intent(chat: Chat, text: str) -> Intent: ...
def escalation_decision(reply, intent, verdict, ctx) -> tuple[str, str] | None: ...
def state_block(conn, ctx) -> str: ...
```

`escalation_decision` is one function rather than checks scattered across tools, so the escalation
policy can be read and tested as a single thing. Order: guard blocked → handover; intent in the
business's escalate-on set → handover; the model requested escalation → its stated urgency;
confidence below threshold *and* an asserted price → flag; unknown intent twice running → flag;
else send.

`ESCALATION_LINE` is a module constant, not model output. A model asked to apologise for a blocked
reply can produce another unsafe reply, which would defeat the block.

### `tools.py` — ten tools

`search_products`, `get_product`, `create_order`, `create_payment_link`, `get_payment_status`,
`tag_lead`, `remember_customer`, `schedule_follow_up`, `escalate_to_human`, `no_reply`.

```python
@dataclass(frozen=True)
class ToolResult:
    call: ToolCall
    ok: bool
    data: dict
    money_kobo: tuple[int, ...] = ()   # the amounts this result authorises
```

Every tool takes a `TurnContext` carrying `business_id`; no query is written without
`business_id = %s`. `no_reply` exists because without it a model asked to be helpful will invent
something to say after "thank you", which is what makes these agents the thing people mute.

Business policies are deliberately **not** a tool. They are fixed context in the system prompt,
loaded by SQL each turn, and their figures join the allowed set — so the choice costs nothing in
safety and saves a model round-trip on every turn.

### `followup.py`

```python
@dataclass(frozen=True)
class FollowUpPolicy:
    first_after_minutes: int = 120      # inside the Service_Window, deliberately
    min_gap_minutes: int = 1440
    max_attempts: int = 2
    quiet_start: str = "21:00"
    quiet_end: str = "08:00"
    tone: str = "warm, brief, Nigerian English"

    @classmethod
    def from_settings(cls, settings: dict) -> "FollowUpPolicy": ...
```

`from_settings` is the single place business-controlled timing, frequency and tone are read, which
is where Requirement 7.2 is satisfied. `dispatch` re-reads all state from SQL — never from the job
payload, which may be hours old — and takes `pg_try_advisory_xact_lock` on the conversation: if a
turn is in flight, requeue rather than wait, because a follow-up can always go later.

### `paystack.py`

```python
class PaystackClient(Protocol):
    def initialise(self, *, email: str, amount_kobo: int, reference: str,
                   metadata: dict) -> str: ...          # returns authorization_url

def verify_webhook(secret: str, raw_body: bytes, signature: str | None) -> bool: ...
```

`FakePaystack` mints a reference and returns a synthetic URL. The dev charge endpoint signs with the
**real** secret and posts at our own webhook, so the verification path is exercised rather than
bypassed.

---

## Data Models

Executed as one idempotent `SCHEMA` string by `db.install()`. Every statement is
`create ... if not exists`, and every column added after the first install is also issued as
`alter table ... add column if not exists`, because a column present only in a `create table` body
does nothing to a database that already exists.

```sql
-- ── extensions ────────────────────────────────────────────────────────────
create extension if not exists pgcrypto;   -- gen_random_uuid()

-- ── the tenant ────────────────────────────────────────────────────────────
create table if not exists businesses (
  id         uuid primary key default gen_random_uuid(),
  slug       text not null unique,
  name       text not null,
  -- Every knob the AI obeys, in one blob: tone, followup{...}, escalate_on[],
  -- confidence_threshold, approved_facts, paystack_subaccount. A column per
  -- knob is twenty migrations before the pilot ends.
  settings   jsonb not null default '{}',
  created_at timestamptz not null default now()
);

-- ── customers ─────────────────────────────────────────────────────────────
create table if not exists customers (
  id            uuid primary key default gen_random_uuid(),
  business_id   uuid not null references businesses(id) on delete cascade,
  phone_e164    text not null,
  phone_raw     text,
  name          text,
  notes         jsonb not null default '{}',
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  unique (business_id, phone_e164)
);
create index if not exists customers_recent_idx
  on customers (business_id, last_seen_at desc);

-- ── conversations ─────────────────────────────────────────────────────────
do $$ begin
  create type conversation_channel as enum ('simulator','whatsapp');
  create type conversation_status  as enum ('ai','human','closed');
exception when duplicate_object then null; end $$;

create table if not exists conversations (
  id            uuid primary key default gen_random_uuid(),
  business_id   uuid not null references businesses(id) on delete cascade,
  customer_id   uuid not null references customers(id) on delete cascade,
  channel       conversation_channel not null default 'simulator',
  -- Ownership: who may reply. Distinct from needs_attention, below.
  status        conversation_status not null default 'ai',
  -- 'The AI asked for help'. Independent of who currently owns the thread.
  needs_attention  boolean not null default false,
  attention_reason text,
  assigned_to   text,
  takeover_at   timestamptz,
  summary       text,
  summarized_upto_message_id bigint,
  -- The newest customer message a Turn actually read when it composed its
  -- reply. Cannot be derived from ordering: a message arriving mid-Turn has a
  -- LOWER id than the reply that ignored it, so any ordering-based check calls
  -- it answered. The Turn has to record what it saw.
  answered_upto_message_id bigint,
  last_inbound_at timestamptz,     -- drives the Service_Window check
  last_message_at timestamptz not null default now(),
  created_at    timestamptz not null default now()
);
create index if not exists conversations_inbox_idx
  on conversations (business_id, last_message_at desc);
create index if not exists conversations_attention_idx
  on conversations (business_id, needs_attention, last_message_at desc)
  where status <> 'closed';
-- One live thread per customer per channel: a second inbound message must join
-- the thread the agent is already answering, not start a parallel one.
create unique index if not exists conversations_one_open_idx
  on conversations (business_id, customer_id, channel) where status <> 'closed';

-- ── messages ──────────────────────────────────────────────────────────────
do $$ begin
  create type message_role as enum ('customer','ai','staff','system');
exception when duplicate_object then null; end $$;

create table if not exists messages (
  id              bigserial primary key,
  business_id     uuid not null references businesses(id) on delete cascade,
  conversation_id uuid not null references conversations(id) on delete cascade,
  role            message_role not null,
  body            text not null,
  -- Channel plumbing and derived facts: provider message id, mime type, voice
  -- duration, and the classified intent at meta->>'intent'.
  meta            jsonb not null default '{}',
  transcribed     boolean not null default false,
  provider_message_id text,
  -- Requirement 13.5: per-message channel cost, so cost per conversation and
  -- per recovered sale is measurable.
  cost_kobo       bigint not null default 0,
  created_at      timestamptz not null default now(),
  -- Provider redelivery and a customer double-tap both land here. NULLs are
  -- distinct in a Postgres unique index, so our own outbound rows do not collide.
  unique (business_id, provider_message_id)
);
create index if not exists messages_thread_idx on messages (conversation_id, id);
create index if not exists messages_intent_idx
  on messages (business_id, (meta->>'intent')) where role = 'customer';

-- ── products ──────────────────────────────────────────────────────────────
create table if not exists products (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  sku         text not null,
  name        text not null,
  price_kobo  bigint not null check (price_kobo > 0),
  -- NULL means "not tracked"; 0 means "none left". A business that does not
  -- count stock must not have the AI refuse to sell.
  stock_qty   integer check (stock_qty >= 0),
  variants    jsonb not null default '[]',
  description text not null default '',
  active      boolean not null default true,
  search_text text not null default '',
  created_at  timestamptz not null default now(),
  unique (business_id, sku)
);
create index if not exists products_active_idx on products (business_id) where active;

-- ── leads ─────────────────────────────────────────────────────────────────
do $$ begin
  create type lead_tier   as enum ('hot','warm','cold');
  create type lead_status as enum ('open','won','lost','dropped');
exception when duplicate_object then null; end $$;

create table if not exists leads (
  id              uuid primary key default gen_random_uuid(),
  business_id     uuid not null references businesses(id) on delete cascade,
  customer_id     uuid not null references customers(id) on delete cascade,
  conversation_id uuid references conversations(id) on delete set null,
  tier            lead_tier not null,
  -- The model's own words for why. This text is what /insights groups on, so
  -- it is product data, not a debug field.
  reason          text not null default '',
  intent          text,
  product_id      uuid references products(id) on delete set null,
  confidence      real check (confidence between 0 and 1),
  status          lead_status not null default 'open',
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create unique index if not exists leads_one_open_idx
  on leads (business_id, customer_id) where status = 'open';
create index if not exists leads_board_idx
  on leads (business_id, tier, created_at desc) where status = 'open';

-- ── orders ────────────────────────────────────────────────────────────────
do $$ begin
  create type order_status as enum
    ('draft','awaiting_payment','paid','fulfilled','cancelled');
exception when duplicate_object then null; end $$;

create table if not exists orders (
  id              uuid primary key default gen_random_uuid(),
  business_id     uuid not null references businesses(id) on delete cascade,
  customer_id     uuid not null references customers(id) on delete restrict,
  conversation_id uuid references conversations(id) on delete set null,
  reference       text not null,
  status          order_status not null default 'draft',
  total_kobo      bigint not null default 0 check (total_kobo >= 0),
  delivery_address text,
  delivery_fee_kobo bigint not null default 0,
  -- Two orders sharing one reference would let a webhook credit the wrong one.
  payment_reference text unique,
  paid_at         timestamptz,
  notes           text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (business_id, reference)
);
create index if not exists orders_board_idx
  on orders (business_id, status, created_at desc);

create table if not exists order_items (
  id          bigserial primary key,
  business_id uuid not null references businesses(id) on delete cascade,
  order_id    uuid not null references orders(id) on delete cascade,
  product_id  uuid references products(id) on delete set null,
  -- Name and price are copied, never joined: a price change next month must not
  -- rewrite what this customer was quoted today.
  name        text not null,
  unit_price_kobo bigint not null check (unit_price_kobo > 0),
  qty         integer not null check (qty > 0),
  line_total_kobo bigint generated always as (unit_price_kobo * qty) stored
);
create index if not exists order_items_order_idx on order_items (order_id);

-- ── payments ──────────────────────────────────────────────────────────────
do $$ begin
  create type payment_status as enum ('pending','success','failed','abandoned');
exception when duplicate_object then null; end $$;

create table if not exists payments (
  id                 uuid primary key default gen_random_uuid(),
  business_id        uuid not null references businesses(id) on delete cascade,
  order_id           uuid references orders(id) on delete set null,
  customer_id        uuid references customers(id) on delete set null,
  -- Globally unique, not per-business: a webhook arrives with no business_id to
  -- scope by, and Paystack's references are globally unique anyway. THIS is the
  -- payment idempotency constraint.
  paystack_reference text not null unique,
  amount_kobo        bigint not null check (amount_kobo > 0),
  status             payment_status not null default 'pending',
  channel            text,
  -- The whole verified webhook body: six weeks later the audit trail must answer
  -- "what did Paystack actually say", after their dashboard view has aged out.
  raw                jsonb,
  verified_at        timestamptz,
  created_at         timestamptz not null default now()
);
create index if not exists payments_order_idx on payments (order_id);
create index if not exists payments_recent_idx on payments (business_id, created_at desc);

-- ── follow-ups ────────────────────────────────────────────────────────────
do $$ begin
  create type follow_up_status as enum ('scheduled','sent','cancelled','skipped');
exception when duplicate_object then null; end $$;

create table if not exists follow_ups (
  id              uuid primary key default gen_random_uuid(),
  business_id     uuid not null references businesses(id) on delete cascade,
  customer_id     uuid not null references customers(id) on delete cascade,
  conversation_id uuid not null references conversations(id) on delete cascade,
  reason          text not null,
  due_at          timestamptz not null,
  status          follow_up_status not null default 'scheduled',
  attempt         integer not null default 1,
  skipped_reason  text,
  job_id          uuid,
  created_at      timestamptz not null default now()
);
-- One open follow-up per conversation, so a chatty model cannot schedule four.
create unique index if not exists follow_ups_one_open_idx
  on follow_ups (conversation_id) where status = 'scheduled';
create index if not exists follow_ups_due_idx
  on follow_ups (due_at) where status = 'scheduled';

-- ── audit log ─────────────────────────────────────────────────────────────
create table if not exists audit_log (
  id              bigserial primary key,
  business_id     uuid not null references businesses(id) on delete cascade,
  conversation_id uuid references conversations(id) on delete set null,
  actor           text not null,
  -- A closed set, because /insights counts these and free text counts nothing:
  -- price_quoted | order_created | payment_link_sent | payment_confirmed
  -- | lead_tagged | follow_up_sent | escalated | takeover | handback
  -- | guard_blocked | takeover_suppressed_turn
  action          text not null,
  detail          jsonb not null default '{}',
  model           text,
  created_at      timestamptz not null default now()
);
create index if not exists audit_recent_idx on audit_log (business_id, created_at desc);
create index if not exists audit_thread_idx on audit_log (conversation_id, id);

-- ── the job queue ─────────────────────────────────────────────────────────
do $$ begin
  create type job_status as enum
    ('queued','leased','running','succeeded','failed','canceled');
exception when duplicate_object then null; end $$;

create table if not exists jobs (
  id               uuid primary key default gen_random_uuid(),
  business_id      uuid not null references businesses(id) on delete cascade,
  kind             text not null,     -- 'agent_turn' | 'follow_up'
  conversation_id  uuid references conversations(id) on delete cascade,
  payload          jsonb not null default '{}',
  status           job_status not null default 'queued',
  lease_owner      text,
  lease_expires_at timestamptz,
  heartbeat_at     timestamptz,
  attempt          integer not null default 0,
  max_attempts     integer not null default 4,
  -- Earliest a job may run. A follow-up due in two hours is queued now and is
  -- simply not claimable until then. This column IS the scheduler.
  not_before       timestamptz not null default now(),
  error_code       text,
  error_message    text,
  idempotency_key  text unique,
  created_at       timestamptz not null default now(),
  started_at       timestamptz,
  finished_at      timestamptz
);
create index if not exists jobs_claim_idx on jobs (not_before, created_at)
  where status = 'queued';
create index if not exists jobs_lease_idx on jobs (lease_expires_at)
  where status in ('leased','running');
create index if not exists jobs_business_idx on jobs (business_id, created_at desc);
-- At most one live agent turn per conversation, enforced by the database. A
-- customer sending three messages in four seconds gets ONE reply. This is what
-- makes the burst case correct without a lock held across a network call.
create unique index if not exists jobs_one_turn_idx
  on jobs (conversation_id)
  where kind = 'agent_turn' and status in ('queued','leased','running');
```

### Business settings shape

`businesses.settings` is a JSON blob read through typed accessors. Documented keys:

```jsonc
{
  "tone": "warm, brief, Nigerian English",
  "confidence_threshold": 0.6,
  "escalate_on": ["refund_request", "complaint", "discount_request",
                  "payment_dispute", "delivery_dispute"],
  "followup": {
    "first_after_minutes": 120,     // inside the Service_Window, deliberately
    "min_gap_minutes": 1440,
    "max_attempts": 2,
    "quiet_start": "21:00",
    "quiet_end": "08:00",
    "tone": "warm, brief"
  },
  "approved_facts": "Delivery within Lagos is ₦2,500 and takes 1-2 days...",
  "paystack_subaccount": null
}
```

Figures inside `approved_facts` are parsed into the allowed set on every turn, so the fenced facts
block cannot be used to smuggle an unauthorised price.

---

## Workspace Integration

```
AI_SALES_EMPLOYEE/
├── pyproject.toml          # root: [tool.pytest.ini_options] + [tool.ruff] ONLY. No package here.
├── core/pyproject.toml     # name = "aisales"        deps: psycopg[binary]>=3.1
├── api/pyproject.toml      # name = "aisales-api"    deps: fastapi, uvicorn[standard], aisales
├── worker/pyproject.toml   # name = "aisales-worker" deps: aisales
└── web/                    # pnpm workspace member
```

Installed with `pip install -e core/ -e api/ -e worker/`, matching the existing convention. No
`uv`, no `poetry`, no Docker requirement for development. Redis is deliberately absent.

Every external dependency is constructed behind a factory (`default_chat()`, `default_paystack()`),
so `AISALES_FAKES=1` swaps the whole system to offline mode and each entry point accepts
`--offline`.

---

## Correctness Properties

The properties that must hold, and where each is checked.

| # | Property | Requirement | Check |
|---|---|---|---|
| P1 | An amount not produced by SQL this turn never reaches a customer | 4.2, 4.3 | `test_guard.py` must-block table |
| P2 | An amount produced by SQL this turn is never wrongly blocked | 4.10 | `test_guard.py` must-allow table |
| P3 | A discount claim with no authorising tool result is blocked | 4.4 | `test_guard.py` |
| P4 | A blocked reply never enters the message history | 4.6 | `test_guard.py` + e2e step 3 |
| P5 | One provider reference produces exactly one payment row | 8.4, 8.7 | e2e steps 7–8 |
| P6 | A payment whose amount differs from the order is never accepted | 8.5 | e2e step 9 |
| P7 | Every Nigerian phone spelling normalises to one E.164 value | 1.7 | `test_channels.py` table |
| P8 | A redelivered provider message is stored exactly once | 1.4 | e2e + `test_channels.py` |
| P9 | A job whose `not_before` is future is never claimed early | 14.6 | `test_queue.py` |
| P10 | No two workers hold the same job | 14.2 | `test_queue.py` (concurrent claims) |
| P11 | At most one live agent turn exists per conversation | 14.8 | `test_queue.py` |
| P12 | A customer burst produces exactly one reply | 3.1, 14.8 | e2e step |
| P13 | A follow-up inside the Service_Window is sent; one outside is skipped | 7.4 | `test_followup.py` |
| P14 | Any inbound message cancels a pending follow-up | 7.6 | e2e step 10 |
| P15 | Money round-trips through storage with no precision loss | 8.2 | `test_money.py` (integer invariant) |
| P16 | The agent is silent while ownership is `human` | 10.3, 10.5 | e2e |
| P17 | On handback, a message received during takeover is answered | 10.9 | e2e |
| P18 | Every reported insight figure derives from stored rows | 11.5 | e2e step 11 |
| P19 | Swapping the channel requires no change outside `channels.py` | 15 | P9 phase diff review |
| P20 | The guard blocks nothing when every figure is grounded | 4.10 | `test_guard.py` |
| P21 | A message arriving *while* a turn is running is still eventually answered | 3.1, 14.8 | `test_intake.py` |

**P21 is the least obvious property in the list, and the one most likely to be broken by a
reasonable-looking refactor.** Coalescing means a message that arrives mid-turn is refused a
second turn, because it should fold into the running one. But the running turn has already read
the thread, so it will never see it: the message is stored and never answered, and the
conversation looks handled. The guard against this cannot compare message ids to the newest reply,
because the mid-turn message has a *lower* id than the reply that ignored it. The turn therefore
records `answered_upto_message_id` itself, and the worker re-queues when anything newer exists.

---

## Error Handling

| Failure | Behaviour |
|---|---|
| Model provider transient (429/503) | Retry with the API's own `retryDelay` hint, then back off; then next model in the chain |
| Model provider daily quota exhausted | Skip to the next model immediately — the quota is per model per day, so waiting is pointless |
| Model does not support tools | Mark unsupported for this model, skip to the next; never retry a permanent 400 |
| **All providers exhausted** | Leave the reply job queued for retry, escalate to a human, send the fixed `ESCALATION_LINE`. The customer is never left in silence. |
| Guard blocks a reply | Re-prompt once; if blocked again, escalate. Record the block, the tokens and the allowed set. Write nothing to `messages`. |
| Tool call invalid against its schema | Do not dispatch; return the validation error to the model and let it correct within the iteration bound |
| Model loops on tools | After the iteration bound, escalate with reason `tool_loop` |
| Channel send fails | Job fails and retries; no outbound message row is written, so the transcript never claims an undelivered message |
| Webhook signature invalid | Reject with 401; store nothing |
| Webhook redelivered | Acknowledge 200, process nothing |
| Paystack callback amount mismatch | Reject, do not mark paid, escalate to a human |
| Paystack callback duplicate | Acknowledge 200, process nothing |
| Follow-up due while a turn is live | Requeue the follow-up; never send concurrently |
| Follow-up due outside the Service_Window | Skip, record `window_closed` |
| Follow-up due during quiet hours | Requeue to the end of quiet hours in `Africa/Lagos` |
| Database unreachable | Fail the job; the lease expires and another worker retries |
| Worker dies mid-turn | Lease expires; the job is reaped and re-run. Idempotency keys prevent duplicate sends. |

### Containment in the dashboard

Per the standing rule that an error must unmount the smallest region rather than the page:

- The inbox has a boundary **per pane**, so a failed thread does not take out the list.
- `/insights` has a boundary per section, so a failed digest does not hide the orders table.
- `web/app/error.tsx` is reserved for a genuinely dead API — the one case where nothing can render.
- Async fetch failures become local state rendered in place, never thrown to the root.

---

## Testing Strategy

Four layers, and the first three need neither credentials nor network.

**1. Unit, offline (`core/tests/`)**

| File | Covers |
|---|---|
| `test_guard.py` | P1–P4, P20 — a must-block table **and** a must-allow table, so the guard cannot pass by refusing everything |
| `test_channels.py` | P7, P8 — the phone normalisation table, parse/verify round-trips, and that SHA-256 and SHA-512 verifiers reject each other's signatures |
| `test_money.py` | P15 — kobo integer invariants, and that no float enters a payment amount |
| `test_queue.py` | P9–P11 — concurrent claims, lease expiry and reaping, `not_before` honoured. Requires a database; skipped without one. |
| `test_nigeria.py` | Requirement 5.9 — the golden set of 28 utterances |
| `test_followup.py` | P13 — Service_Window and quiet-hours boundaries, including the DST-adjacent cases |

**2. The golden set.** `scripts/record_intent_fixture.py` runs the set once against a live model and
writes `core/tests/fixtures/intent.json`. The offline test replays that recording, so the fixture is
a recording of the real model rather than a hand-written stub — which is what keeps the offline test
honest. Re-record after every prompt change; a drop in accuracy is a prompt regression. Accuracy is
printed as a per-intent confusion table so a regression names the intent that broke.

Intents are asserted hard; the eight utterances carrying slots are asserted softly, because slot
extraction from "Give me two" is genuinely harder and should not gate the build.

**3. End-to-end (`scripts/e2e.py`)**

One file, no framework, `assert`-based, exits non-zero naming the failing step:

| Step | Assertion |
|---|---|
| 1 | Seed business, catalogue and settings |
| 2 | "Abeg how much for the lace?" → the reply contains the seeded price; audit has `price_quoted` |
| 3 | "Reduce am" → no discount offered, no ungrounded amount, escalated, `needs_attention` set |
| 4 | "You get XL?" → the answer matches `products.variants` |
| 5 | "Send account" → order and payment created; the reply carries a payment URL |
| 6 | "I don pay" → payment status was queried; the reply does **not** confirm payment |
| 7 | Signed charge → payment `success`; order `paid` |
| 8 | Same reference again → idempotent; `paid_at` unchanged; exactly one payment row |
| 9 | Wrong amount → rejected; order **not** paid |
| 10 | Follow-up 1 minute out → sent exactly once; a reply beforehand cancels it |
| 11 | `/insights/daily` → collected equals `orders.total_kobo` |

PostgreSQL is the only prerequisite.

**4. The simulator** is the manual layer: opening `/sim/{slug}` and holding a conversation, with the
x-ray panel showing intent, tool calls, the guard's verdict and the allowed set. It calls the same
`run_turn` the worker calls, so a working simulator is evidence about production behaviour rather
than about a test harness.

### What this strategy does not cover

- **Model quality.** The golden set tests intent classification, not whether the agent sells well.
  Judging that requires the pilot business's real conversations, which is why the set is seeded from
  28 utterances and is expected to be replaced by that business's own history.
- **Prompt injection by customers.** Adversarial tests are required before live traffic; the guard
  covers the highest-consequence outcome, and tools are authority-scoped, but this is not yet
  systematically tested.
- **Load.** No test establishes behaviour under concurrent conversations at volume.
