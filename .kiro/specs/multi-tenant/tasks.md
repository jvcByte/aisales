# Implementation Plan: Multi-Tenant Isolation

## Overview

Four phases, each gated on a business rather than a date. **Phase A is needed for the first customer to be a real product at all** — it is not tenancy work, it is the difference between a deployment and a product. Phase B is needed the moment a second business exists. Phases C and D are deliberately deferred until evidence exists.

Each phase is independently verifiable, and every phase ends with its isolation tests passing rather than with code merged.

---

## Tasks

- [ ] 1. Phase A — Authentication
  - [x] 1.1 Add the `users`, `sessions` and `business_members` tables from `design.md` to `db.SCHEMA`, with
      `alter table ... add column if not exists` for every column, as the rest of the schema does.
    - _Requirements: 1.3, 1.4, 2.1_
  - [x] 1.2 Implement password hashing with a memory-hard algorithm, and a `verify()` that does not
      short-circuit on a missing user — a login that returns faster for an unknown address leaks which
      addresses exist.
    - _Requirements: 1.3_
  - [x] 1.3 Implement session issue, verify and revoke. Store the token hashed; a leaked session table
      must not be a set of usable tokens.
    - _Requirements: 1.4, 1.5_
  - [x] 1.4 Add authentication as a FastAPI dependency on every business-data route, returning 401
      without disclosing whether the resource exists.
    - _Requirements: 1.1, 1.2_
  - [x] 1.5 Rate-limit authentication per account and per source address.
    - _Requirements: 1.6_
  - [x] 1.6 Add a sign-in surface to the dashboard and a session cookie that is `HttpOnly`, `Secure`
      and `SameSite`.
    - _Requirements: 1.5_
  - [x]* 1.7 Add a `--create-user` / `--add-member` path to the worker CLI, so Phase A can be used
      before any onboarding UI exists.
    - _Requirements: 2.1_

- [ ] 2. Phase A — Tenant context from the session
  - [x] 2.1 Implement `current_business(request)`, resolving `business_id` from the session's
      `business_members` row — never from a query parameter, body field or header.
    - _Requirements: 1.7, 2.1_
  - [x] 2.2 Return the same response for "not a member" as for "does not exist", so membership cannot
      be probed.
    - _Requirements: 2.2, 10.4_
  - [x] 2.3 Delete `_sole_business()` and its three call sites (`app.py:162`, `:262`, `:454`), replacing
      each with a failure.
    - _Requirements: 2.3, 2.4_
  - [x] 2.4 Remove the `slug` query parameter as an authorization input across all list endpoints, and
      update every dashboard page that supplies it.
    - _Requirements: 2.5_
  - [x] 2.5 Remove `payload.get("slug")` from `/sim/messages` (`app.py:153`) — a client currently
      chooses which business it writes into. Derive it from the session, and mount the simulator only
      for an operator.
    - _Requirements: 2.1, 2.5, 4.9_
  - [x] 2.6 Remove the hardcoded `SLUG` from `web/lib/api.ts:3` and thread the session's business
      through the client.
    - _Requirements: 2.1_
  - [x] 2.7 Write `test_cross_tenant_api.py`: authenticated as A, request B's conversation, order and
      customer; assert each response is indistinguishable from a non-existent resource.
    - _Requirements: 10.4_

- [ ] 3. Phase A — Credential hygiene
  - [x] 3.1 Document `businesses.settings` in the schema as **public by design** — data that may be
      returned to the business's members in full, and therefore never a place for a credential.
    - _Requirements: 8.1, 8.2_
  - [x]* 3.2 Keep the `EDITABLE` allow-list on the settings endpoint, and assert that a write to an
      unknown key is refused by name rather than silently dropped.
    - _Requirements: 8.4_
  - [x] 3.3 Write `test_secrets.py` asserting no documented secret type appears anywhere in
      `businesses.settings`, so a future contributor adding one is stopped by a test rather than by
      review.
    - _Requirements: 7.2, 7.6_
  - [x] 3.4 Confirm the settings endpoint returns no field of `business_secrets`.
    - _Requirements: 7.7_

