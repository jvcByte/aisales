"""Paystack, behind a Protocol.

Fake first, deliberately. The pilot business needs CAC registration and a
settlement account before it can take a naira, which is days to weeks outside
our control -- so the order and payment loop is built and tested against a fake
that signs with the *real* algorithm. A bug in signature verification therefore
fails a test instead of the test stepping around it.

Amounts are integer kobo throughout. Paystack transmits kobo and `data.amount`
arrives as kobo, so nothing here divides by 100: no float ever touches a
payment amount, and `12499.999999999998` cannot be requested.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

from aisales import agent, db

API_ROOT = "https://api.paystack.co"


class PaystackError(RuntimeError):
    def __init__(self, message: str, *, reason: str = "error"):
        super().__init__(message)
        self.reason = reason


def new_reference(prefix: str = "AIS") -> str:
    """A globally unique reference.

    Unique across the platform, not just the business, because a webhook
    arrives carrying only this string -- there is no business id on it to scope
    by. Paystack's own references are globally unique, so matching that keeps
    one constraint doing one job.
    """
    return f"{prefix}-{secrets.token_hex(8).upper()}"


def sign(secret: str, raw_body: bytes) -> str:
    """The signature Paystack puts in `x-paystack-signature`.

    SHA-512. Meta signs with SHA-256 over the same idea, and the two adapters
    look alike enough that a copy between them produces a verifier which either
    always passes or never does.
    """
    return hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()


def verify_webhook(secret: str, raw_body: bytes, signature: str | None) -> bool:
    """Whether a callback genuinely came from Paystack.

    Compared with `compare_digest`. A plain `==` on a hex digest leaks how much
    of a guess was correct through timing, which is a slow attack but a real
    one against a public endpoint.
    """
    if not secret or not signature:
        return False
    return hmac.compare_digest(sign(secret, raw_body), signature)


class PaystackClient(Protocol):
    def initialise(self, *, email: str, amount_kobo: int, reference: str,
                   metadata: dict) -> str:
        """Start a transaction. Returns the URL to send the customer."""


class FakePaystack:
    """A Paystack that never touches the network.

    It mints nothing and stores nothing: the reference is supplied by the
    caller, so the fake cannot diverge from the real one on the only field that
    matters for idempotency.
    """

    name = "fake"

    def __init__(self, secret: str = "sk_test_fake"):
        self.secret = secret
        self.calls: list[dict[str, Any]] = []

    def initialise(self, *, email: str, amount_kobo: int, reference: str,
                   metadata: dict) -> str:
        self.calls.append({"email": email, "amount_kobo": amount_kobo,
                           "reference": reference, "metadata": metadata})
        return f"https://checkout.paystack.com/{reference.lower()}"

    def callback(self, *, reference: str, amount_kobo: int,
                 event: str = "charge.success") -> tuple[bytes, str]:
        """A signed webhook body and its signature, as Paystack would send it.

        Signed with the real secret using the real algorithm, so the handler
        under test verifies rather than being told to trust.
        """
        body = json.dumps({
            "event": event,
            "data": {"reference": reference, "amount": amount_kobo,
                     "status": "success", "channel": "card",
                     "currency": "NGN"},
        }).encode()
        return body, sign(self.secret, body)


class LivePaystack:
    """The real thing. ~40 lines because `verify_webhook` is already shared."""

    name = "paystack"

    def __init__(self, secret: str, *, timeout_s: float = 30.0):
        if not secret:
            raise PaystackError("a Paystack secret key is required")
        self._secret = secret
        self._timeout = timeout_s

    def initialise(self, *, email: str, amount_kobo: int, reference: str,
                   metadata: dict) -> str:
        payload = json.dumps({
            "email": email,
            "amount": amount_kobo,      # kobo, already. No conversion.
            "reference": reference,
            "metadata": metadata,
            "currency": "NGN",
        }).encode()
        request = urllib.request.Request(
            f"{API_ROOT}/transaction/initialize", data=payload, method="POST",
            headers={"Authorization": f"Bearer {self._secret}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise PaystackError(f"Paystack refused the request ({exc.code}): {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PaystackError(f"could not reach Paystack: {exc}",
                                reason="busy") from exc

        if not body.get("status"):
            raise PaystackError(f"Paystack returned an error: {str(body)[:200]}")
        return body["data"]["authorization_url"]


@dataclass(frozen=True)
class CallbackOutcome:
    handled: bool
    idempotent: bool = False
    reason: str = ""
    order_id: str | None = None
    conversation_id: str | None = None
    business_id: str | None = None


#: Events we act on. Anything else is acknowledged and ignored -- see
#: `apply_callback`, which checks this before touching the database.
CHARGE_SUCCESS = "charge.success"


def apply_callback(conn, dsn, *, event: str, data: dict) -> CallbackOutcome:
    """Record a verified Paystack callback. Safe to call twice.

    `conn` may be unscoped: a callback arrives with no session, so the only
    thing that names its tenant is the reference on the payment row. Finding
    that business is the one deliberate cross-tenant read here, in the same
    class as claiming a job, and it returns a uuid and nothing else. Every
    effect after it runs inside that business's tenant on a connection of its
    own, so the policies apply to the write and not merely to the read.

    Three things this does that the existing local precedent does not, each of
    which is a real bug there rather than a difference of taste:

    1. **The event type is checked first.** Nothing touches the database until
       we know the event is one we handle.

    2. **The claim and the validation are one statement.** The precedent does
       SELECT, check the status, then UPDATE. Two concurrent deliveries of the
       same callback both pass the check and both run the downstream effects,
       so an escrow gets funded twice. Here `where status = 'pending'` is the
       guard and Postgres row-locks it: the second delivery re-evaluates the
       predicate after the lock releases, matches nothing, and is correctly
       reported as a duplicate.

    3. **The amount is compared to the order.** The precedent reads the amount
       from its own ledger row and never looks at what actually arrived, so a
       callback for ₦500 against a ₦12,500 order marks it paid. Here the
       comparison is inside the UPDATE: a wrong amount atomically marks the
       payment failed instead of successful.
    """
    if event != CHARGE_SUCCESS:
        return CallbackOutcome(handled=False, reason=f"ignored event {event!r}")

    reference = str(data.get("reference") or "")
    if not reference:
        return CallbackOutcome(handled=False, reason="callback carried no reference")
    try:
        paid_kobo = int(data.get("amount"))
    except (TypeError, ValueError):
        return CallbackOutcome(handled=False, reason="callback carried no amount")

    # Boundary: which business does this reference belong to?
    business_id = db.resolve_payment_tenant(conn, reference)
    if business_id is None:
        # Acknowledged, because Paystack must stop retrying a delivery we can
        # never apply. Not idempotent -- nothing was ever processed.
        return CallbackOutcome(handled=False, reason="unknown reference")

    with db.tenant(dsn, business_id) as scoped:
        return _apply_scoped(scoped, reference=reference, paid_kobo=paid_kobo, data=data)


def _apply_scoped(conn, *, reference: str, paid_kobo: int, data: dict) -> CallbackOutcome:
    """The effects, inside the paying business's tenant."""
    row = conn.execute(
        """
        update payments
           set status = case when amount_kobo = %(paid)s
                             then 'success'::payment_status
                             else 'failed'::payment_status end,
               raw = %(raw)s,
               channel = %(channel)s,
               verified_at = now()
         where paystack_reference = %(reference)s
           and status = 'pending'
        returning id, order_id, business_id, amount_kobo, status
        """,
        {"paid": paid_kobo, "reference": reference,
         "raw": psycopg.types.json.Json(data),
         "channel": data.get("channel")},
    ).fetchone()

    if row is None:
        # The reference resolved to this business a moment ago and the
        # predicate is `status = 'pending'`, so the only way to match nothing
        # is that another delivery already applied it.
        return CallbackOutcome(handled=False, idempotent=True, reason="already processed")

    business_id = str(row["business_id"])
    if row["status"] == "failed":
        conversation = _conversation_of(conn, row["order_id"])
        agent.audit(conn, business_id=business_id, conversation_id=conversation,
                    actor="system", action="payment_amount_mismatch",
                    detail={"reference": reference, "expected_kobo": row["amount_kobo"],
                            "paid_kobo": paid_kobo})
        if conversation:
            conn.execute(
                """
                update conversations set needs_attention = true,
                       attention_reason = 'payment amount did not match the order'
                 where id = %s
                """,
                (conversation,),
            )
        return CallbackOutcome(handled=False, reason="amount did not match the order",
                               order_id=str(row["order_id"]), business_id=business_id,
                               conversation_id=conversation)

    conversation = _conversation_of(conn, row["order_id"])
    conn.execute(
        "update orders set status = 'paid', paid_at = now(), updated_at = now() "
        "where id = %s",
        (row["order_id"],),
    )
    agent.audit(conn, business_id=business_id, conversation_id=conversation,
                actor="system", action="payment_confirmed",
                detail={"reference": reference, "amount_kobo": paid_kobo,
                        "order_id": str(row["order_id"])})
    return CallbackOutcome(handled=True, order_id=str(row["order_id"]),
                           business_id=business_id, conversation_id=conversation)


