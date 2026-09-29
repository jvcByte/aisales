"""The schema, the job queue, and the connection.

One file, because the queue is the schema's most load-bearing table and
splitting them would put the DDL and the queries that depend on its exact
shape in different places.

Deliberately not Celery, and deliberately not Redis. `redis-server` is not
installed on the target machine and a second datastore buys nothing at this
volume. `FOR UPDATE SKIP LOCKED` makes the claim transactional with the job
state, so there is no broker message to lose and no row/message dual-write to
reconcile. A job that spends money must reach a terminal state exactly once.

`jobs.not_before` does double duty here: it is both a retry's backoff and the
follow-up scheduler's timer. A follow-up due in two hours is queued now and is
simply not claimable until then, which is the whole of the scheduling
mechanism -- no cron, no sleeping worker, no second scheduler to keep
consistent with the first.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

# Statuses that mean "a worker is on it". The lease on these can expire, and
# the claim query below reclaims them once it has.
ACTIVE_STATUSES = ("leased", "running")

LEASE_SECONDS = 120
# Beyond the lease, so a worker merely between heartbeats is not declared dead
# and reaped out from under itself.
REAP_GRACE_SECONDS = 300

# pgcrypto is still needed, for `pgp_sym_encrypt` in business_secrets. It is
# not needed for uuid generation: `gen_random_uuid()` is core in PostgreSQL
# 13+, so no pgcrypto extension is
# needed -- which also means no superuser is needed to install this.
#
# Every column added after the first install must be repeated as
# `alter table ... add column if not exists`: a column present only in a
# `create table` body does nothing to a database that already exists, and the
# failure is silent until a query names it.
SCHEMA = """
create table if not exists businesses (
  id         uuid primary key default gen_random_uuid(),
  slug       text not null unique,
  name       text not null,
  -- PUBLIC BY DESIGN. Everything here may be returned to the business's own
  -- members in full, so it must never hold a credential -- see
  -- business_secrets. The settings endpoint returns this blob verbatim, and
  -- test_secrets.py asserts no secret type ever appears in it.
  settings   jsonb not null default '{}',
  created_at timestamptz not null default now(),
  -- Null means running. A timestamp rather than a boolean because "when" is
  -- the thing you want afterwards, and "null is the normal state" reads
  -- correctly at every call site.
  suspended_at     timestamptz,
  suspended_reason text
);

-- ── people ────────────────────────────────────────────────────────────────
create table if not exists users (
  id            uuid primary key default gen_random_uuid(),
  email         text not null unique,
  -- A memory-hard hash (scrypt), never a password. The column is named for
  -- what it holds so nobody stores the other thing in it.
  password_hash text not null,
  name          text,
  created_at    timestamptz not null default now(),
  disabled_at   timestamptz,
  -- Runs the deployment rather than a shop: sees across every business. Not a
  -- business role -- `business_members.role` is owner|staff, and this is
  -- orthogonal to it. A user may be both.
  is_operator   boolean not null default false
);

create table if not exists sessions (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references users(id) on delete cascade,
  -- Stored hashed, so a leaked session table is not a set of usable tokens.
  token_hash  text not null unique,
  issued_at   timestamptz not null default now(),
  expires_at  timestamptz not null,
  revoked_at  timestamptz
);
create index if not exists sessions_user_idx on sessions (user_id) where revoked_at is null;

create table if not exists business_members (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  user_id     uuid not null references users(id) on delete cascade,
  role        text not null default 'owner' check (role in ('owner', 'staff')),
  created_at  timestamptz not null default now(),
  unique (business_id, user_id)
);
create index if not exists members_user_idx on business_members (user_id);

-- Written and read by the operator role alone, and deliberately not scoped by
-- tenant: scoping it would hide the one row that matters, the one naming a
-- tenant the reader is not part of.
create table if not exists operator_audit (
  id          bigserial primary key,
  actor       text not null,
  -- The action name ("business.created", "business.suspended"), not a
  -- sentence: this is a table to group by, not prose to read.
  note        text not null,
  business_id uuid references businesses(id) on delete set null,
  detail      jsonb not null default '{}',
  at          timestamptz not null default now()
);
alter table operator_audit add column if not exists detail jsonb not null default '{}';

-- Encryption for business_secrets. Separate from gen_random_uuid(), which is
-- core in PostgreSQL 13+ and needs no extension at all.
create extension if not exists pgcrypto;

-- ── integrations: configuration, never credentials ────────────────────────
do $$ begin
  create type integration_status as enum ('active', 'revoked', 'error');
exception when duplicate_object then null; end $$;

create table if not exists business_integrations (
  id                  uuid primary key default gen_random_uuid(),
  business_id         uuid not null references businesses(id) on delete cascade,
  provider            text not null check (provider in ('whatsapp', 'paystack')),
  -- The provider's own identifier: phone_number_id, or a Paystack account
  -- code. Non-secret by definition -- it travels in webhooks and URLs.
  external_account_id text not null,
  status              integration_status not null default 'active',
  metadata            jsonb not null default '{}',
  created_at          timestamptz not null default now(),
  -- THIS INDEX is what makes "two businesses claim one number" impossible
  -- rather than merely unlikely. The ambiguity cannot arise, so it never has
  -- to be detected.
  unique (provider, external_account_id)
);
create index if not exists integrations_business_idx on business_integrations (business_id);