- [ ] 4. Phase B — Row-Level Security
  - [x] 4.1 Draw up the policy set: `enable` and **`force`** row level security on all eleven
      business-scoped tables, with `using` and `with check` both present. `force` is not optional — the
      table owner is otherwise exempt, and in a small deployment the owner is the app role.
    - _Requirements: 3.1, 3.2, 3.3, 3.6_
  - [x] 4.2 Implement the `tenant()` context manager using `set_config('aisales.business_id', %s, true)`,
      opening a real transaction rather than relying on `autocommit`. Audit every existing call site
      that assumed autocommit.
    - _Requirements: 3.5, 3.6_
  - [x] 4.3 Assert the fail-closed expression: with the setting unset, `current_setting(...)::uuid` is
      NULL and the policy matches nothing. Add a guard that the context is never set to an empty
      string, which would raise instead of returning nothing.
    - _Requirements: 3.4, 5.1, 5.5_
  - [x] 4.4 Create the `aisales_queue` role with `BYPASSRLS` and the `claim_job` / `reap_jobs`
      `SECURITY DEFINER` functions it owns, granted to the app role. A `SECURITY DEFINER` function
      owned by the *table owner* is still subject to `force` RLS, so the bypass must be a role.
    - _Requirements: 4.2, 4.3_
  - [x] 4.5 Confirm `heartbeat`, `finish`, `fail` and `get` correctly require a tenant context, so the
      exception is two functions rather than the `jobs` table.
    - _Requirements: 4.4_
  - [x] 4.6 Write `test_rls.py`: no context reads nothing; a tenant cannot write another's row; a
      secret is unreadable outside its tenant.
    - _Requirements: 10.5, 3.7_

- [ ] 5. Phase B — The worker under RLS
  - [x] 5.1 Wrap the turn in `runner._turn` with `tenant(dsn, job.business_id)`.
    - _Requirements: 3.5, 9.1_
  - [x] 5.2 Keep `claim` outside the tenant transaction — a worker must be able to pick up any
      business's job, so the scan happens before the context exists.
    - _Requirements: 4.1, 4.2_
  - [x] 5.3 Write `test_tenant_isolation.py`: queue a job for A, one for B, one for A; run them in
      sequence on **one** worker connection; assert each read and wrote only its own rows and that the
      context did not survive between jobs.
    - _Requirements: 10.1, 10.2, 10.3_
  - [x]* 5.4 Add a deliberately-wrong second worker that sets the context at connect time, and assert
      the test in 5.3 fails against it. A test for a leak that cannot fail is not a test.
    - _Requirements: 10.1_

- [ ] 6. Phase B — Integrations and boundaries
  - [x] 6.1 Add `business_integrations` with the `unique (provider, external_account_id)` index — this
      index is what makes "two businesses claim one number" impossible rather than unlikely.
    - _Requirements: 6.1, 6.2, 5.4_
  - [x] 6.2 Implement `resolve_whatsapp_tenant(phone_number_id)`, returning one `uuid` or None, called
      before any tenant transaction opens.
    - _Requirements: 4.7, 4.8, 5.2_
  - [x] 6.3 Implement `resolve_payment_tenant(reference)` the same way.
    - _Requirements: 4.5, 4.6, 5.3_
  - [x] 6.4 Route outbound WhatsApp sends through the conversation's own integration, and outbound
      sends through nothing at all when the integration is absent.
    - _Requirements: 6.3, 6.5_
  - [x] 6.5 Resolve the Paystack account per business; confirm no code path settles one business's
      payment into another's account.
    - _Requirements: 6.4_
  - [x] 6.6 Record integration status, so a lapsed WhatsApp connection is visible rather than silently
      retried.
    - _Requirements: 6.6_
  - [x] 6.7 Write `test_boundaries.py` covering each boundary: correct business selected, unknown
      identifier rejects, `claim`/`reap` see all tenants while `finish`/`fail` see one.
    - _Requirements: 10.6, 10.7, 5.2, 5.3, 5.7_

