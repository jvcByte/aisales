# Requirements Document

## Introduction

The AI Sales Employee is a WhatsApp-first AI agent for Nigerian businesses. It answers customer
enquiries 24/7, qualifies leads, follows up on interest, sends payment links, creates orders and
reports business intelligence to the owner.

This specification covers the **V1 prototype wedge** only:

> Customer conversation → Lead qualification → Follow-up → Payment → Order → Business intelligence

Explicitly out of scope for this specification: inventory management, delivery-fee logistics,
customer segmentation, sales forecasting, the marketing assistant, voice AI, and lost-sale
recovery beyond what automatic follow-up already provides.

The system is built for **one pilot business**. Tenant isolation is present in the schema from the
first migration so a second business is a configuration change rather than a migration, but no
self-serve onboarding, subscription billing or control plane is in scope.

**The governing constraint.** Vision §12 requires that the agent never invent prices, availability
or policies, and never grant unauthorised discounts or refunds. A sales agent that invents a price
is worse than no agent: it creates a dispute the business must honour or lose a customer over. In
this specification that constraint is enforced by mechanism (see Requirement 4) rather than by
instruction to a language model, and it takes precedence over conversational fluency wherever the
two conflict.

**Platform constraints that shape the requirements.** Meta permits free-form messages to a customer
only within 24 hours of that customer's last inbound message; outside that window only a
pre-approved template may be sent (Requirement 7). Payment confirmation must come from a payment
provider's own records, never from a customer's assertion or screenshot (Requirement 8).

---

## Glossary

- **Channel_Gateway**: The inbound/outbound boundary. Normalises deliveries from any messaging
  platform into `Inbound` records, and delivers `Outbound` messages. Owns signature verification
  and message-level de-duplication.
- **Channel**: A concrete adapter behind the Channel_Gateway. V1 ships `SimulatorChannel` and
  `MetaCloudChannel`.
- **Agent_Runtime**: The component that runs one conversation turn: assembles context, calls the
  model, dispatches tool calls, applies the Guard, and decides whether to send or escalate.
- **Guard**: The component that decides whether a drafted reply may be sent. Enforces Requirement 4.
- **Intent_Classifier**: The component that maps a customer utterance to one member of a closed
  intent set, capturing Nigerian commercial idiom (Requirement 5).
- **Catalogue_Service**: Owns products, prices, variants and stock.
- **Order_Service**: Owns orders and their line items.
- **Payment_Service**: Owns payment initiation and confirmation against Paystack.
- **FollowUp_Scheduler**: Owns scheduled follow-ups and their business-controlled policy.
- **Takeover_Service**: Owns the AI/human ownership state of a conversation.
- **Insights_Service**: Owns the daily business-intelligence digest.
- **Audit_Log**: The append-only record of consequential actions.
- **Job_Queue**: The Postgres-backed job queue. There is no Redis in this deployment.
- **Simulator**: The developer-facing page that stands in for WhatsApp while no API access exists.
- **Business_Config**: The per-business settings blob: tone, follow-up policy, escalation triggers,
  confidence threshold, and approved policy text.
- **Approved_Facts**: Business policies and figures the owner has stated (delivery terms, returns,
  payment instructions, delivery fee), supplied to the model as fixed context.
- **Tool**: A capability the model may invoke: a named function with a JSON schema, dispatched
  against real business data.
- **Tool_Result**: The outcome of a Tool, including `money_kobo` — the currency amounts that result
  authorises the agent to state.
- **Allowed_Set**: The union of every `money_kobo` from Tool_Results in the current turn and every
  currency figure loaded from the database during that turn. The sole authority for what the agent
  may say about money.
- **Turn**: One request-response cycle of the Agent_Runtime, from an inbound message to at most one
  outbound reply.
- **Money_Fact**: A currency amount expressed as an integer number of kobo.
- **Kobo**: One hundredth of a Naira. The only unit in which money is stored or transmitted.
- **E.164**: The international telephone number format, e.g. `+2348031234567`.
- **Lead_Tier**: One of `hot`, `warm`, `cold`.
- **Attention_Flag**: The state meaning a staff member should look at a conversation. Distinct from
  ownership (Requirement 10).
