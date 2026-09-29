# Deploying: pooled, and one-business-per-stack

Two shapes, one codebase. Nothing in this document requires a code change —
that is the point of writing it, and the reason it is worth writing before
anyone asks.

The relevant question is not "which is better". It is **what does a given
business actually need**, and the two answers differ on exactly one axis:
whether they can share a database process with another business.

---

## The default: pooled

One API, one worker, one Postgres, many businesses in it. This is what the
pilot runs and what everything in this repository is built for.

```
                    ┌──────────────────────────┐
   browser ────────▶│  Next.js (dashboard)     │
                    └────────────┬─────────────┘
                                 │  session cookie
                    ┌────────────▼─────────────┐
   WhatsApp ───────▶│  FastAPI                 │  signature, not session
   Paystack ───────▶│                          │
                    └────────────┬─────────────┘
                                 │  set_config('aisales.business_id', …)
                    ┌────────────▼─────────────┐
                    │  Postgres + RLS          │◀── worker
                    └──────────────────────────┘
```

What separates two businesses in that picture:

| Mechanism | Where | What it stops |
|---|---|---|
| Row-level security, `force`d, on eleven tables | `db.harden()` | A query that forgot `where business_id = …` |
| Explicit `where business_id = …` in the app | every route and helper | RLS being off, which is the default until `harden()` runs |
| A session naming the business, never a parameter | `api/app.py` | Choosing a tenant by asking for it |
| `unique (provider, external_account_id)` | `business_integrations` | Two businesses claiming one WhatsApp number |
| `SECURITY DEFINER` claim/reap owned by a `BYPASSRLS` role | `db.QUEUE_FUNCTIONS` | Everything else that would need to cross tenants |
| Per-business channels and Paystack clients | `channels.for_business`, `paystack.for_business` | One business's replies or money moving through another's account |
| An operator role with its own DSN, unreachable from the API | `db.operator` | Looking across tenants without it being a decision |

Setting it up:

```bash
createdb aisales
AISALES_DSN=postgresql:///aisales .venv/bin/python -m aisales_worker --install --seed
AISALES_DSN=postgresql:///aisales .venv/bin/python -m aisales_worker \
    --create-user you@example.com --name "Your Name"
```

**`db.harden()` must be run, and it is not part of `install()` on purpose.**
It refuses a role that is a superuser or holds `BYPASSRLS`, because row-level
security does not apply to one and the policies would be decoration. When the
application connects as such a role, isolation is being provided by the
explicit `where` clauses alone — which is real but is one mechanism, not two.
For a deployment carrying other people's customer conversations, connect as a
role without either attribute.

---

## The other shape: one business per stack

The same image, deployed once per business, with its own database. No RLS is
doing any work, because there is only ever one tenant in the process.

**This is not a different product.** It is the pooled build with a smaller
`businesses` table, and it works today because every mechanism above degrades
correctly when there is one business:

- A session still names its business; there is just one to name.
- `for_business` finds that business's integration; there is just one.
- The queue's cross-tenant claim sees the same jobs it would have seen anyway.
- RLS still applies, and still fails closed, if it is switched on.

### What actually changes

| | Pooled | Per-stack |
|---|---|---|
| `AISALES_DSN` | shared | one database per business |
| `AISALES_SECRET_KEY` | platform-wide | per business, and can be held in that customer's own KMS |
| Where customer data lives | one database | that customer's own |
| Blast radius of a bad migration | every business | one |
| Blast radius of a noisy neighbour | shared process | none |
| Cost | one stack | one stack each |
| Upgrades | once | N times, or a fleet |

### When a business will ask for it

Three reasons come up, and only the first two are technical:

1. **Data residency or a security questionnaire** that will not accept a shared
   database, however good the row-level policy is. This is the common one, and
   it is not a disagreement about the engineering.
2. **A contractual right to their own backups and restore point.** A pooled
   restore is all-or-nothing, and "we restored every business to fix yours" is
   a sentence no account manager wants to say.
3. **A very large customer** who wants their own release cadence.

### What to do when it happens

1. Provision a Postgres for that business.
2. Run the same image with `AISALES_DSN` pointed at it.
3. `--install`, then `--create-user` for their staff.
4. Migrate their rows out of the pooled database with a scoped export, and
   delete them from it once their stack is verified.

There is deliberately no automated path for step 4. It is a walk over the
eleven scoped tables filtered by one `business_id`, which is a script somebody
writes when they have a real business to move — not a feature to build, test
and maintain against a customer who may never ask. Writing it now would be
guessing at the shape of a migration nobody has needed yet.

---

## What is deliberately not built

- **Self-service signup.** Creating a business is a CLI command. A signup form
  needs email verification, a way to decide which business a stranger joins, and
  a control plane to hold it — none of which is on the path to the first ten
  customers. See `--create-user` in `worker/src/aisales_worker/__main__.py`.
- **Billing and usage metering.** Phase C in `tasks.md`. It becomes worth
  building when there is a second invoice to send, and the numbers it needs
  (`messages.cost_kobo`) are already recorded.
- **A dedicated-deployment fleet.** Nothing above is automated because a fleet
  of one is a deployment, and a fleet of two is a runbook.