def _conversation_of(conn, order_id) -> str | None:
    row = conn.execute(
        "select conversation_id from orders where id = %s", (order_id,)
    ).fetchone()
    return str(row["conversation_id"]) if row and row["conversation_id"] else None


def default_paystack() -> PaystackClient:
    """The real client when a key is configured, and a clear failure when not.

    Deliberately not a silent fallback to the fake: an order created against a
    fake payment link looks completely normal and takes no money, which is the
    worst kind of bug to find in production.
    """
    if os.environ.get("AISALES_FAKES") == "1":
        return FakePaystack()
    secret = os.environ.get("PAYSTACK_SECRET_KEY", "")
    if not secret:
        raise PaystackError(
            "no Paystack key is configured. Set PAYSTACK_SECRET_KEY in .env. "
            "Test keys work before business verification, so this can be "
            "sk_ and still exercise the real API."
        )
    return LivePaystack(secret)


def for_business(conn, *, business_id: str, fallback: PaystackClient | None):
    """The payment account this business is paid into.

    The same shape as `channels.for_business`, and for the same reason: with
    one platform key, every business's customers pay into the platform's
    account and the businesses are left reconciling by hand. The account code
    comes from the business's own integration row, the secret key from its own
    encrypted row, and a business with neither gets `fallback`.

    Deliberately no cross-business fallback: a business with no Paystack
    account must not silently take money through somebody else's. It gets
    `fallback`, which for the worker is `None`, and `create_payment_link`
    reports that payments are not configured for this business.
    """
    from aisales import db, secrets

    row = conn.execute(
        """
        select external_account_id, status from business_integrations
         where business_id = %s and provider = 'paystack' and status <> 'revoked'
         order by created_at limit 1
        """,
        (business_id,),
    ).fetchone()
    if row is None:
        return fallback

    secret = secrets.get(conn, business_id=business_id, provider="paystack",
                         secret_type="secret_key")
    if not secret:
        return fallback
    return LivePaystack(secret)