- **Ownership**: Who is permitted to reply to a conversation: `ai`, `human`, or `closed`.
- **Service_Window**: The 24 hours following a customer's last inbound message, within which Meta
  permits free-form outbound messages.
- **Quiet_Hours**: The per-business local-time span during which follow-ups must not be sent.
- **NDPA**: Nigeria Data Protection Act 2023.

---

## Requirements

### Requirement 1: Channel Intake and Customer Identity

**User Story:** As a business owner, I want every customer message to arrive exactly once and be
attributed to the right customer, so that the AI never loses a message or answers the wrong person.

#### Acceptance Criteria

1. WHEN a delivery arrives at the Channel_Gateway, THE Channel_Gateway SHALL verify the delivery's
   authenticity before parsing it, and SHALL reject an unverified delivery without processing it.
2. THE Channel_Gateway SHALL normalise every inbound message into an `Inbound` record before any
   Agent_Runtime code observes it, so that no component outside the Channel_Gateway names a
   specific messaging platform.
3. WHEN a delivery contains no message (for example a delivery receipt or read receipt), THE
   Channel_Gateway SHALL acknowledge it and produce zero `Inbound` records.
4. WHEN an inbound message arrives whose platform message identifier has already been stored for
   that business, THE Channel_Gateway SHALL discard it and SHALL NOT enqueue a Turn.
5. THE Channel_Gateway SHALL derive customer identity by normalising the sender's telephone number
   to E.164 format.
6. IF a sender's telephone number cannot be normalised to a valid Nigerian number, THEN THE
   Channel_Gateway SHALL reject the message and SHALL NOT guess a country code.
7. FOR ALL valid Nigerian telephone number spellings, normalising then formatting SHALL produce the
   same E.164 value (round-trip property).
8. WHEN the Channel_Gateway acknowledges a webhook delivery, THE Channel_Gateway SHALL respond with
   success before any model invocation occurs, so that the platform does not retry a slow endpoint.
9. WHERE a channel requires a verification handshake, THE Channel_Gateway SHALL answer that
   handshake without requiring application state.

### Requirement 2: Catalogue and Approved Facts

**User Story:** As a business owner, I want the AI to sell from my real catalogue and my stated
policies, so that customers are quoted what I actually sell on the terms I actually offer.

#### Acceptance Criteria

1. THE Catalogue_Service SHALL store every product with a name, an integer kobo price, an
   availability state, a variant list, and a description.
2. WHEN a product's stock is tracked, THE Catalogue_Service SHALL represent "none remaining"
   distinctly from "not tracked", and the Agent_Runtime SHALL treat "not tracked" as permission to
   sell rather than as unavailability.
3. THE Agent_Runtime SHALL supply Approved_Facts to the model on every Turn as fixed context,
   including the business's delivery terms, returns policy, payment instructions and any delivery
   fee the owner has stated.
4. WHEN the Agent_Runtime builds a prompt, THE Agent_Runtime SHALL NOT include any currency figure
   from a prior Turn in the Allowed_Set.
5. WHEN a customer enquires about a product by name or description fragment, THE Catalogue_Service
   SHALL return matching products without requiring an exact name.
6. THE Catalogue_Service SHALL scope every product query to the requesting business.

### Requirement 3: The Agent Turn

**User Story:** As a customer, I want a useful answer to what I actually asked, so that I can decide
whether to buy without repeating myself or waiting for a human.

#### Acceptance Criteria

1. WHEN an inbound message is recorded, THE Agent_Runtime SHALL run a Turn unless Ownership is
   `human`, in which case the Agent_Runtime SHALL NOT reply.
2. THE Agent_Runtime SHALL expose the model a Tool set comprising product search, product lookup,
   order creation, payment-link creation, payment-status lookup, lead tagging, customer-note
   recording, follow-up scheduling, human escalation, and the option to send no reply.