-- ── secrets: separate store, separate lifetime ────────────────────────────
create table if not exists business_secrets (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  provider    text not null,
  secret_type text not null,
  ciphertext  bytea not null,
  key_version integer not null default 1,
  rotated_at  timestamptz,
  created_at  timestamptz not null default now(),
  unique (business_id, provider, secret_type)
);

-- ── rate limiting for sign-in ─────────────────────────────────────────────
create table if not exists auth_attempts (
  id          bigserial primary key,
  bucket      text not null,          -- "account:<email>" | "ip:<addr>"
  succeeded   boolean not null default false,
  created_at  timestamptz not null default now()
);
create index if not exists auth_attempts_idx on auth_attempts (bucket, created_at desc);

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

do $$ begin
  create type conversation_channel as enum ('simulator','whatsapp');
  create type conversation_status  as enum ('ai','human','closed');
exception when duplicate_object then null; end $$;

create table if not exists conversations (
  id            uuid primary key default gen_random_uuid(),
  business_id   uuid not null references businesses(id) on delete cascade,
  customer_id   uuid not null references customers(id) on delete cascade,
  channel       conversation_channel not null default 'simulator',
  -- Ownership: who may reply. Distinct from needs_attention below, because
  -- "the AI asked for help" and "a person is now answering" are different
  -- facts, and collapsing them leaves the inbox unable to show what is
  -- actually outstanding.
  status        conversation_status not null default 'ai',
  needs_attention  boolean not null default false,
  attention_reason text,
  assigned_to   text,
  takeover_at   timestamptz,
  summary       text,
  summarized_upto_message_id bigint,
  -- The newest customer message a turn actually read when it composed its
  -- reply. Cannot be derived from ordering: a message arriving mid-turn has a
  -- LOWER id than the reply, so comparing against the last reply's id makes an
  -- unseen message look answered. The turn has to say what it saw.
  answered_upto_message_id bigint,
  last_inbound_at timestamptz,
  last_message_at timestamptz not null default now(),
  created_at    timestamptz not null default now()
);
alter table conversations add column if not exists answered_upto_message_id bigint;
alter table businesses add column if not exists suspended_at timestamptz;
alter table businesses add column if not exists suspended_reason text;
alter table users add column if not exists is_operator boolean not null default false;

create index if not exists conversations_inbox_idx
  on conversations (business_id, last_message_at desc);
create index if not exists conversations_attention_idx
  on conversations (business_id, needs_attention, last_message_at desc)
  where status <> 'closed';
-- One live thread per customer per channel: a second inbound message must join
-- the thread the agent is already answering, not start a parallel one that the
-- guard and the summariser both lose track of.
create unique index if not exists conversations_one_open_idx
  on conversations (business_id, customer_id, channel) where status <> 'closed';

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
  -- Per-message channel cost, so cost per conversation is measurable.
  cost_kobo       bigint not null default 0,
  created_at      timestamptz not null default now(),
  -- Provider redelivery and a customer double-tap both land here. NULLs are
  -- distinct in a Postgres unique index, so our own outbound rows -- which
  -- have no provider id -- do not collide with each other.
  unique (business_id, provider_message_id)
);
create index if not exists messages_thread_idx on messages (conversation_id, id);
create index if not exists messages_intent_idx
  on messages (business_id, (meta->>'intent')) where role = 'customer';

create table if not exists products (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  sku         text not null,
  name        text not null,
  price_kobo  bigint not null check (price_kobo > 0),
  -- NULL means "not tracked"; 0 means "none left". A business that does not
  -- count its stock must not have the AI refuse to sell.
  stock_qty   integer check (stock_qty >= 0),
  variants    jsonb not null default '[]',
  description text not null default '',
  -- A photo of the item. Nullable and unseeded: the dashboard falls back to a
  -- woven-textile tile rather than pretending to have a photograph.
  image_url   text,
  active      boolean not null default true,
  search_text text not null default '',
  created_at  timestamptz not null default now(),
  unique (business_id, sku)
);
alter table products add column if not exists image_url text;

create index if not exists products_active_idx on products (business_id) where active;

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
  -- The model's own words for why. /insights groups on this text, so it is
  -- product data rather than a debug field.
  reason          text not null default '',
  intent          text,
  product_id      uuid references products(id) on delete set null,
  confidence      real check (confidence between 0 and 1),
  status          lead_status not null default 'open',
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
-- A customer has one open lead at a time; the tier rises as the chat warms
-- rather than piling up four rows for one person.
create unique index if not exists leads_one_open_idx
  on leads (business_id, customer_id) where status = 'open';
create index if not exists leads_board_idx
  on leads (business_id, tier, created_at desc) where status = 'open';

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
  -- Unique here as well as on payments: two orders sharing one reference would
  -- let a single webhook credit the wrong one.
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
  -- Name and price are copied, never joined: a price change next month must
  -- not rewrite what this customer was quoted today.
  name        text not null,
  unit_price_kobo bigint not null check (unit_price_kobo > 0),
  qty         integer not null check (qty > 0),
  line_total_kobo bigint generated always as (unit_price_kobo * qty) stored
);
create index if not exists order_items_order_idx on order_items (order_id);

do $$ begin
  create type payment_status as enum ('pending','success','failed','abandoned');
