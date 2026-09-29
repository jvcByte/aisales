# Design Document

## Overview

Isolation is enforced by **PostgreSQL Row-Level Security**, driven by a transaction-scoped tenant setting. The application's job is to establish that setting correctly at exactly five places, and the database's job is to make every other access safe by default.

The pivot of the design is this expression:

```sql
business_id = current_setting('aisales.business_id', true)::uuid
```

`current_setting(..., true)` returns NULL when the setting is absent, and `business_id = NULL` is NULL — which is not true, so the policy matches nothing. **An unset tenant context reads and writes nothing.** Fail-closed is therefore a property of the expression rather than a rule someone has to remember, which is the only kind of guarantee worth having here.

### The five boundaries

```text
                    inbound
                       │
        ┌──────────────┼───────────────┬─────────────────┐
        │              │               │                 │
  authenticated   WhatsApp         Paystack          operator
     request      webhook          webhook            surface
        │              │               │                 │
   session →      phone_number_id  reference →      BYPASSRLS
   business_id      → business_id   business_id        role
        │              │               │                 │
        └──────────────┴───────┬───────┴─────────────────┘
                               │
                    BEGIN; SET LOCAL business_id; work; COMMIT
                               │
                        RLS-protected tables
                               │
                    ┌──────────┴──────────┐
                    │  queue scans        │   ← boundary 1, the other kind:
                    │  claim / reap       │     inherently cross-tenant,
                    └─────────────────────┘     not resolving anything
```

Four boundaries *resolve* a tenant; the fifth (`claim`/`reap`) operates without one because a worker must be able to pick up any business's job.

| # | Boundary | Mechanism | Blast radius if wrong |
|---|---|---|---|
| 1 | Queue scan | `SECURITY DEFINER` function owned by a `BYPASSRLS` role | Worker processes the wrong tenant's job |
| 2 | WhatsApp webhook | `resolve_whatsapp_tenant(phone_number_id)` | Messages land in the wrong shop |
| 3 | Paystack webhook | `resolve_payment_tenant(reference)` | Money credited to the wrong shop |
| 4 | Operator | Separate role, separate connection, audited | Every shop's conversations readable |
| 5 | `businesses` | Intentionally unscoped; holds config, never credentials | Low, *because* of Requirement 7 |

### Key decisions

| Decision | Rationale |
|---|---|
| `set_config(..., true)`, not `SET LOCAL` | `SET LOCAL` cannot take a bound parameter. `set_config(name, value, is_local => true)` is the same transaction-scoped behaviour with a parameterised value, so a tenant id never reaches the SQL text. |
| `force row level security` | Without it the table owner bypasses RLS, and the migration role — which is likely the app role in a small deployment — would see everything. |
| Boundary functions are `SECURITY DEFINER`, owned by a `BYPASSRLS` role | A `SECURITY DEFINER` function owned by the table owner is *still* subject to `force` RLS. The bypass must be an explicit role, not an assumption about ownership. |
| Resolution functions return only a `uuid` | A function that returns a row would leak that row without a tenant context. One `business_id` for one external identifier is the minimum disclosure that works. |
| Secrets via `pgcrypto` in the database | Adds no Python dependency, matching this codebase's convention. The honest cost is below. |
| `businesses` stays unscoped | It is the resolution root; scoping it by tenant is circular. Safe only while it holds no credentials. |

---

## Data Models

### Authentication and membership

```sql
create table if not exists users (
  id            uuid primary key default gen_random_uuid(),
  email         text not null unique,
  -- A memory-hard hash, never a password. The column name says so on purpose:
  -- "password" invites someone to store one.
  password_hash text not null,
  name          text,
  created_at    timestamptz not null default now(),
  disabled_at   timestamptz
);

create table if not exists sessions (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references users(id) on delete cascade,
  -- Stored hashed: a leaked session table must not be a set of usable tokens.
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
  role        text not null default 'owner'
              check (role in ('owner', 'staff')),
  created_at  timestamptz not null default now(),
  unique (business_id, user_id)
);
create index if not exists members_user_idx on business_members (user_id);
```

