# Architecture

Why this is built the way it is. Not a file listing — the decisions, and what
each one costs.

The short version: **the model is a dependency, not the system.** It sits behind
a protocol, its output is checked before it reaches anyone, and every fact it is
allowed to assert comes from SQL. Almost every decision below follows from
taking that seriously.

---

## The shape

```
   WhatsApp ─┐
             ├─▶ FastAPI ──▶ Postgres ◀── worker ──▶ model (Gemini → Groq → …)
  Simulator ─┘      │            ▲
                    │            └── the queue is a table
   Paystack ────────┘
                    │
                    ▼
              Next.js dashboard ──▶ same FastAPI
```

Four processes and a database. No message broker, no cache, no scheduler.

**The webhook returns 200 before any model runs.** Verify, parse, store,
enqueue, reply. Everything slow happens in the worker. A platform that waits too
long retries the delivery, and repeated retries degrade the sending number's
quality rating — a slow, hard-to-reverse failure that has nothing to do with the
quality of the reply.

---

## The decisions

### Postgres is the queue

A job is a row. `select … for update skip locked` is the claim, a lease column
is the recovery mechanism, and `not_before` is the follow-up timer.

The alternative — Redis, or Celery, or a cron — buys nothing here and costs a
second thing to run, back up and secure. The specific unlock is `not_before`
doing double duty: a follow-up due in two hours is a job that is not claimable
yet, so "follow up later" needs no scheduler at all.

Concurrency correctness comes free and is enforced where it cannot be bypassed:
a partial unique index allows at most one live `agent_turn` per conversation, so
a customer sending three messages in four seconds gets **one** reply — enforced
by the database rather than by a lock held across a ten-second model call.

### Facts live in SQL, not in the transcript

Every turn rebuilds a state block from Postgres: the open order and its
reference, the lead tier, scheduled follow-ups, the customer's notes, the
business's approved facts.

This is not an optimisation. It is what makes long conversations work at all:
something discussed sixty messages ago is still in the prompt because it is a
*row*, not a sentence that has since been trimmed out of the context window.

And it is what makes the guard sound. If the state block were assembled from the
conversation history, a price the model invented six turns ago would be sitting
in the prompt looking exactly like a price that came from the catalogue.

### The guard

**The invariant: a naira figure may appear in an outbound message if and only
if SQL produced it during this turn.**

This is the product. Vision §12 says the agent must never invent prices, stock
or policies. An agent that hallucinates a price is worse than no agent — it
creates a dispute the business must honour or lose a customer over. So it is
code with its own module and its own test, not a line in a prompt.

Four rules, first failure wins: money, authority (discounts, refunds, waivers),
payment confirmation, availability. Three details are load-bearing:

- **Per-turn.** A price from six turns ago is stale and must not authorise
  today's quote. This forces a fresh lookup on every quote, which is the point.
- **Four spellings.** Nigerians write prices as `₦12,500`, `12,500 naira`,
  `12.5k` and bare `12500`. A guard that only handles the symbol handles almost
  none of them in practice.
- **Two-way tested.** `test_guard.py` has a must-block table *and* a must-allow
  table. A guard with only the first passes by refusing everything, which is a
  broken product that looks safe.

A blocked reply is never written to `messages`. The customer never saw it, and
storing it would feed it back to the model next turn as something it had said.

### Money is an integer number of kobo, everywhere

No floats, no decimals, no exceptions. Paystack's `data.amount` is already kobo,
so the webhook does zero arithmetic — no divide by 100, therefore no
`12499.999999999998` in a payment request. Naira is rendered only at the edges.

One consequence worth knowing: `SUM(bigint)` returns `numeric` in Postgres, so
every sum is cast back with `::bigint`. Without it a `Decimal` leaks out of the
API.

### Send, *then* record

`channel.send()` happens before the outbound row is written. A failed send
therefore leaves no message, and the `jobs` row fails rather than succeeding.