exception when duplicate_object then null; end $$;

create table if not exists payments (
  id                 uuid primary key default gen_random_uuid(),
  business_id        uuid not null references businesses(id) on delete cascade,
  order_id           uuid references orders(id) on delete set null,
  customer_id        uuid references customers(id) on delete set null,
  -- Globally unique rather than per-business: a webhook arrives with no
  -- business_id on it to scope by, and Paystack's references are globally
  -- unique anyway. THIS constraint is the payment idempotency guard.
  paystack_reference text not null unique,
  amount_kobo        bigint not null check (amount_kobo > 0),
  status             payment_status not null default 'pending',
  channel            text,
  -- Kept so a second request for the link returns the same one. Re-initialising
  -- a reference Paystack has already seen is an error, and a customer who asks
  -- twice must not get two different payment pages.
  authorization_url  text,
  -- The whole verified webhook body: six weeks later the audit trail has to
  -- answer "what did Paystack actually say", after their dashboard view of the
  -- transaction has aged out.
  raw                jsonb,
  verified_at        timestamptz,
  created_at         timestamptz not null default now()
);
alter table payments add column if not exists authorization_url text;

create index if not exists payments_order_idx on payments (order_id);
create index if not exists payments_recent_idx on payments (business_id, created_at desc);

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

-- Append-only. `action` is a closed set because /insights counts these, and
-- free text counts nothing:
--   price_quoted | order_created | payment_link_sent | payment_confirmed
--   lead_tagged  | follow_up_sent | escalated | takeover | handback
--   guard_blocked | takeover_suppressed_turn
create table if not exists audit_log (
  id              bigserial primary key,
  business_id     uuid not null references businesses(id) on delete cascade,
  conversation_id uuid references conversations(id) on delete set null,
  actor           text not null,
  action          text not null,
  detail          jsonb not null default '{}',
  model           text,
  created_at      timestamptz not null default now()
);
create index if not exists audit_recent_idx on audit_log (business_id, created_at desc);
create index if not exists audit_thread_idx on audit_log (conversation_id, id);

do $$ begin
  create type job_status as enum
    ('queued','leased','running','succeeded','failed','canceled');
exception when duplicate_object then null; end $$;

create table if not exists jobs (
  id               uuid primary key default gen_random_uuid(),
  business_id      uuid not null references businesses(id) on delete cascade,
  kind             text not null,
  conversation_id  uuid references conversations(id) on delete cascade,
  payload          jsonb not null default '{}',
  status           job_status not null default 'queued',
  lease_owner      text,
  lease_expires_at timestamptz,
  heartbeat_at     timestamptz,
  attempt          integer not null default 0,
  max_attempts     integer not null default 4,
  -- Earliest a job may run: a retry's backoff, and the follow-up timer.
  not_before       timestamptz not null default now(),
  error_code       text,
  error_message    text,
  idempotency_key  text unique,
  created_at       timestamptz not null default now(),
  started_at       timestamptz,
  finished_at      timestamptz
);
-- Indexed on not_before rather than created_at because not_before is what
-- gates claimability: a follow-up scheduled for tomorrow must not be scanned
-- past on every claim for a day.
create index if not exists jobs_claim_idx on jobs (not_before, created_at)
  where status = 'queued';
create index if not exists jobs_lease_idx on jobs (lease_expires_at)
  where status in ('leased','running');
create index if not exists jobs_business_idx on jobs (business_id, created_at desc);
-- At most one live agent turn per conversation, enforced by the database. A
-- customer sending three messages in four seconds gets ONE reply, and the
-- second and third messages are folded into the same turn. This is what makes
-- the burst case correct without a lock held across a network call.
create unique index if not exists jobs_one_turn_idx
  on jobs (conversation_id)
  where kind = 'agent_turn' and status in ('queued','leased','running');
"""


# --------------------------------------------------------------------- rows


@dataclass(frozen=True)
class Business:
    id: str
    slug: str
    name: str
    settings: dict[str, Any]
    suspended_at: datetime | None = None
    suspended_reason: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> "Business":
        return cls(id=str(row["id"]), slug=row["slug"], name=row["name"],
                   settings=row["settings"] or {},
                   suspended_at=row.get("suspended_at"),
                   suspended_reason=row.get("suspended_reason"))

    @property
    def suspended(self) -> bool:
        """The agent is paused. Customers are still recorded, never answered."""
        return self.suspended_at is not None

    # -- settings accessors. One place, so a missing key has one behaviour.

    @property
    def tone(self) -> str:
        return self.settings.get("tone", "warm, brief, Nigerian English")

    @property
    def confidence_threshold(self) -> float:
        return float(self.settings.get("confidence_threshold", 0.6))

    @property
    def escalate_on(self) -> frozenset[str]:
        """Intents that stop the AI and hand the thread to a person.

        Deliberately *not* including a discount request, which is the most
        common opening move in Nigerian commerce ("Reduce am"). Handing over
        permanently on every haggle would take the agent offline for most
        customers after one message. The guard already makes it impossible for
        the agent to grant a discount, so the correct response is to hold the
        price and tell the owner -- which is what `flag_on` is for.
        """
        return frozenset(self.settings.get("escalate_on", (
            "refund_request", "complaint", "payment_dispute", "delivery_dispute",
        )))

    @property
    def flag_on(self) -> frozenset[str]:
        """Intents worth the owner's attention that do not stop the agent."""
        return frozenset(self.settings.get("flag_on", (
            "discount_request", "price_objection",
        )))

    @property
    def approved_facts(self) -> str:
        return str(self.settings.get("approved_facts", ""))

    @property
    def privacy_notice(self) -> str:
        """What the agent may say about a customer's data.

        A property rather than a prompt constant because the lawful basis, the
        retention period and the recipients are the business's to state, not
        ours -- and because an agent that improvises a data-protection policy
        has invented a legal commitment on the owner's behalf.
        """
        return str(self.settings.get("privacy_notice", ""))