### Integrations — configuration, not credentials

```sql
do $$ begin
  create type integration_status as enum ('active', 'revoked', 'error');
exception when duplicate_object then null; end $$;

create table if not exists business_integrations (
  id                  uuid primary key default gen_random_uuid(),
  business_id         uuid not null references businesses(id) on delete cascade,
  provider            text not null check (provider in ('whatsapp', 'paystack')),
  -- The provider's own identifier: phone_number_id, or the Paystack
  -- subaccount/account code. Non-secret by definition -- it appears in
  -- webhooks and in URLs.
  external_account_id text not null,
  status              integration_status not null default 'active',
  metadata            jsonb not null default '{}',
  created_at          timestamptz not null default now(),
  -- One active integration per external identifier. This index IS the
  -- fail-closed guarantee for Requirement 5.4: a phone_number_id cannot
  -- belong to two businesses, so the ambiguity cannot arise.
  unique (provider, external_account_id)
);
create index if not exists integrations_business_idx on business_integrations (business_id);
```

### Secrets — separate store, separate lifetime

```sql
create extension if not exists pgcrypto;

create table if not exists business_secrets (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  provider    text not null,
  secret_type text not null,   -- 'secret_key' | 'access_token' | 'app_secret' | 'verify_token'
  ciphertext  bytea not null,
  key_version integer not null default 1,
  rotated_at  timestamptz,
  created_at  timestamptz not null default now(),
  unique (business_id, provider, secret_type)
);
```

Reads and writes go through `pgp_sym_encrypt` / `pgp_sym_decrypt`, with the key supplied as a **bound parameter** from the environment so it is never part of the statement text:

```sql
insert into business_secrets (business_id, provider, secret_type, ciphertext)
values (%s, %s, %s, pgp_sym_encrypt(%s, %s))
on conflict (business_id, provider, secret_type)
do update set ciphertext = excluded.ciphertext,
              key_version = key_version + 1,
              rotated_at = now();

select pgp_sym_decrypt(ciphertext, %s) from business_secrets
 where business_id = %s and provider = %s and secret_type = %s;
```

**The honest cost of choosing pgcrypto:** the key travels to the database on every read. Bound parameters keep it out of `pg_stat_statements`, but a server configured with `log_statement = 'all'` or `log_min_duration_statement = 0` would record it. Mitigation is configuration, not code, so the deployment note is: **those two settings must be off in production.** The alternative — encrypting in Python — means adding `cryptography` as a dependency for an operation this codebase already has a database for; that trade is worth revisiting if the key ever needs to be per-tenant.

**Key management.** `AISALES_SECRET_KEY` is injected at runtime and never committed, never stored in the database, and never in the same `.env` that the application reads its DSN from in a hosted deployment. Requirement 7.4 asks for rotation: `key_version` exists so that a re-encryption pass can be done a row at a time rather than as a flag day.

### RLS on every scoped table

Applied identically to `audit_log`, `conversations`, `customers`, `follow_ups`, `jobs`, `leads`, `messages`, `order_items`, `orders`, `payments`, `products`:

```sql
alter table customers enable row level security;
alter table customers force  row level security;

create policy tenant_isolation on customers
  using      (business_id = current_setting('aisales.business_id', true)::uuid)
  with check (business_id = current_setting('aisales.business_id', true)::uuid);
```

`using` filters reads and the rows an update may target; `with check` filters what may be written. Both are required — `using` alone would let a tenant *write* a row belonging to another.

### The queue boundary

`claim` and `reap` must see every business's jobs. That cannot be expressed as a tenant policy, so it is expressed as **two named functions owned by a role exempt from RLS**:

```sql
-- A role that exists for exactly two functions.
create role aisales_queue bypassrls;

create function claim_job(p_owner text) returns setof jobs
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
       set status = 'leased', lease_owner = p_owner,
           lease_expires_at = now() + interval '120 seconds',
           heartbeat_at = now(), attempt = j.attempt + 1,
           started_at = coalesce(j.started_at, now())
      from candidate where j.id = candidate.id
    returning j.*;
$$;
alter function claim_job(text) owner to aisales_queue;

create function reap_jobs() returns setof uuid
  language sql security definer set search_path = public as $$ ... $$;
alter function reap_jobs() owner to aisales_queue;

grant execute on function claim_job(text), reap_jobs() to aisales_app;
```

Everything else on `jobs` — `heartbeat`, `finish`, `fail`, `get` — runs under the ordinary tenant policy after the context is set. So the exposure is two functions, not a table.

---

## Components

### `connect()` and the tenant cursor

```python
@contextmanager
def tenant(dsn: str, business_id: str):
    """A connection whose whole transaction runs as one tenant.

    `set_config(..., is_local=true)` rather than `SET LOCAL`, because
    SET LOCAL cannot take a bound parameter and a tenant id interpolated
    into SQL text is a tenant id in a log.
    """
    with connect(dsn) as conn:            # autocommit OFF for this block
        with conn.transaction():
            conn.execute("select set_config('aisales.business_id', %s, true)",
                         (str(business_id),))
            yield conn
        # COMMIT: the setting is gone. This is the property that makes a
        # pooled or reused connection safe.
```

