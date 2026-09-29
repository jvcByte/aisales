# Data protection notes

**This is an engineering note, not legal advice.** It records what the system
does with personal data so that someone qualified can check it before the pilot
takes real customers. Nobody should read it as a compliance sign-off.

## What is stored

| Data | Where | Why |
|---|---|---|
| Customer phone number (E.164) | `customers.phone_e164` | It is the identity. One customer, one thread. |
| The raw number as received | `customers.phone_raw` | Debugging a normalisation bug. |
| Every message, in and out | `messages.body` | The agent needs the conversation; the owner needs the record. |
| Notes the agent records | `customers.notes` | Budget, size, preferences — so a customer is not asked twice. |
| Orders, payments, leads | `orders`, `payments`, `leads` | The business's own records. |
| What the AI did, and why | `audit_log` | Resolving a dispute about what was promised. |

## Lawful basis

The arguable basis is **performance of a contract or steps towards one**
(NDPA 2023 s.25(1)(b)): the customer messaged the business first, asking to
buy, and the processing is what answering them requires. Legitimate interests
is the fallback for anything beyond that.

This should be **written down and reviewed**, because the argument is strongest
for the conversation itself and weakest for the derived material — lead scoring,
intent classification, and the daily digest are analytics about a person, not
the reply they asked for.

## Cross-border transfer

This is the part that needs the most scrutiny. The model providers —
Google (Gemini), Groq, Hugging Face, OpenRouter — process outside Nigeria, so
**every customer message leaves the country** when it is sent to a model.

Consequences worth acting on:

- It belongs in the privacy notice the agent can send. The mechanism exists:
  set `settings.privacy_notice`.
- It argues for redacting anything the model does not need. The agent is sent
  the phone number in its state block; it does not need it to sell fabric.
- Which provider answers depends on quota, so a message may reach any of four
  companies on any given turn. That is hard to describe accurately in a notice,
  and it is a reason to pin one provider for production.

## Retention

**This is the decision that is hardest to retrofit**, which is why it exists as
code rather than as an intention:

```python
from aisales.db import purge_conversations_before
purge_conversations_before(dsn, cutoff)
```

Nothing schedules it. Leaving "keep everything forever" as the default is a
choice, and it should be a deliberate one. A defensible starting point is
12 months for conversations and indefinite for orders — an order is a financial
record with its own retention expectations, and deleting it would break the
business's books rather than protect anyone.

## What the agent may say

The agent must never improvise a data-protection policy. It may send
`settings.privacy_notice` verbatim and otherwise says it will check with the
owner. Inventing a policy about someone's data creates a commitment the
business did not make.

## Meta's terms

WhatsApp's Business Terms restrict using message data to train models without
consent. Nothing here does, but this matters before promising any fine-tuning
on the pilot's conversations — the transcripts are not ours to train on.

## Registration

The Nigeria Data Protection Commission requires registration for data
controllers of major importance. A one-business pilot is very unlikely to meet
that threshold; a platform serving many businesses is a different question, and
the answer should be established before the second business is onboarded.