3. WHEN the model requests a Tool, THE Agent_Runtime SHALL validate the request against that Tool's
   schema before dispatch, and SHALL NOT dispatch an invalid request.
4. WHILE the model requests Tools, THE Agent_Runtime SHALL dispatch each and return its result to
   the model, up to a bounded number of iterations.
5. IF the model exceeds the iteration bound, THEN THE Agent_Runtime SHALL escalate the conversation
   to a human rather than continue.
6. THE Agent_Runtime SHALL load the current open order, its reference and total, the current lead
   tier, any scheduled follow-up, and recorded customer notes from the database on every Turn.
7. WHEN a conversation's stored messages exceed the prompt window, THE Agent_Runtime SHALL
   summarise the excess into a rolling summary and SHALL record how far it has summarised.
8. WHEN the model selects the no-reply Tool, THE Agent_Runtime SHALL send nothing and SHALL NOT
   record an outbound message.
9. IF every configured model provider fails, THEN THE Agent_Runtime SHALL leave the reply queued for
   retry, SHALL escalate the conversation to a human, and SHALL send the customer a fixed
   non-model acknowledgement.
10. THE Agent_Runtime SHALL complete the outbound send before recording the outbound message, so
    that a failed send never appears in the conversation history as a sent message.

### Requirement 4: Price and Policy Fidelity

**User Story:** As a business owner, I want the AI to be incapable of quoting a price I did not set
or granting a discount I did not authorise, so that I can let it talk to customers without
supervising every message.

#### Acceptance Criteria

1. WHEN a reply is drafted, THE Guard SHALL extract every currency-shaped token from it, including
   amounts written with a Naira symbol, with the word "naira", with a `k` suffix, and as a bare
   number of four or more digits.
2. THE Guard SHALL permit a currency amount to be sent **if and only if** that amount is a member of
   the Allowed_Set for the current Turn.
3. IF a drafted reply contains a currency amount that is not a member of the Allowed_Set, THEN THE
   Guard SHALL block the reply.
4. IF a drafted reply asserts a discount, a price reduction, a refund, a waiver, free delivery, or a
   final-price concession, and no Tool_Result in the current Turn authorises it, THEN THE Guard
   SHALL block the reply.
5. IF a drafted reply asserts that a product is available and no Tool_Result in the current Turn
   reports a tracked stock quantity greater than zero, THEN THE Guard SHALL block the reply.
6. WHEN the Guard blocks a reply, THE Agent_Runtime SHALL NOT record the blocked reply as a message.
7. WHEN the Guard blocks a reply, THE Agent_Runtime SHALL re-prompt the model at most once with the
   Turn's Tool_Results restated, and IF the second draft is also blocked, THEN THE Agent_Runtime
   SHALL escalate the conversation to a human.
8. WHEN the Guard blocks a reply, THE Agent_Runtime SHALL record the block, the offending tokens and
   the Allowed_Set in the Audit_Log.
9. THE Guard SHALL NOT block a currency-shaped token that appears inside a URL, inside an order
   reference, inside a clock time, or adjacent to a quantity, size or colour marker.
10. FOR ALL replies composed only of amounts drawn from the Allowed_Set, the Guard SHALL permit the
    reply (no-false-positive property).

### Requirement 5: Nigerian Commerce Intent Classification

**User Story:** As a Nigerian customer, I want the AI to understand what I mean rather than what I
literally typed, so that I do not have to translate myself to be served.

#### Acceptance Criteria

1. WHEN an inbound message is received, THE Intent_Classifier SHALL classify it as exactly one
   member of a closed intent set.
2. THE Intent_Classifier SHALL classify "How much last?" and "Last price?" as a discount request
   rather than as an enquiry about time.
3. THE Intent_Classifier SHALL classify "Reduce am" and equivalent imperatives as a discount
   request.
4. THE Intent_Classifier SHALL classify a request for account details as payment intent.
5. THE Intent_Classifier SHALL classify an assertion that payment has been made as a payment claim,
   and THE Agent_Runtime SHALL NOT treat a payment claim as evidence of payment.