Note the interaction with the existing code: `connect()` currently sets `autocommit=True`, and several call sites rely on that (notably `install()`, and `_create_order`'s nested `with conn.transaction()`). The tenant context therefore needs an explicit transaction rather than relying on autocommit. **This is the single largest change to existing code** and the reason the worker test in Requirement 10.3 exists.

**Never set the context to an empty string.** `''::uuid` raises, and a raising policy looks like a database fault rather than a missing tenant. Set a real uuid, or leave it unset.

### Boundary implementations

```python
def resolve_whatsapp_tenant(conn, phone_number_id: str) -> str | None:
    """Boundary 2. Returns one uuid, or None. Never guesses."""

def resolve_payment_tenant(conn, reference: str) -> str | None:
    """Boundary 3. Returns one uuid, or None. Never guesses."""
```

Both are `select ... from <resolution function>`, both are called **before** any tenant transaction opens, and both return `None` rather than falling back:

```python
business_id = resolve_whatsapp_tenant(conn, phone_id)
if business_id is None:
    raise HTTPException(404, "unknown number")   # not _sole_business()
```

### Worker turn, end to end

```python
job = queue.claim(owner)                 # boundary 1: cross-tenant, own transaction
if job is None:
    return
with tenant(dsn, job.business_id):       # BEGIN; SET LOCAL; ...
    run_turn(...)                        # every query now RLS-scoped
# COMMIT — and the next job, possibly for another business, starts clean
```

### Operator surface

`AISALES_ADMIN_DSN` connects as a role holding `BYPASSRLS`. It is used only by the operator routes, is not imported by the business API, and every statement executed through it writes an `audit_log` row naming the operator and the tenant touched. Requirement 4.9 is the enforcement: the role must not be reachable from a path an ordinary request can take.

---

## Blast Radius on Existing Code

Named honestly, because two of these are bigger than they look.

| Area | Change |
|---|---|
| `db.connect()` | Gains the `tenant()` context manager; `autocommit` needs revisiting |
| **All 10 dashboard pages** | Every one calls `get("…?slug=${SLUG}")`. All become session-scoped. `web/lib/api.ts:3`'s hardcoded `SLUG` disappears. |
| `app.py` | `_sole_business()` deleted from three call sites (`:162`, `:262`, `:454`); `_business()` resolves from the session |
| `/sim/messages` | `payload.get("slug")` (`:153`) is a **client-chosen tenant today**. Becomes session-derived, and the simulator becomes admin-only. |
| `agent.record_inbound` | Unchanged — it already takes a resolved `Business` |
| `worker/runner.py` | The turn is wrapped in `tenant()` |
| `paystack.apply_callback` | Splits: resolve first (boundary 3), then act under `tenant()` |
| `views.settings()` | Unchanged in shape, but documented as public-by-design; secrets never enter the blob |

---

## Correctness Properties

| # | Property | Requirement | Check |
|---|---|---|---|
| T1 | A job for A, then B, then A on one connection: no context leaks | 10.1–10.3 | `test_tenant_isolation.py` |
| T2 | A query on any scoped table with no context returns zero rows | 3.4, 5.1, 5.5 | `test_rls.py` |
| T3 | A tenant cannot write a row belonging to another | 3.6 | `test_rls.py` (`with check`) |
| T4 | Authenticated as A, reading B's resource is indistinguishable from a 404 | 2.2, 10.4 | `test_cross_tenant_api.py` |
| T5 | An unknown `phone_number_id` rejects and does not fall through | 5.2, 5.7 | `test_boundaries.py` |
| T6 | An unknown payment reference is acknowledged and attributed to nobody | 5.3 | `test_boundaries.py` |
| T7 | `claim` and `reap` see every tenant; `finish` and `fail` see only their own | 4.2, 4.4 | `test_boundaries.py` |
| T8 | No documented secret type appears in `businesses.settings` | 7.2, 7.6 | `test_secrets.py` |
| T9 | The settings endpoint returns no field of `business_secrets` | 7.7 | `test_secrets.py` |
| T10 | A secret is readable only under its own tenant's context | 7.5 | `test_secrets.py` |
| T11 | A message is sent through the integration of its own business | 6.3 | `test_boundaries.py` |

---

## Error Handling

Every row here is fail-closed by design, and the second column is the point: the wrong answer is always the one that *looks like it worked*.

| Situation | Behaviour | The wrong answer this avoids |
|---|---|---|
| No session | 401, no resource disclosure | A 404 that confirms the resource exists |
| Session user is not a member | Same response as a non-existent resource | A 403 that confirms it exists |
| No tenant context on a query | Zero rows | Returning all rows |
| Tenant context set to `''` | Never happens — always a uuid or unset | `''::uuid` raising, read as a database fault |
| Unknown `phone_number_id` | Reject the delivery, log it | `_sole_business()` |
| Unknown payment reference | Acknowledge 200, attribute to nobody | Crediting the only business that exists |
| `phone_number_id` matching two businesses | Prevented by the unique index | An arbitrary pick |
| Integration revoked | Surface the failure, stop sending | Retry forever against a dead credential |
| Operator query | Audited with the operator and tenant | Invisible cross-tenant reads |
| Secret decryption fails | Fail the operation that needed it | Falling back to a platform default credential |
| RLS returns nothing unexpectedly | Investigate the context | Disabling RLS to "unblock" |

---

## Testing Strategy

The suite inverts the usual emphasis: the important tests are not that a feature works, but that it **does not work across tenants**.

**`test_rls.py`** — the database layer, no HTTP. Enables RLS, then asserts T2, T3, T10 directly with SQL. These are the tests that survive a refactor of the application, because they test the guarantee rather than the code that relies on it.

**`test_tenant_isolation.py`** — the worker test. Queue a job for A, one for B, one for A; run them in sequence on a single worker; assert each turn read and wrote only its own rows and that the context did not survive. This is the test that catches both a missing `set_config` and a context set for the connection's lifetime, and without it the failure is silent.

**`test_boundaries.py`** — one case per boundary, asserting the correct business is selected and that each unknown identifier fails closed.

**`test_cross_tenant_api.py`** — authenticated HTTP as A against B's identifiers, asserting the responses are indistinguishable from non-existence.

**`test_secrets.py`** — that no secret type appears in a settings blob, that the settings endpoint returns nothing from `business_secrets`, and that a secret is unreadable outside its tenant.

**What this does not cover.** Encryption strength against an attacker with database read access *and* the key; the operator role's own compromise; and any isolation property of the model providers, whose processing happens outside this system entirely. Those are deployment and legal questions, not test cases.