@dataclass(frozen=True)
class TurnContext:
    """Everything a tool needs. Carries business_id so no query is written
    without it -- see the tenancy note in install()."""

    business: Business
    customer_id: str
    conversation_id: str
    channel: str

    @property
    def business_id(self) -> str:
        return self.business.id


@dataclass(frozen=True)
class Job:
    id: str
    business_id: str
    kind: str
    conversation_id: str | None
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    not_before: datetime | None = None

    @classmethod
    def from_row(cls, row: dict) -> "Job":
        cid = row.get("conversation_id")
        return cls(
            id=str(row["id"]),
            business_id=str(row["business_id"]),
            kind=row["kind"],
            conversation_id=str(cid) if cid else None,
            payload=row["payload"] or {},
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            not_before=row.get("not_before"),
        )


# ---------------------------------------------------------------- connection


#: Every table that carries `business_id`, and therefore every table that gets
#: a tenant policy. `businesses` is deliberately absent -- it is the resolution
#: root, so scoping it by tenant is circular. That is safe only because it
#: holds configuration and never credentials.
SCOPED_TABLES = (
    "audit_log", "business_secrets", "conversations", "customers", "follow_ups",
    "jobs", "leads", "messages", "order_items", "orders", "payments", "products",
)

#: Deliberately NOT scoped: `business_integrations`. The WhatsApp resolver
#: reads it *before* a tenant exists, because it is the thing that determines
#: the tenant -- a policy here would make the resolver return nothing and every
#: inbound delivery would fail to route. It is safe only because it holds no
#: credentials: `external_account_id` is a phone_number_id or an account code,
#: both of which travel in webhooks and URLs. Anything secret about an
#: integration belongs in `business_secrets`, which *is* scoped.

#: The tenant expression, in one place.
#:
#: `current_setting(..., true)` returns NULL when the setting was never
#: defined, and `business_id = NULL` is NULL -- not true -- so the policy
#: matches nothing. Fail-closed is a property of this expression rather than a
#: rule anyone has to remember.
#:
#: `nullif(..., '')` is load-bearing and not defensive noise. Calling
#: `set_config` *defines* a custom parameter, so on a connection where a tenant
#: was ever set and the transaction has since ended, the setting reverts to the
#: empty string rather than to NULL -- and `''::uuid` raises. Without the
#: nullif, a reused connection gets "invalid input syntax for type uuid" from
#: an unrelated query instead of a clean empty result.
TENANT_PREDICATE = (
    "business_id = nullif(current_setting('aisales.business_id', true), '')::uuid"
)

QUEUE_ROLE = "aisales_queue"

#: Two functions, owned by a role exempt from RLS. They are the *only* way to
#: touch the queue without a tenant, and they return nothing but the job.
#:
#: A `security definer` function owned by the table owner would NOT work: under
#: `force row level security` the owner is subject to the policy too. The
#: bypass has to be an actual role holding BYPASSRLS.
QUEUE_FUNCTIONS = f"""
do $$ begin
  if not exists (select 1 from pg_roles where rolname = '{QUEUE_ROLE}') then
    execute 'create role {QUEUE_ROLE} bypassrls nologin';
  end if;
end $$;

-- BYPASSRLS exempts a role from *policies*; it does not grant table access.
-- A SECURITY DEFINER function runs as its owner, so without these the claims
-- fail with "permission denied for table jobs" rather than with anything that
-- mentions RLS.
grant usage on schema public to {QUEUE_ROLE};
grant select, update on jobs to {QUEUE_ROLE};

create or replace function claim_job(p_owner text) returns setof jobs
  language sql security definer set search_path = public as $$
    with candidate as (
      select id from jobs
       where (status = 'queued'
              or (status in ('leased','running') and lease_expires_at < now()))
         and not_before <= now()
       order by not_before, created_at
       for update skip locked
       limit 1
    )
    update jobs j
       set status           = 'leased',
           lease_owner      = p_owner,
           lease_expires_at = now() + make_interval(secs => {LEASE_SECONDS}),
           heartbeat_at     = now(),
           attempt          = j.attempt + 1,
           started_at       = coalesce(j.started_at, now())
      from candidate
     where j.id = candidate.id
    returning j.*;
$$;

create or replace function reap_jobs() returns setof uuid
  language sql security definer set search_path = public as $$
    update jobs
       set status = case when attempt < max_attempts
                         then 'queued'::job_status else 'failed'::job_status end,
           lease_owner = null,
           lease_expires_at = null,
           error_code = case when attempt < max_attempts
                             then null else 'lease_expired' end,
           finished_at = case when attempt < max_attempts then null else now() end
     where status in ('leased','running')
       and lease_expires_at < now() - make_interval(secs => {REAP_GRACE_SECONDS})
    returning id;
$$;
"""

