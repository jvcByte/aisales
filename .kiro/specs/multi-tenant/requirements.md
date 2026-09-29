# Requirements Document

## Introduction

The platform currently serves **one business per deployment**. Every business-scoped table already carries `business_id`, and the worker already resolves a business per job — so the *data* would not collide. But the *runtime* is single-tenant in seven specific places, the most serious being that **there is no authentication at all**.

This specification covers the move to a pooled multi-tenant SaaS, sequenced by which business creates the requirement rather than by a roadmap:

| Trigger | What is built | Why now |
|---|---|---|
| **Business #1 becomes real** | Authentication, session-derived tenant context, removal of every "the only business" fallback, credential hygiene | Without these it is a demo, not a product — true whether or not a second business ever exists. |
| **Business #2 exists** | Row-Level Security, `business_integrations`, `business_secrets`, per-tenant Paystack and WhatsApp | The first moment a forgotten `WHERE` can leak another shop's customers. |
| **Businesses #3–5** | Usage metering, onboarding automation, billing | Only once a commercial pattern is visible. |
| **When a customer needs it** | Dedicated deployment for one tenant | An isolation requirement, not an ordinary onboarding path. |

**The governing invariant.** Once a request or job has resolved a `business_id`, every subsequent access to business data occurs under that tenant's RLS context. Cross-tenant access is permitted **only** at the five boundaries named in Requirement 4, each of which is documented, narrow, and tested.

**Out of scope.** Self-service signup, subscription billing, a public marketing site, and the dedicated-tenant deployment path. Those are named here so they are not built by accident.

**A note on the AI quota.** An earlier draft treated the free-tier model allowance as an argument for one deployment per business. That was wrong: provider rate limits are applied per project and move to a much higher tier once billing is enabled, so the quota is an operating-cost decision, not a tenancy decision. Requirement 10 exists so that cost becomes measurable per business rather than being anticipated.

---

## Glossary

- **Tenant**: One business. The unit of isolation.
- **Tenant_Context**: The `business_id` in force for the current transaction, carried in the PostgreSQL session setting `aisales.business_id`.
- **RLS**: PostgreSQL Row-Level Security. A policy attached to a table that filters every query against it, including queries issued by the table's owner.
- **Policy**: An RLS policy enforcing `business_id = current_setting('aisales.business_id', true)::uuid`.
- **Boundary**: A place where the system deliberately operates without a Tenant_Context, because the tenant is not yet known, or because the operation is inherently cross-tenant. There are exactly five.
- **Resolution_Boundary**: A Boundary whose purpose is to *determine* the tenant (WhatsApp webhook, Paystack webhook, session lookup).
- **Queue_Scan**: A Boundary that must examine jobs belonging to any business (`claim`, `reap`).
- **Operator**: A person who works for the platform rather than for a business. Reads across tenants for support and debugging.
- **Operator_Role**: A distinct database role holding `BYPASSRLS`, used only by the operator surface, on its own connection.
- **Business_Member**: A person with access to one business. A person may belong to more than one.
- **Integration**: A per-business external connection — a WhatsApp number, a Paystack account.
- **Secret**: A credential belonging to one business. Never stored in `businesses.settings`.
- **Fail_Closed**: When a Tenant_Context is absent or cannot be resolved, the system refuses to act. It does not guess, default, or fall back.
- **Cross_Tenant_Test**: A test that authenticates as Business A and asserts it can neither read nor write Business B's data.

---

## Requirements

### Requirement 1: Authentication and Sessions

**User Story:** As a business owner, I want my shop's conversations to be private to me, so that I can put real customer data into this system.

#### Acceptance Criteria

1. THE platform SHALL require an authenticated session for every endpoint that returns or modifies business data.
2. WHEN a request arrives without a valid session, THE platform SHALL reject it without disclosing whether the requested resource exists.
3. THE platform SHALL store credentials using a memory-hard password hash, and SHALL NOT store a password in recoverable form.
4. WHEN a session is created, THE platform SHALL record the user, the issuing time and an expiry.
5. THE platform SHALL provide a sign-out that invalidates the session server-side, not merely in the browser.
6. THE platform SHALL rate-limit authentication attempts per account and per source address.
7. THE platform SHALL NOT accept a `business_id` supplied by the client as evidence of authorization.