6. THE Intent_Classifier SHALL classify an intention to return later as a deferral, and THE
   Agent_Runtime SHALL treat a deferral as a reason to schedule a follow-up.
7. IF classification fails or is indeterminate, THEN THE Intent_Classifier SHALL return an unknown
   intent with zero confidence rather than raise an error.
8. THE Agent_Runtime SHALL record the classified intent with the message.
9. THE system SHALL provide a golden set of at least 25 real Nigerian commercial utterances with
   their expected intents, executable without network access or credentials.

### Requirement 6: Lead Qualification

**User Story:** As a business owner, I want to know which conversations are worth my attention, so
that I spend my time on customers who are ready to buy.

#### Acceptance Criteria

1. WHEN a Turn indicates buying intent, THE Agent_Runtime SHALL record or update a lead for that
   customer with a tier of `hot`, `warm`, or `cold`.
2. THE Agent_Runtime SHALL record the reason for the assigned tier in the lead.
3. WHILE a customer has an open lead, THE Agent_Runtime SHALL update that lead rather than create an
   additional open lead.
4. WHEN a customer's intent strengthens, THE Agent_Runtime SHALL raise the lead tier and SHALL NOT
   lower it within the same open lead.
5. THE Agent_Runtime SHALL record the customer's stated intent and any product they named with the
   lead.

### Requirement 7: Follow-Up

**User Story:** As a business owner, I want customers who said they would come back to actually be
followed up, on a schedule and in a tone I control.

#### Acceptance Criteria

1. WHEN a Turn records a deferral, THE FollowUp_Scheduler SHALL schedule a follow-up for that
   conversation.
2. THE FollowUp_Scheduler SHALL read timing, frequency cap and tone from Business_Config.
3. THE FollowUp_Scheduler SHALL store the scheduled time as the job's earliest-run time, so that no
   polling loop or sleeping process is required to fire it.
4. IF a scheduled follow-up's due time would fall outside the Service_Window, THEN THE
   FollowUp_Scheduler SHALL skip it and record why.
5. IF a scheduled follow-up's due time would fall within Quiet_Hours, THEN THE FollowUp_Scheduler
   SHALL defer it to the end of Quiet_Hours computed in the business's own timezone.
6. WHEN a customer sends a message, THE FollowUp_Scheduler SHALL cancel any pending follow-up for
   that conversation.
7. WHILE a conversation's Ownership is `human`, THE FollowUp_Scheduler SHALL NOT send a follow-up to
   it.
8. THE FollowUp_Scheduler SHALL send at most the configured maximum number of follow-ups per
   conversation.
9. WHEN a follow-up is sent, THE FollowUp_Scheduler SHALL apply the Guard to it exactly as to a
   normal Turn.
10. IF a follow-up is due while a Turn is in progress on the same conversation, THEN THE
    FollowUp_Scheduler SHALL re-queue the follow-up rather than send it concurrently.
11. THE FollowUp_Scheduler SHALL permit at most one pending follow-up per conversation.

### Requirement 8: Payment

**User Story:** As a business owner, I want payments confirmed by the payment provider before I
treat an order as paid, so that no one can talk my AI into releasing goods for free.

#### Acceptance Criteria

1. WHEN a customer is ready to pay, THE Payment_Service SHALL create a payment record and obtain a
   payment link from Paystack for the order's total.
2. THE Payment_Service SHALL transmit and store all amounts as integer kobo.
3. WHEN a payment callback is received, THE Payment_Service SHALL verify the provider's signature
   over the raw request body before acting on it.
4. WHEN a payment callback is received, THE Payment_Service SHALL record the payment in a single
   atomic operation that cannot process the same provider reference twice.
5. IF a payment callback's amount does not equal the order total, THEN THE Payment_Service SHALL
   reject it, SHALL NOT mark the order paid, and SHALL escalate the conversation to a human.
6. WHEN a callback event type is unrecognised, THE Payment_Service SHALL acknowledge it and SHALL
   NOT modify any record.