#: The role the application connects as. It owns nothing and can do nothing
#: except what the tenant policy allows -- which is the point, because RLS does
#: not apply to a table's owner, and does not apply at all to a superuser.
APP_ROLE = "aisales_app"

#: Privileges are granted wholesale. That is safe here in a way it would not be
#: without RLS: the grants decide *which tables* the role may touch, the
#: policies decide *which rows*. Scoping the grants too would mean a table
#: added later silently breaks the app instead of being covered.
#:
#: `nologin`: how this role authenticates is a deployment decision (password,
#: peer, IAM), and a schema installer that invents a login role is a schema
#: installer that invents a credential.
APP_ROLE_GRANTS = """
do $$ begin
  if not exists (select 1 from pg_roles where rolname = '{role}') then
    execute 'create role {role} nologin';
  end if;
end $$;

grant usage on schema public to {role};
grant select, insert, update, delete on all tables in schema public to {role};
grant usage, select on all sequences in schema public to {role};

alter default privileges in schema public
  grant select, insert, update, delete on tables to {role};
alter default privileges in schema public
  grant usage, select on sequences to {role};
"""

#: Roles for which RLS is not enforced, and for which a policy is therefore a
#: comment. Checked rather than assumed: `harden()` doing nothing is
#: indistinguishable from `harden()` working until the second business exists.
UNSCOPED_ROLE_SQL = """
select rolsuper or rolbypassrls as unscoped
  from pg_roles where rolname = %s
"""


class UnscopedRoleError(RuntimeError):
    """RLS would be installed for a role it does not apply to."""


def harden(dsn: str, *, app_role: str | None = None) -> str:
    """Turn on tenant isolation. Safe to run repeatedly. Returns the app role.

    Deliberately NOT part of `install()`. The application has to set a tenant
    context on every query first -- enabling this against code that does not
    would make every read return nothing, which looks like data loss rather
    than a missing setting.

    `app_role` defaults to `APP_ROLE`, which is created if missing. Pass the
    role the application will actually connect as; it must not be a superuser
    and must not hold BYPASSRLS, or every policy below is decoration. That is
    checked rather than assumed, because it fails silently otherwise.
    """
    role = app_role or APP_ROLE
    with connect(dsn) as conn:
        conn.execute(APP_ROLE_GRANTS.format(role=role))
        _install_queue_functions(conn, role)

        row = conn.execute(UNSCOPED_ROLE_SQL, (role,)).fetchone()
        if row is None:
            raise UnscopedRoleError(f"no such role: {role}")
        if row["unscoped"]:
            # The failure this prevents: everything below succeeds, the app
            # runs, every query returns every tenant's rows, and nothing
            # anywhere says so.
            raise UnscopedRoleError(
                f"{role!r} is a superuser or holds BYPASSRLS, so row level "
                f"security does not apply to it and these policies would do "
                f"nothing. Connect the application as a role without either."
            )

        for table in SCOPED_TABLES:
            # `force` is not optional: without it the table *owner* bypasses the
            # policy, and a deployment where the app role created the schema is
            # a deployment where the owner is the app role.
            conn.execute(f"alter table {table} enable row level security")
            conn.execute(f"alter table {table} force row level security")
            # Idempotent without a catalogue check: drop then create.
            conn.execute(f"drop policy if exists tenant_isolation on {table}")
            conn.execute(
                f"create policy tenant_isolation on {table} "
                f"using ({TENANT_PREDICATE}) with check ({TENANT_PREDICATE})"
            )
    return role


def unharden(dsn: str) -> None:
    """Turn tenant isolation back off, for tests that need the raw table."""
    with connect(dsn) as conn:
        for table in SCOPED_TABLES:
            conn.execute(f"drop policy if exists tenant_isolation on {table}")
            # `no force` too: leaving it set while RLS is off is inert, but it
            # makes the catalogue say something untrue about the table.
            conn.execute(f"alter table {table} no force row level security")
            conn.execute(f"alter table {table} disable row level security")