The inverse order produces a transcript that claims a customer was told
something they never received — which is worse than a missing message, because
everything downstream (the model's next turn, the dashboard, the audit) then
treats a lie as a fact.

### Everything external sits behind a protocol, with a fake

WhatsApp → `SimulatorChannel`. Model → `ScriptedChat`. Payments →
`FakePaystack`. Transcription → a scripted one.

This is what makes the offline tests evidence about production rather than about
a harness. Two properties do the work:

- `ScriptedChat` can script **tool calls**, so a scripted first turn calls a
  tool, the real dispatcher runs real SQL, and the scripted second turn sees the
  result. The guard's allowed set is populated exactly as it would be live.
- `FakePaystack` signs with the **real** secret and posts at the real webhook. A
  bug in signature verification fails the test instead of the test skipping
  around it.

`scripts/e2e.py` runs the whole sale — eleven steps, no keys, no network.

### Tenancy: a policy *and* the predicate

Two mechanisms, deliberately.

Row-level security is driven by a transaction-scoped setting:
`set_config('aisales.business_id', %s, true)` rather than `SET LOCAL`, because
the latter cannot take a bound parameter and a tenant id interpolated into SQL
text is a tenant id in a log. `harden()` applies it with `force`, because
without `force` the table owner is exempt and in a small deployment the owner
*is* the app role.

But every query also carries its own `where business_id = …`. Relying on the
policy alone would make the application **incorrect** — not merely unprotected —
until somebody remembered to switch it on. The policy is the backstop for the
clause someone forgot; the clause is what makes the code right today.

Three details that were each found the hard way:

- `nullif(current_setting(…), '')`. `set_config` *defines* a custom parameter,
  so on a connection where a tenant was ever set and the transaction has since
  ended, the value reverts to the empty string — and `''::uuid` raises. Without
  the nullif, a reused connection fails every query with a type error instead of
  reading nothing.
- `harden()` refuses a role that is a superuser or holds `BYPASSRLS`. It would
  otherwise install policies that do nothing, silently, on every table at once.
- `SECURITY DEFINER` functions owned by the table owner are *still* subject to
  `force` RLS. The queue's cross-tenant exception has to be an actual role
  holding `BYPASSRLS`, not a definer function.

### Five boundaries, and no more

Exactly five things legitimately cross tenants, and each is named and tested:
`claim_job` / `reap_jobs`, the Paystack webhook, the WhatsApp webhook, the
operator connection, and the unscoped `businesses` table.

A sixth would be a leak nobody has noticed yet. `test_boundaries.py` asserts the
*set* — it fails if a new one appears, and it also fails if one of the five
disappears.

### The operator console is one module

The deployment needs somebody who can onboard a business and stop one that
should not be answering. That means cross-tenant access, which is the thing the
whole design is built to prevent.

The resolution: `operator.py` is the only module permitted to open that
connection, its routes are all gated on an operator principal, and every action
is audited. The boundary test asserts the *file list* has one member — so a
second module reaching for it fails the suite rather than passing review.

The console is off unless `AISALES_ADMIN_DSN` is set, and says so with a 503
rather than a 500 when it is not.

### Suspension stops four things, not one

There are four places a turn can begin, and a suspension that misses one is
worse than none at all: it looks off and quietly isn't.

1. **Intake** — a message arrives: store it, queue nothing.
2. **The worker** — jobs already queued when the flag flipped.
3. **`run_turn`** — the last cheap stop before a model call is paid for.
4. **Follow-up dispatch** — the only path that reaches a customer unprompted.

Inbound messages are still *recorded*. Dropping them loses a customer's words
that the platform has already been told we accepted.

Resuming catches up only threads inside WhatsApp's 24-hour service window.
Outside it Meta refuses free-form replies (error 131047), so queueing a turn
there is queueing a guaranteed failure; those threads are left flagged for a
person instead.

### Two surfaces, in different route groups

The dashboard is one shop's instrument. The console sees across all of them.
Wrapping the second in the first's sidebar would suggest the numbers beside it
were that shop's.

Layouts cannot read the pathname, so "which chrome does this page get" can only
be answered by where a page sits in the tree — hence `app/(business)/` and
`app/operator/`. Route groups do not appear in URLs, so nothing moved.

### The guard rails around secrets

`businesses.settings` is **public by design** — it is returned verbatim to any
member. So credentials live in `business_secrets`, encrypted with pgcrypto,
with the key as a *bound parameter* so it never reaches statement text.

A decryption failure raises. It never falls back to the platform key, because
there is no safe default for "whose credential is this" — the fallback sends one
business's messages over another's account.

`business_secrets` is tenant-scoped. `business_integrations` deliberately is
not: the WhatsApp resolver reads it *before* a tenant exists, since it is what
determines the tenant. It is safe only because it holds no credentials — a
`phone_number_id` travels in webhooks and URLs anyway.

---

## The failure modes designed against

| Must never happen | What stops it |
|---|---|
| A hallucinated price reaches a customer | The guard; blocked replies are never stored |
| A customer's "I don pay" is believed | Payment is confirmed only by a signed webhook, and the amount is compared to the order |
| A customer waits and gets no reply because every provider is out of quota | The job stays queued and retries; the conversation escalates; the customer gets a **static** line, because a model-generated apology can itself hallucinate |
| A webhook redelivery double-charges or double-replies | Uniqueness on `(business_id, provider_message_id)` and on `paystack_reference` |
| A customer who already answered gets chased anyway | Follow-ups cancel inside `record_inbound` |
| A message arriving mid-turn is silently dropped | `answered_upto_message_id`, written by the turn itself — ordering cannot detect it, because a mid-turn message has a *lower* id than the reply that ignored it |
| A business's agent answers after it was suspended | Four enforcement sites |
| One business's data appears in another's dashboard | The policy, the predicate, and `test_cross_tenant_api.py` |

---

## What is deliberately not built

Each of these is a decision, not an oversight.

- **No ORM.** Hand-written SQL, so the tenancy predicate and the `::bigint` cast
  are visible where they matter.
- **No self-service signup.** Creating a business is a CLI command or the
  console. A signup form needs email verification, a rule for which business a
  stranger joins, and a control plane — none of which is on the path to the
  first ten customers.
- **No billing or usage metering.** The numbers it needs
  (`messages.cost_kobo`) are already recorded. It becomes worth building when
  there is a second invoice to send.
- **No business switcher.** A user who belongs to several businesses currently
  gets the first. Worth doing when someone actually belongs to two.
- **No dedicated-deployment automation.** See `.kiro/specs/multi-tenant/deployment.md`:
  the same image, one business per stack, and a migration script written when
  there is a real business to move rather than a customer who might ask.

---

## Where the ideas came from

Two things shaped this more than any code decision.

**Meta's 24-hour service window.** Free-form messages are only permitted within
24 hours of the customer's last inbound, and a follow-up is the part of the
product that most naturally wants to reach further than that. So the follow-up
default is 120 minutes, not "next day", and the whole feature is scoped to the
window until templates are approved.

**Stripe cannot serve Nigerian businesses.** They cannot open standard Stripe
accounts or receive payouts to Nigerian bank accounts. Hence Paystack — and
hence the webhook handler, which is where the money is and therefore where the
three corrections to the local precedent live.