7. WHEN a duplicate callback is received for an already-recorded payment, THE Payment_Service SHALL
   acknowledge it with success and SHALL NOT modify any record.
8. WHEN a payment is confirmed, THE Payment_Service SHALL transition the order to `paid` and record
   the confirmation in the Audit_Log.
9. THE Payment_Service SHALL store the provider's own payload for the transaction.
10. THE Agent_Runtime SHALL determine payment status only by querying stored provider records, and
    SHALL NOT accept a customer's statement or image as evidence of payment.

### Requirement 9: Order Management

**User Story:** As a business owner, I want orders created from confirmed conversations with the
prices that were quoted, so that my records match what the customer agreed to.

#### Acceptance Criteria

1. WHEN the model creates an order, THE Order_Service SHALL create the order and its line items in
   a single transaction.
2. THE Order_Service SHALL copy each product's name and unit price into the line item rather than
   referencing the product, so that a later catalogue change does not alter a placed order.
3. THE Order_Service SHALL compute the order total from its line items at creation and store it as
   integer kobo.
4. THE Order_Service SHALL assign each order a human-readable reference unique within the business.
5. THE Order_Service SHALL transition an order through its permitted statuses only, and SHALL reject
   an unpermitted transition.
6. WHEN an order is created or its status changes, THE Order_Service SHALL record the change in the
   Audit_Log.

### Requirement 10: Human Takeover

**User Story:** As a business owner or staff member, I want to take over a conversation instantly
and hand it back when I am done, without the AI talking over me.

#### Acceptance Criteria

1. THE Takeover_Service SHALL track Attention_Flag and Ownership as independent facts, such that a
   conversation can require attention while the AI is still permitted to answer it.
2. WHEN the Guard blocks a reply, or a customer expresses a complaint, a refund demand, a discount
   demand, or a payment or delivery dispute, THE Takeover_Service SHALL set the Attention_Flag.
3. WHEN a handover-severity escalation occurs, THE Takeover_Service SHALL set Ownership to `human`,
   which SHALL cause the Agent_Runtime to send nothing further on that conversation.
4. WHEN a staff member takes over a conversation, THE Takeover_Service SHALL record who took over
   and when, and SHALL clear the Attention_Flag.
5. WHILE Ownership is `human`, THE Agent_Runtime SHALL record inbound customer messages and SHALL
   NOT enqueue a Turn for them.
6. WHILE Ownership is `human`, THE Agent_Runtime SHALL record a suppressed-turn entry in the
   Audit_Log for each inbound message.
7. WHEN a staff member sends a message, THE Agent_Runtime SHALL record it with staff authorship.
8. WHEN Ownership returns to `ai`, THE Agent_Runtime SHALL include staff messages in the model's
   history, so that the agent does not contradict what the staff member told the customer.
9. WHEN Ownership returns to `ai` and a customer message arrived during the human period, THE
   Agent_Runtime SHALL run exactly one Turn to answer it.
10. THE Takeover_Service SHALL record every ownership transition in the Audit_Log.

### Requirement 11: Business Intelligence

**User Story:** As a business owner, I want to understand what happened in my business today and why
customers did not buy, so that I can act on it.

#### Acceptance Criteria

1. THE Insights_Service SHALL report the total collected, the number of conversations, the number of
   orders, and the number of new leads for a given day.
2. THE Insights_Service SHALL report the most frequently classified customer intents for a period.
3. THE Insights_Service SHALL group un-converted leads by their recorded reason.
4. THE Insights_Service SHALL report the number of replies the Guard blocked for a period.
5. THE Insights_Service SHALL derive every figure from stored records, and SHALL NOT derive any
   figure from model-generated text.
6. WHEN the Insights_Service presents a comparison, THE Insights_Service SHALL name the comparison
   period explicitly.

### Requirement 12: Audit Trail

**User Story:** As a business owner, I want a record of what the AI did and why, so that I can
resolve a dispute about what was promised.

#### Acceptance Criteria