### Requirement 2: Session-Derived Tenant Context

**User Story:** As the operator, I want the tenant to come from the session rather than the request, so that a client cannot choose whose data it sees.

#### Acceptance Criteria

1. WHEN an authenticated request touches business data, THE platform SHALL derive the Tenant_Context from the session's Business_Member record.
2. IF the session's user is not a member of the business being addressed, THEN THE platform SHALL reject the request with the same response it gives for a non-existent resource.
3. THE platform SHALL NOT resolve a business by "the only one that exists" under any circumstance.
4. WHEN more than one business exists and no tenant can be resolved, THE platform SHALL fail rather than choose.
5. THE platform SHALL scope every list endpoint to the session's business, and SHALL NOT accept a business identifier as a query parameter for authorization purposes.

### Requirement 3: Tenant Isolation Enforced by the Database

**User Story:** As the operator, I want isolation enforced by the database rather than by discipline, so that one forgotten `WHERE` clause cannot leak a shop's customers.

#### Acceptance Criteria

1. THE platform SHALL enable Row-Level Security on every table that carries `business_id`.
2. THE platform SHALL force Row-Level Security on those tables, so that the table owner is not exempt.
3. THE platform SHALL define, for each such table, a policy permitting rows where `business_id` equals the Tenant_Context.
4. WHEN the Tenant_Context is not set, THE policy SHALL match no rows and permit no writes.
5. THE platform SHALL set the Tenant_Context using `set_config('aisales.business_id', <value>, true)`, which is transaction-scoped.
6. THE platform SHALL NOT set the Tenant_Context for the duration of a connection, because a pooled or reused connection would carry one tenant's context into another's work.
7. THE platform SHALL provide a Cross_Tenant_Test for each business-scoped table.

### Requirement 4: The Five Cross-Tenant Boundaries

**User Story:** As a developer, I want every place that operates without a tenant to be named and narrow, so that the exceptions are findable rather than scattered.

#### Acceptance Criteria

1. THE platform SHALL have exactly five boundaries, and each SHALL be named in this specification and in the implementation.
2. **Queue_Scan.** The Queue_Scan boundary SHALL permit `claim` and `reap` to examine jobs belonging to any business.
3. The Queue_Scan boundary SHALL be implemented as database functions owned by a role that is not subject to RLS, rather than by a flag the application sets.
4. Operations on a job already held — `heartbeat`, `finish`, `fail`, `get` — SHALL NOT use the Queue_Scan boundary; they SHALL run under the Tenant_Context of the job's business.
5. **Paystack webhook.** WHEN a callback arrives, THE platform SHALL resolve the business from the payment reference before entering any Tenant_Context.
6. Paystack resolution SHALL return only the `business_id` for one reference, and SHALL NOT expose any other row.
7. **WhatsApp webhook.** WHEN a delivery arrives, THE platform SHALL resolve the business from `phone_number_id` before entering any Tenant_Context.
8. WhatsApp resolution SHALL return only the `business_id` for one integration, and SHALL NOT expose any other row.
9. **Operator access.** The Operator_Role SHALL be a distinct database role holding `BYPASSRLS`, used on a separate connection, and SHALL NOT be reachable from an ordinary business request path.
10. THE platform SHALL record every Operator query that reads across tenants in the audit log.
11. **`businesses` table.** The `businesses` table SHALL remain outside RLS because it is the resolution root, and SHALL NOT store credentials.

### Requirement 5: Fail-Closed Behaviour

**User Story:** As the operator, I want the system to refuse rather than guess, so that the silent cross-tenant bug cannot happen.

#### Acceptance Criteria

1. IF no Tenant_Context can be established, THEN THE platform SHALL refuse to read or write business data.
2. IF an inbound webhook carries a `phone_number_id` that matches no integration, THEN THE platform SHALL reject the delivery and SHALL NOT fall back to another business.
3. IF a payment callback carries a reference that matches no payment, THEN THE platform SHALL acknowledge it and SHALL NOT attribute it to any business.
4. IF an inbound webhook carries a `phone_number_id` that matches integrations belonging to more than one business, THEN THE platform SHALL reject the delivery and record the conflict.
5. IF the Tenant_Context is unset during a query against an RLS-protected table, THEN THE query SHALL return no rows rather than all rows.
6. THE platform SHALL NOT disable RLS, widen a policy, or assume a default tenant in response to a runtime error.
7. THE platform SHALL provide a test for each fail-closed case above.