@contextmanager
def tenant(dsn: str, business_id: str):
    """A connection whose whole transaction runs as one tenant.

    `set_config(..., is_local => true)` rather than `SET LOCAL`, because
    SET LOCAL cannot take a bound parameter and a tenant id interpolated into
    SQL text is a tenant id in a log.

    The setting dies with the transaction. That is the property that makes a
    pooled or reused connection safe -- a worker processing jobs for different
    businesses cannot carry one tenant's context into the next job.

    Never pass an empty string: `''::uuid` raises, and a raising policy looks
    like a database fault rather than a missing tenant.
    """
    if not business_id:
        raise ValueError("tenant() needs a real business_id, not an empty value")
    with psycopg.connect(dsn, autocommit=False, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute("select set_config('aisales.business_id', %s, true)",
                         (str(business_id),))
            yield conn


def connect(dsn: str) -> psycopg.Connection:
    """A connection with dict rows and no implicit transaction.

    autocommit because almost every call here is a single statement, and an
    accidental multi-statement transaction holding a lock on `jobs` would
    stall every other worker.
    """
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def install(dsn: str) -> None:
    """Create the types, tables and indexes. Safe to run repeatedly.

    ponytail: tenancy is enforced in application code, not by the database.
    Correct for one business, no control plane, one process. Ceiling: a
    forgotten `where business_id = ...` silently returns another tenant's rows.
    Upgrade path is small and should be taken before a second business exists:
        alter table X enable row level security;
        create policy tenant_isolation on X
          using (business_id = current_setting('aisales.business_id')::uuid);
    for each scoped table, plus a `set_config` call inside connect().
    """
    with connect(dsn) as conn:
        conn.execute(SCHEMA)
        # The queue functions belong to install(), not to harden(): they are
        # the claim and reap implementation, not an RLS feature, and the queue
        # has to work whether or not isolation has been switched on.
        role = conn.execute("select current_user as u").fetchone()["u"]
        _install_queue_functions(conn, role)


def _install_queue_functions(conn: psycopg.Connection, app_role: str) -> None:
    """The two boundary functions, and the role that makes them work.

    Creating the role needs CREATEROLE. If it fails, install fails loudly --
    a deployment without it cannot run the queue under RLS, and discovering
    that later is worse than discovering it now.
    """
    try:
        conn.execute(QUEUE_FUNCTIONS)
    except psycopg.errors.InsufficientPrivilege as exc:
        raise RuntimeError(
            f"could not create the {QUEUE_ROLE} role. The queue needs a role "
            f"holding BYPASSRLS, because claim and reap are cross-tenant by "
            f"nature. Grant CREATEROLE to this role, or create {QUEUE_ROLE} "
            f"manually first."
        ) from exc
    conn.execute(f"alter function claim_job(text) owner to {QUEUE_ROLE}")
    conn.execute(f"alter function reap_jobs() owner to {QUEUE_ROLE}")
    # Restricted to the application's own role. The claim function returns
    # whichever job it takes, so EXECUTE on it is cross-tenant read access by
    # construction -- it must not be granted to PUBLIC.
    conn.execute(f"grant execute on function claim_job(text) to {app_role}")
    conn.execute(f"grant execute on function reap_jobs() to {app_role}")


class Queue:
    """Submit, claim, heartbeat, finish, fail, reap."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def _connect(self) -> psycopg.Connection:
        return connect(self.dsn)

    # ---------------------------------------------------------------- submit

    def submit(self, kind: str, *, business_id: str,
               conversation_id: str | None = None,
               payload: dict | None = None,
               not_before: datetime | None = None,
               idempotency_key: str | None = None,
               max_attempts: int = 4,
               conn: psycopg.Connection | None = None) -> str | None:
        """Queue a job and return its id, or None if it was not queued.

        None means "this is already queued", which is a normal outcome rather
        than an error: a redelivered webhook, a customer's third message in
        four seconds, and a double-clicked button all land here. The caller
        treats None as "nothing to do".

        Two separate constraints can produce that, and both are deliberate:
        the idempotency key (same work submitted twice) and jobs_one_turn_idx
        (a second turn for a conversation already being answered).

        Pass `conn` when you already hold an open tenant transaction. Without
        it this opens a second connection, which cannot see your uncommitted
        rows -- so queueing a turn for a conversation created in the same
        transaction fails on `jobs_conversation_id_fkey`. Passing it also
        makes the message and its job commit together or not at all, which is
        the behaviour that was intended all along.
        """
        if conn is not None:
            return self._submit_on(conn, kind, business_id, conversation_id,
                                   payload, not_before, idempotency_key, max_attempts)
        with tenant(self.dsn, business_id) as owned:
            return self._submit_on(owned, kind, business_id, conversation_id,
                                   payload, not_before, idempotency_key, max_attempts)

    def _submit_on(self, conn: psycopg.Connection, kind: str, business_id: str,
                   conversation_id: str | None, payload: dict | None,
                   not_before: datetime | None, idempotency_key: str | None,
                   max_attempts: int) -> str | None:
        try:
            # Nested, so this is a savepoint rather than a transaction of its
            # own. That matters for the refusal below: catching the violation
            # is not enough, the caller's transaction has to stay usable.
            with conn.transaction():
                row = conn.execute(
                    """
                    insert into jobs (kind, business_id, conversation_id, payload,
                                      not_before, idempotency_key, max_attempts)
                    values (%s, %s, %s, %s, coalesce(%s, now()), %s, %s)
                    on conflict (idempotency_key) do update
                       set kind = jobs.kind
                    returning id
                    """,
                    (kind, business_id, conversation_id,
                     psycopg.types.json.Json(payload or {}),
                     not_before, idempotency_key, max_attempts),
                ).fetchone()
                return str(row["id"]) if row else None
        except psycopg.errors.UniqueViolation:
            # jobs_one_turn_idx: a live turn already owns this conversation.
            return None

    # ----------------------------------------------------------------- claim

    def claim(self, owner: str) -> Job | None:
        """Take the oldest due job, or return None.

        This is one of the two boundary operations: it must see jobs belonging
        to any business, because a worker does not know whose job it is until
        it has taken one. It runs through a `SECURITY DEFINER` function owned
        by a role exempt from RLS, rather than by a flag this code sets --
        so the exception is a named function in the schema, not a boolean
        somebody can flip.

        Two workers running it concurrently cannot get the same row: the
        function's `for update skip locked` and its state change are one
        statement, so there is no window between them.
        """
        with self._connect() as conn:
            row = conn.execute("select * from claim_job(%s)", (owner,)).fetchone()
        return Job.from_row(row) if row else None

    # ------------------------------------------------------------- heartbeat

    def heartbeat(self, job: Job, owner: str, *, status: str | None = None) -> bool:
        """Extend the lease. Returns False if the job is no longer ours.

        The return value is the point. If this comes back False the lease
        expired and another worker may already be running the same job, so the
        caller must stop rather than finish and send a second message.
        """
        with tenant(self.dsn, job.business_id) as conn:
            row = conn.execute(
                """
                update jobs
                   set heartbeat_at     = now(),
                       lease_expires_at = now() + make_interval(secs => %s),
                       status           = coalesce(%s, status)::job_status
                 where id = %s and lease_owner = %s
                returning id
                """,
                (float(LEASE_SECONDS), status, job.id, owner),
            ).fetchone()
        return row is not None

    # -------------------------------------------------------------- terminal

    def finish(self, job: Job, owner: str) -> bool:
        with tenant(self.dsn, job.business_id) as conn:
            row = conn.execute(
                """
                update jobs
                   set status = 'succeeded', finished_at = now(),
                       lease_owner = null, lease_expires_at = null
                 where id = %s and lease_owner = %s
                returning id
                """,
                (job.id, owner),
            ).fetchone()
        return row is not None

    def fail(self, job: Job, owner: str, *, code: str, message: str,
             delay_seconds: float | None = None) -> bool:
        """Mark a job failed, or requeue it to be tried again later.

        *delay_seconds* is how long to wait before the retry becomes
        claimable, or None to stop here however many attempts remain -- which
        is for work that was declined rather than attempted, and would be
        declined identically on the next run.

        A quota-exhausted model is the reason this distinction exists: it is
        not an error to retry in five seconds, it is a wait until tomorrow.
        """
        retrying = delay_seconds is not None
        with tenant(self.dsn, job.business_id) as conn:
            row = conn.execute(
                """
                update jobs
                   set status = case when %(retrying)s and attempt < max_attempts
                                     then 'queued'::job_status
                                     else 'failed'::job_status end,
                       lease_owner = null,
                       lease_expires_at = null,
                       -- Kept on the requeue path too, so a job still being
                       -- retried can show what went wrong last time.
                       error_code = %(code)s,
                       error_message = %(message)s,
                       not_before = case when %(retrying)s and attempt < max_attempts
                                         then now() + make_interval(secs => %(delay)s)
                                         else not_before end,
                       finished_at = case when %(retrying)s and attempt < max_attempts
                                          then null else now() end
                 where id = %(job_id)s and lease_owner = %(owner)s
                returning status
                """,
                {"retrying": retrying, "delay": float(delay_seconds or 0.0),
                 "code": code, "message": message[:2000],
                 "job_id": job.id, "owner": owner},
            ).fetchone()
        return row is not None

    def reap(self) -> list[str]:
        """Recover jobs whose lease expired and whose worker never came back.

        The other boundary operation, and cross-tenant for the same reason:
        a sweep looks at every business's leases. Returns the ids touched, so
        a test can assert the transition rather than trust the SQL.
        """
        with self._connect() as conn:
            rows = conn.execute("select reap_jobs() as id").fetchall()
        return [str(r["id"]) for r in rows]

    # ----------------------------------------------------------------- query

    def get(self, job: Job) -> dict | None:
        with tenant(self.dsn, job.business_id) as conn:
            return conn.execute(
                "select * from jobs where id = %s", (job.id,)
            ).fetchone()

    def counts(self, business_id: str) -> dict[str, int]:
        """Job counts for one business.

        Takes a business rather than reporting a global total on purpose: a
        count across all tenants would be a cross-tenant read, and this is not
        one of the five boundaries. A health endpoint that wants a global
        figure needs a boundary function of its own.
        """
        with tenant(self.dsn, business_id) as conn:
            rows = conn.execute(
                "select status, count(*) as n from jobs group by status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}


def new_owner() -> str:
    """A stable-ish identity for this worker process."""
    import os
    import socket

    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def purge_conversations_before(dsn: str, cutoff: datetime) -> int:
    """Delete conversations last active before *cutoff*, with their messages.

    Shipped even though nothing schedules it, because retention is the one
    data-protection decision that is genuinely hard to retrofit: leaving
    "keep everything forever" as the default is a choice, and it should be a
    deliberate one. Cascades cover messages, follow-ups and jobs.
    """
    with connect(dsn) as conn:
        rows = conn.execute(
            "delete from conversations where last_message_at < %s returning id",
            (cutoff,),
        ).fetchall()
    return len(rows)


# ------------------------------------------------------------- boundaries


def resolve_whatsapp_tenant(conn: psycopg.Connection, phone_number_id: str) -> str | None:
    """Which business owns this WhatsApp number? One uuid, or None.

    Called *before* any tenant transaction opens, because it is the thing that
    decides which tenant that will be. It reads `business_integrations`, whose
    `unique (provider, external_account_id)` index is what makes the answer
    single-valued: an identifier claimed by two businesses cannot exist, so
    there is no ambiguity to resolve and no "first match" to get wrong.

    Returns only the id. A resolver that returned a Business would hand the
    caller a row read without a policy, which is how an unscoped read becomes
    an unscoped habit.
    """
    if not phone_number_id:
        return None
    row = conn.execute(
        """
        select business_id from business_integrations
         where provider = 'whatsapp' and external_account_id = %s
           and status <> 'revoked'
        """,
        (phone_number_id,),
    ).fetchone()
    return str(row["business_id"]) if row else None


def resolve_payment_tenant(conn: psycopg.Connection, reference: str) -> str | None:
    """Which business does this Paystack reference belong to?

    The same shape as the WhatsApp resolver and for the same reason: a callback
    arrives with no session, so the reference is the only thing naming a
    tenant. `payments.paystack_reference` is globally unique, so this cannot
    return two answers.
    """
    if not reference:
        return None
    row = conn.execute(
        "select business_id from payments where paystack_reference = %s", (reference,)
    ).fetchone()
    return str(row["business_id"]) if row else None


# --------------------------------------------------------------- the operator


#: The role an operator connects as when they need to see across businesses --
#: answering "which tenants are on this deployment", investigating an incident,
#: running a migration. It is never reachable from the business API.
OPERATOR_ROLE = "aisales_operator"

#: The environment variable naming its DSN. Deliberately not `AISALES_DSN`:
#: a deployment that set the ordinary one to a superuser would silently promote
#: every request to operator, and `harden()` refuses a role it cannot
#: constrain, so the mistake would at least be loud -- but not making the two
#: interchangeable is better than detecting the confusion.
OPERATOR_DSN_VAR = "AISALES_ADMIN_DSN"

OPERATOR_SQL = f"""
do $$ begin
  if not exists (select 1 from pg_roles where rolname = '{OPERATOR_ROLE}') then
    execute 'create role {OPERATOR_ROLE} bypassrls nologin';
  end if;
end $$;
"""


def install_operator(dsn: str) -> str:
    """Create the operator role. Returns its name.

    `nologin`, like the app role, and for the same reason: how a role
    authenticates is a deployment decision and a schema installer that invents
    a login role invents a credential. An operator also has to be a member of
    this role to `set role` into it, which is the actual access control --
    membership is granted by a person, to a person.
    """
    with connect(dsn) as conn:
        conn.execute(OPERATOR_SQL)
        conn.execute(f"grant usage on schema public to {OPERATOR_ROLE}")
        # Writes, not just reads: onboarding creates a business and its owner,
        # suspending updates the flag, and attaching an integration writes a
        # row. A read-only operator could not run the console it exists for.
        conn.execute(
            f"grant select, insert, update on all tables in schema public "
            f"to {OPERATOR_ROLE}"
        )
        # Sequences too: `operator_audit.id` is a bigserial, and an INSERT
        # into it without USAGE on the sequence fails with a permission error
        # that names the sequence rather than the audit it was writing.
        conn.execute(
            f"grant usage, select on all sequences in schema public to {OPERATOR_ROLE}"
        )
    return OPERATOR_ROLE


def operator_action(conn: psycopg.Connection, *, actor: str, action: str,
                    business_id: str | None = None,
                    detail: dict[str, Any] | None = None) -> None:
    """Record one cross-tenant action.

    `note` carries the action name rather than a sentence, so the trail is
    something a query can group by -- "every suspension this month" -- instead
    of prose somebody has to read. `detail` is the same jsonb shape
    `audit_log` uses, for anything that needs the specifics.

    Called on the operator connection, which is the only connection that can
    write a row naming a business the actor is not part of.
    """
    conn.execute(
        "insert into operator_audit (actor, note, business_id, detail) "
        "values (%s, %s, %s, %s)",
        (actor or "unspecified", action, business_id,
         psycopg.types.json.Json(detail or {})),
    )


@contextmanager
def operator(dsn: str | None = None, *, actor: str = ""):
    """A connection that sees every tenant, and records that it did.

    The audit is not decoration. A boundary whose use is invisible is a
    boundary nobody can review, and "who looked at this customer's messages"
    is a question a business is entitled to an answer to.
    """
    target = dsn or os.environ.get(OPERATOR_DSN_VAR)
    if not target:
        raise RuntimeError(
            f"{OPERATOR_DSN_VAR} is not set, so there is no operator connection. "
            f"That is the intended state for a running deployment: the business "
            f"API must not hold one. Set it explicitly when you mean to look "
            f"across tenants."
        )
    with psycopg.connect(target, autocommit=False, row_factory=dict_row) as conn:
        with conn.transaction():
            try:
                conn.execute(f"set local role {OPERATOR_ROLE}")
            except psycopg.errors.InsufficientPrivilege as exc:
                # The error PostgreSQL gives is "permission denied to set role
                # aisales_operator", which names the symptom and not the fix.
                # This is the first thing anyone following the setup hits when
                # the admin DSN authenticates as a role that is not a member.
                who = conn.execute("select session_user as u").fetchone()["u"]
                raise RuntimeError(
                    f"{OPERATOR_DSN_VAR} connects as {who!r}, which may not become "
                    f"{OPERATOR_ROLE!r}. Grant it: "
                    f"GRANT {OPERATOR_ROLE} TO {who};  -- or point the DSN at a "
                    f"superuser for local development."
                ) from exc
            conn.execute(
                "insert into operator_audit (actor, note) values (%s, %s)",
                (actor or "unspecified", "operator session opened"),
            )
            yield conn


