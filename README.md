# AI Sales Employee

A WhatsApp-first AI sales assistant for Nigerian businesses: it answers
customers, qualifies leads, follows up, sends Paystack payment links, creates
orders, and reports what happened to the owner.

The governing constraint is that **it must never invent a price, a stock
figure, a policy, or a payment confirmation.** A sales agent that hallucinates
a price creates a dispute the business has to honour or lose a customer over,
so that rule is enforced by code (`core/src/aisales/guard.py`) rather than by
asking a model nicely.

Everything runs offline. There is no WhatsApp access yet and no Paystack
account, so the model, the messaging channel and the payment provider all sit
behind protocols with fakes behind them.

---

## Ports

| Service | Port | Why not the default |
|---|---|---|
| API + simulator | **8100** | 8000 is already `pycoding-api` on this machine |
| Dashboard | **3100** | 3000 is already `pycoding/web` |
| Postgres | 5432 | shared, database `aisales` |

## Run it

```bash
cd ~/Software/AI_SALES_EMPLOYEE

# Once.
python3 -m venv .venv
.venv/bin/pip install -e core/ -e api/ -e worker/
createdb aisales
.venv/bin/python -m aisales_worker --install --seed
```

`--seed` creates one pilot business (Ada Fabrics), its 8-product catalogue and
its settings. It is idempotent, so re-running is safe.

Give yourself a sign-in. The dashboard, the API and the simulator all require
one; there is no anonymous access to a business's conversations.

```bash
AISALES_PASSWORD='choose-something-long' \
  .venv/bin/python -m aisales_worker --create-user you@example.com --name "Your Name"
```

The password comes from `AISALES_PASSWORD` or a prompt, never from an argument,
because an argument is in your shell history and in `ps` output for everyone
else on the machine.

To run the deployment rather than one shop — a console listing every business,
with onboarding and suspension — make yourself an operator instead:

```bash
AISALES_PASSWORD='choose-something-long' \
  .venv/bin/python -m aisales_worker --create-user you@example.com --operator
```

An operator belongs to no business and sees across all of them. It needs
`AISALES_ADMIN_DSN` set (see `.env.example`) — without it the console says so
rather than failing, since a single-business deployment is not expected to have
one.

```bash
# Terminal 1 — the API and the simulator.
.venv/bin/python -m aisales_api
#   → http://127.0.0.1:8100/sim  (sign in first)
```

Open that URL and type as a customer. The simulator page calls the **same**
`agent.run_turn` the worker calls, so what you see there is production
behaviour rather than a test harness.

```bash
# Terminal 2 — the dashboard.
pnpm install
cd web && pnpm dev
#   → http://127.0.0.1:3100  (redirects to /sign-in)
```

```bash
# Terminal 3 — the worker. This is what actually answers.
export GEMINI_API_KEY=...        # or GROQ_API_KEY / HF_TOKEN / OPENROUTER_API_KEY
.venv/bin/python -m aisales_worker --poll 2
```

**The worker needs a model key.** Without one the simulator will record your
messages and queue turns, but nothing will reply — there is no model to reply
with. Everything else (the dashboard, the payment flow, the tests, the
end-to-end script) runs with no keys at all.

## The operator console

Everything above runs **one** business. The console is the other surface: it
sees every business on the deployment, onboards new ones, and can suspend one
so its agent stops answering.

It is off unless you switch it on, because it is not for shop owners — it reads
across every tenant, and a deployment running a single business does not need it.

### Turning it on

```bash
# 1. The role and the columns. Idempotent; safe to re-run.
.venv/bin/python -m aisales_worker --install

# 2. An operator account. An operator belongs to NO business -- that is the
#    point of the flag, so it takes no --business.
AISALES_PASSWORD='choose-something-long' \
  .venv/bin/python -m aisales_worker --create-user you@example.com --operator
```

Then add two lines to `.env` and restart the API:

```bash
AISALES_ADMIN_DSN=postgresql:///aisales
AISALES_SECRET_KEY=$(openssl rand -base64 48)
```

`AISALES_SECRET_KEY` is only needed if you store a business's WhatsApp or
Paystack credentials from the console — they are encrypted with it. Set it
before you onboard anything, because changing it later makes existing secrets
unreadable.

Open **http://127.0.0.1:3100/operator** and sign in as the operator.

### The DSN rule

`AISALES_ADMIN_DSN` is deliberately not `AISALES_DSN`. The business API must
not hold a cross-tenant connection: `test_boundaries.py` asserts that exactly
one module may open one, and it is not any of the routes serving shop owners.

**The role that DSN authenticates as must be a member of `aisales_operator`, or
be a superuser**, because opening a console session means `SET ROLE
aisales_operator` on that connection. Locally, connecting as yourself is
usually enough. For a deployment that is not, the error says exactly what to
run:

```
GRANT aisales_operator TO your_role;
```

Without `AISALES_ADMIN_DSN` the console does not fail — it answers **503** and
says the DSN is unset, which is the correct state for a single-business
deployment.

### What it does

| | |
|---|---|
| **Lists** | Every business with its state, its WhatsApp and Paystack connections, and messages / leads / open threads for the last 30 days |
| **Onboards** | Creates the business, its owner's sign-in, its integrations, and its encrypted credentials in one step |
| **Suspends** | The agent stops answering immediately and scheduled follow-ups are cancelled. Customers' messages are still **recorded**, so nothing is lost and you can see what was missed |
| **Resumes** | Starts answering again, and catches up any thread whose last message is unanswered *and* still inside WhatsApp's 24-hour window. Older ones are left flagged for a person — outside the window Meta refuses free-form replies, and a backlog answered the instant a switch is flipped is a backlog nobody read |

Every operator action is written to `operator_audit` with the account that did
it, so "who looked at this customer's messages" has an answer. A suspended
business's own staff see a banner saying the agent is paused, rather than a
dashboard that silently answers nobody.

## Check it works

```bash
.venv/bin/python -m pytest -q          # 205 tests, no keys, no network
.venv/bin/python scripts/e2e.py        # the whole slice, 11 steps
```

`scripts/e2e.py` walks one sale end to end: a price is looked up and quoted, an
invented one is blocked, an untracked-stock item is sold, an order and a
payment link are created, a customer's "I don pay" is refused, a signed Paystack
callback settles the order, a replay changes nothing, a short payment is
rejected, a follow-up fires, and the digest agrees with the ledger. It creates
its own database (`aisales_e2e`).

## Configuration

Copy `.env.example` to `.env`. Nothing in it is required for the tests.

| Variable | Default | Notes |
|---|---|---|
| `AISALES_DSN` | `postgresql:///aisales` | |
| `GEMINI_API_KEY` etc. | — | first one present wins; the chain survives running out |
| `PAYSTACK_SECRET_KEY` | — | `sk_test_` works before business verification |
| `AISALES_FAKES` | `1` in the example | **set to 0 in production** |
| `AISALES_CHANNEL` | `simulator` | `whatsapp` needs the keys below |
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` | — | only for the Meta adapter |
| `AISALES_API` | `http://127.0.0.1:8100` | where the dashboard finds the API. Baked into the dashboard's rewrite at **build** time, so changing it needs `pnpm build`, not just a restart |
| `AISALES_SECRET_KEY` | — | encrypts stored business credentials. No default: unset means nothing can be encrypted or read |
| `AISALES_ADMIN_DSN` | — | switches on the operator console. See above |

`AISALES_FAKES=1` with a live channel is refused at startup rather than
warned about: a scripted model answering a real customer looks completely
healthy from the outside.

## What is where

```
core/src/aisales/
  guard.py       the safety property. a ₦ figure may be sent iff SQL produced it this turn
  agent.py       one conversation turn, and the AI/human state machine
  tools.py       the ten things the agent can do, and what each authorises
  prompts.py     the system prompt and the Nigerian commerce intent layer
  providers.py   the model, behind a Protocol, with the free-tier fallback chain
  channels.py    the messaging boundary: simulator and WhatsApp
  paystack.py    the payment client and the webhook handler
  followup.py    the follow-up policy. the timer is a column, not a cron
  db.py          the schema, the queue, and the connection
api/             FastAPI: channel webhooks, dashboard API, the simulator
worker/          the process that claims jobs and runs turns
web/             the owner's dashboard (Next.js)
scripts/e2e.py   the whole slice, one runnable check
.kiro/specs/     requirements, design and tasks
PRIVACY.md       what is stored, the lawful basis, and the NDPA gaps
```

The specification is in `.kiro/specs/ai-sales-employee/`. `design.md` carries
the full schema and the twenty-one correctness properties the tests defend.

## Deploying

```bash
docker compose up -d postgres
.venv/bin/python -m aisales_worker --install --seed
docker compose up api worker
```

One image, three roles; the worker image does not install FastAPI. Set
`AISALES_FAKES=0` and the real keys first.

## Before real customers

- **NDPA.** Read `PRIVACY.md`. Messages reach model providers outside Nigeria,
  and retention is the one decision that is genuinely hard to retrofit.
- **Paystack onboarding** is days to weeks and needs CAC registration. Start it
  before the pilot, not after.
- **The intent layer is measured at 30/30** on a golden set of 30 Nigerian
  utterances, but that set is one person's idea of Nigerian English. Reseed it
  from the pilot business's real WhatsApp history and re-record:
  `python scripts/record_intent_fixture.py`.