### Requirement 6: Per-Business Integrations

**User Story:** As a business owner, I want my WhatsApp number and my Paystack account to be mine, so that my messages and my money do not pass through anyone else's account.

#### Acceptance Criteria

1. THE platform SHALL store each external connection in a `business_integrations` record carrying its business, its provider, and the provider's own identifier for it.
2. THE platform SHALL route an inbound WhatsApp delivery by `phone_number_id`, and SHALL have at most one active integration per `phone_number_id`.
3. THE platform SHALL send through the integration belonging to the conversation's business, and SHALL NOT use one business's credentials for another's message.
4. THE platform SHALL resolve the payment provider account per business, and SHALL NOT settle one business's payment into another's account.
5. WHEN an integration is disconnected or revoked, THE platform SHALL stop sending through it and SHALL surface the failure rather than silently retrying.
6. THE platform SHALL record the integration's status, so that a business whose WhatsApp access has lapsed is visible.

### Requirement 7: Secrets Are Separate From Settings

**User Story:** As the operator, I want credentials in a separate store from configuration, so that an endpoint which returns configuration cannot return a credential.

#### Acceptance Criteria

1. THE platform SHALL store credentials in `business_secrets`, keyed by business, provider and secret type.
2. THE platform SHALL store each secret encrypted, and SHALL NOT store any secret in `businesses.settings`.
3. THE platform SHALL keep the encryption key outside the database, and SHALL NOT store it alongside the ciphertext it protects.
4. THE platform SHALL provide a means to rotate a key, and SHALL record when each secret was last rotated.
5. WHEN a secret is read, THE platform SHALL do so under the Tenant_Context of the business that owns it.
6. THE platform SHALL provide a test asserting that no documented secret type appears in `businesses.settings`.
7. THE platform SHALL provide a test asserting that the settings endpoint does not return any field of `business_secrets`.

### Requirement 8: Settings Are Public By Design

**User Story:** As a developer, I want the settings blob to be safe to return verbatim, so that its safety does not depend on remembering to filter it.

#### Acceptance Criteria

1. THE platform SHALL treat `businesses.settings` as data that may be returned to the business's own members in full.
2. THE platform SHALL document, in the schema, that `businesses.settings` is not a location for credentials.
3. WHEN a new setting is added, THE platform SHALL NOT add a credential to it.
4. THE settings endpoint SHALL continue to refuse writes to keys outside the documented editable set.

### Requirement 9: Per-Business Operational Accounting

**User Story:** As the operator, I want to see what each business costs to serve, so that I can price the product from evidence rather than assumption.

#### Acceptance Criteria

1. THE platform SHALL record, per model call, the business, the model, the input and output token counts, and the provider.
2. THE platform SHALL record, per outbound channel message, the business and the estimated channel cost.
3. THE platform SHALL expose per-business totals for a period.
4. THE platform SHALL distinguish a business's usage from the platform's total.
5. THE platform SHALL report the number of businesses whose usage exceeds a configured allowance, without preventing service.

### Requirement 10: Isolation Is Tested, Not Assumed

**User Story:** As the operator, I want proof that tenants cannot see each other, so that I can onboard a second business without fearing the first.

#### Acceptance Criteria

1. THE platform SHALL provide a test that processes a job for Business A, then a job for Business B, then a job for Business A again, on the same worker connection.
2. That test SHALL assert that each job read and wrote only its own business's rows.
3. That test SHALL assert that the Tenant_Context did not survive from one job to the next.
4. THE platform SHALL provide a test that, authenticated as Business A, requests a resource belonging to Business B and receives the same response as for a non-existent resource.
5. THE platform SHALL provide a test that a query against an RLS-protected table with no Tenant_Context returns no rows.
6. THE platform SHALL provide tests for each of the five boundaries asserting that resolution selects the correct business.
7. THE platform SHALL provide a test asserting that an unknown `phone_number_id` and an unknown payment reference each fail closed.
8. THE platform SHALL NOT consider tenant isolation complete while any of these tests is absent.