- [ ] 7. Phase B — Secrets
  - [x] 7.1 Add `business_secrets` and `pgcrypto`; implement encrypt and decrypt with the key as a
      bound parameter so it never appears in statement text.
    - _Requirements: 7.1, 7.2, 7.3_
  - [x] 7.2 Implement `key_version` and a re-encryption pass, so rotation is a walk rather than a
      flag day.
    - _Requirements: 7.4_
  - [x] 7.3 Read secrets only inside the owning tenant's transaction, and fail the operation rather
      than falling back to a platform default when decryption fails.
    - _Requirements: 7.5_
  - [x]* 7.4 Document the two server settings that must be off in production, since the key reaches
      the database on every read: `log_statement = 'all'` and `log_min_duration_statement = 0`.
    - _Requirements: 7.3_

- [ ] 8. Phase B — The operator boundary
  - [x] 8.1 Create the operator role with `BYPASSRLS` and an `AISALES_ADMIN_DSN` that is not read by
      the business API.
    - _Requirements: 4.9_
  - [x] 8.2 Audit every statement executed through the operator connection, naming the operator and
      the tenant touched.
    - _Requirements: 4.10_
  - [x] 8.3 Assert the operator role is unreachable from any route an ordinary business request can
      take — a test, not a convention.
    - _Requirements: 4.9_

- [ ] 9. Phase C — After three to five businesses
  - [ ]* 9.1 Record per model call: business, model, input and output tokens, provider.
    - _Requirements: 9.1, 9.4_
  - [ ]* 9.2 Record per outbound channel message: business and estimated channel cost. `messages.cost_kobo`
      already exists for this and is unused.
    - _Requirements: 9.2_
  - [ ]* 9.3 Expose per-business usage for a period, and the count exceeding a configured allowance
      without cutting service.
    - _Requirements: 9.3, 9.5_
  - [ ]* 9.4 Automate onboarding — create business, member, integrations, secrets, catalogue — only
      once the manual version has been done three times and the steps are known.
    - _Requirements: —_

- [ ] 10. Phase D — When a customer requires it
  - [x]* 10.1 Document the dedicated-deployment path: the same image, one business per stack, with the
      pooled code unchanged. Do not build it before a customer asks.
    - _Requirements: —_

- [x] 11. The operator console — onboarding, oversight, suspension
  - [x] 11.1 `businesses.suspended_at` / `suspended_reason`, `users.is_operator`, both as
      `alter table … add column if not exists` so an existing database picks them up.
  - [x] 11.2 `principal_for` no longer INNER JOINs `business_members`: an operator belongs to
      no business, and an inner join resolved them to NULL — the same answer as a bad token.
  - [x] 11.3 `business_secrets` added to `SCOPED_TABLES`. It was missing, so the table holding
      every business's credentials had no policy at all. `business_integrations` stays unscoped
      and is documented: the resolver reads it before a tenant exists.
  - [x] 11.4 Suspension enforced at four sites — intake, worker, `run_turn`, follow-up dispatch
      — each matching the existing takeover-suppression shape rather than inventing a new one.
  - [x] 11.5 Resume catches up only threads inside WhatsApp's service window; older ones are
      left flagged for a person.
  - [x] 11.6 `api/src/aisales_api/operator.py`, the one module permitted a cross-tenant
      connection, with every route gated and every action audited in `operator_audit`.
  - [x] 11.7 `/operator` in its own route group, with its own chrome and error boundary.
      The business pages moved to `app/(business)/` so the two surfaces cannot share a shell.
  - [x] 11.8 `--operator` on `--create-user`, and `install_operator()` run from `--install`.
  - [x] 11.9 `test_suspension.py`, `test_operator.py`, and the boundary test narrowed from
      "no module may" to "exactly this module may".