1. THE Audit_Log SHALL be append-only.
2. WHEN a price is quoted, an order created, a payment link sent, a payment confirmed, a lead
   tagged, a follow-up sent, a conversation escalated, taken over or handed back, or a reply
   blocked, THE Audit_Log SHALL record the action with its conversation, actor and timestamp.
3. WHEN a Tool is dispatched, THE Audit_Log SHALL record the Tool's name, arguments and result.
4. WHEN the Guard blocks a reply, THE Audit_Log SHALL record the allowed set and the offending
   tokens.
5. THE Audit_Log SHALL record the model that produced a given reply.
6. WHEN an action is recorded, THE Audit_Log SHALL associate it with the business that owns the
   conversation.

### Requirement 13: Tenant Isolation and Data Protection

**User Story:** As the operator, I want every record scoped to its business and customer data
handled lawfully, so that a second business can be onboarded without cross-contamination.

#### Acceptance Criteria

1. THE system SHALL include a business identifier on every business-scoped table from the first
   migration.
2. THE system SHALL scope every read and write of business data to the requesting business.
3. THE system SHALL NOT return records belonging to one business to another.
4. WHEN a conversation is purged, THE system SHALL delete its messages and associated personal data.
5. THE system SHALL record the cost of each outbound channel message, so that per-conversation cost
   is measurable.

### Requirement 14: Job Queue and Scheduling

**User Story:** As the operator, I want reliable asynchronous processing without adding
infrastructure, so that a slow or failing model call never blocks message intake.

#### Acceptance Criteria

1. THE Job_Queue SHALL store jobs in the same PostgreSQL database as application data, and SHALL NOT
   require a separate broker.
2. WHEN a worker claims a job, THE Job_Queue SHALL guarantee that no other worker can claim the same
   job concurrently.
3. WHEN a worker claims a job, THE Job_Queue SHALL grant a lease and SHALL permit the worker to
   extend it.
4. IF a worker fails to complete a job before its lease expires, THEN THE Job_Queue SHALL make the
   job claimable again.
5. WHEN a job fails, THE Job_Queue SHALL retry it with increasing delay up to a maximum attempt
   count, after which THE Job_Queue SHALL mark it failed.
6. WHEN a job's earliest-run time is in the future, THE Job_Queue SHALL NOT make it claimable before
   that time.
7. WHEN a job is submitted with an idempotency key that already exists, THE Job_Queue SHALL NOT
   create a second job.
8. WHERE a conversation already has a queued or running agent Turn, THE Job_Queue SHALL NOT accept a
   second agent Turn for that conversation.

### Requirement 15: Simulator and Offline Verification

**User Story:** As the developer, I want to exercise the whole system without WhatsApp access and
without credentials, so that I can build and test before external approvals exist.

#### Acceptance Criteria

1. THE Simulator SHALL present a page on which a person can send messages as a customer and see the
   agent's replies.
2. THE Simulator SHALL invoke the same Agent_Runtime entry point that the Job_Queue worker invokes.
3. THE Simulator SHALL display, for each Turn, the classified intent, each Tool call with its
   arguments and result, and the Guard's verdict including the Allowed_Set.
4. THE Simulator SHALL offer controls that produce a voice note, an image, a burst of consecutive
   messages, and a payment claim unaccompanied by any payment.
5. THE system SHALL permit every external dependency — messaging channel, language model,
   transcription and payment provider — to be replaced by a fake.
6. THE system SHALL provide a scripted model fake capable of emitting Tool calls, so that a full
   Turn is exercisable with no credentials.
7. THE payment fake SHALL sign its callbacks with the real signing algorithm and secret, so that
   the signature-verification path is exercised rather than bypassed.
8. THE system SHALL refuse to deliver a reply to a real customer while any dependency is a fake.
9. THE system SHALL provide an end-to-end script that seeds a business, conducts a sale, confirms
   payment, exercises duplicate and mismatched-amount callbacks, fires a follow-up, and verifies the
   reported total, exiting non-zero and naming the failing step on any failure.
