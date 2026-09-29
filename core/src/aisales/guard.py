"""What the agent is allowed to say about money, stock and discounts.

The invariant, in one sentence:

    A currency figure may appear in an outbound message if and only if the
    database produced that figure during this turn.

Enforced by mechanism rather than by instruction. The prompt is written as
though this module did not exist, and this module is written as though the
model were adversarial -- because a model that invents a price creates a
dispute the business must honour or lose a customer over, and no amount of
careful prompting makes that impossible.

Three rules, first failure wins. Rule 1 protects the customer's trust, rule 2
protects the business's money, rule 3 is the weakest and is marked as such.

`allowed` is rebuilt every turn and never carried over. A price from six turns
ago is stale, so requiring a fresh lookup on every quote is not an inefficiency
-- it is exactly the "never invent prices" promise, and it is why the rolling
summary cannot authorise a figure: the summary is prose, and prose is not in
the allowed set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------- patterns

# ₦12,500 / NGN 12,500 / N5k / ₦12.5k. The lookahead after a bare "N" keeps
# "No." and "Nike" out.
MARKED = re.compile(r"(?:₦|\bNGN\b|\bN(?=\s?\d))\s?([\d,]+(?:\.\d{1,2})?)\s?(k|thousand)?",
                    re.I)
# "12,500 naira"
NAIRA = re.compile(r"\b([\d,]+(?:\.\d{1,2})?)\s?(?:naira|₦)\b", re.I)
# "12.5k", "5K". The trailing \b keeps "5km" and "2k23" out.
K_SUFFIX = re.compile(r"\b(\d+(?:\.\d{1,2})?)\s?k\b", re.I)
# A bare amount: four or more digits, or a comma-grouped number of any size.
#
# The comma branch is not decoration. Without it "9,999" is invisible -- it is
# not four consecutive digits -- so every four-figure price under ₦10,000
# written the way prices are actually written would pass ungrounded, which is
# exactly the hole this pattern exists to close. Three digits and below is a
# size, a quantity, or a house number.
BARE = re.compile(r"(?<![\w(.-])(\d{1,3}(?:,\d{3})+|\d{4,})(?![\w-])")

# Spans removed before extraction, so their digits never look like money.
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_ORDER_REF = re.compile(r"\b[A-Z]{2,}-\d+\b")
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
# A count or a measurement, not a price. Two shapes, because English puts the
# marker on either side: "qty 5000", and "5000 yards".
#
# Deliberately excludes "N x" -- "2 x 5000" means two at five thousand, so that
# 5000 IS a price and must be grounded.
_QUANTITY_BEFORE = re.compile(r"(?:qty|quantity|units?|no\.|#)\s*$", re.I)
# Directly after the number, with no "per" between: "5000 yards" is a quantity,
# while "12500 per yard" is a rate and stays grounded.
_UNIT_AFTER = re.compile(r"^\s*(?:pcs|pieces|units?|yards?|metres?|meters?|kg|"
                         r"litres?|liters?|rolls?|bundles?)\b", re.I)

# A telephone number is not a price, and an agent handing over a contact
# number is ordinary. Eleven bare digits would otherwise trip the bare-number
# rule every time.
_PHONE = re.compile(r"^(?:\+?234\d{7,}|0\d{9,})$")

# A year is not a price either, and "since 2015" is a normal thing for a shop
# to say. Only escaped when the prose marks it as a year, because a blanket
# escape on 1900-2099 would also swallow ₦2,000 -- a very common Nigerian
# price point written bare.
_YEAR_BEFORE = re.compile(r"(?:since|from|est\.?|©|copyright|year|until|till)\s*$", re.I)
_YEAR_AFTER = re.compile(r"^\s*(?:collection|edition|range|season|version)\b", re.I)


def _looks_like_a_year(token: str) -> bool:
    return len(token) == 4 and token.isdigit() and 1900 <= int(token) <= 2099

# Rule 2. Every one of these costs the business money if the model says it
# without authority.
_AUTHORITY = re.compile(
    r"\b(discount|reduce|reduction|% ?off|percent off|knock off|cheaper|"
    r"refund|waive[dr]?|free delivery|last price|final price|special price)\b", re.I)

# Rule 3. A claim that something is buyable.
#
# Kept narrow on purpose. Bare "e dey" and bare "we get" are the obvious
# phrasings to reach for and both are wrong: "e dey cost 5,000" means it costs
# 5,000, and "we get back to you" means we will reply. A rule that fires on
# those escalates correct replies, which is the failure mode that makes an
# agent feel broken.
_AVAILABILITY = re.compile(
    r"\b(in stock|available|we have it|we have them|we get am|we get it|"
    r"e dey available|dey in stock|still get am)\b", re.I)

# Rule 4. A claim that money has arrived. The vision's own example of what
# must never be believed: "Confirm payment through payment-provider records
# rather than screenshots alone." Customers say "I don pay" before the money
# lands, and sometimes it never does.
_PAYMENT = re.compile(
    # "payment confirmed" and "payment has been confirmed" are the same claim,
    # so the auxiliary verb is optional rather than required.
    r"\b(payment\s+(?:has\s+been\s+|is\s+|was\s+|don\s+)?"
    r"(?:received|confirmed|successful|complete[d]?|don\s+land)|"
    r"we(?:'ve| have) received your (?:payment|money)|"
    r"your (?:payment|money)\s+(?:has\s+|don\s+)?(?:landed|arrived|come in|enter)|"
    r"i (?:don\s+)?see your (?:payment|money)|"
    r"confirmed your payment)\b", re.I)

# Looked for in the run-up to a match, so a refusal is not read as an offer.
# "We do not offer discounts" contains the word discount and means its
# opposite; without this, echoing the business's own policy would escalate.
_NEGATION = re.compile(
    r"\b(no|not|never|cannot|can't|don't|doesn't|won't|without|no dey|we no|"
    r"isn't|aren't|unable|sorry)\b", re.I)

_NEGATION_LOOKBACK = 48


@dataclass(frozen=True)
class Allowed:
    """What this turn's database reads authorise the agent to say.

    Built by the turn from two sources and nothing else: the `money_kobo` of
    each tool result, and the currency figures loaded from SQL by the state
    block. Text -- a prompt, a summary, a customer's message -- never
    contributes to it.
    """

    money_kobo: frozenset[int] = frozenset()
    #: Some tool result this turn reported stock as a number above zero, or as
    #: NULL meaning "not tracked". Both mean the business is willing to sell.
    availability: bool = False
    #: Some tool result this turn authorised a discount, refund or waiver.
    #: Nothing in the wedge does, which is the point -- see rule 2.
    authority: bool = False
    #: A payment-provider record, read this turn, says the money arrived.
    #: Only `get_payment_status` can set this, and only from our own database.
    payment_confirmed: bool = False

    @property
    def naira(self) -> set[int]:
        return {k // 100 for k in self.money_kobo if k % 100 == 0}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    rule: str | None = None          # 'money' | 'authority' | 'availability'
    reason: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return not self.ok


def extract_money(text: str) -> list[tuple[str, int]]:
    """Every currency figure in *text*, as (token, kobo).

    Returns them in the order they appear, with overlapping matches dropped:
    "₦5k" matches both MARKED and K_SUFFIX and is one amount, not two.
    """
    scrubbed = _scrub(text)
    found: list[tuple[int, int, str, int]] = []   # (start, end, token, kobo)

    for pattern, has_suffix in ((MARKED, True), (NAIRA, False), (K_SUFFIX, True)):
        for match in pattern.finditer(scrubbed):
            start, end = match.span()
            token = match.group(0)
            suffix = ""
            if has_suffix and match.lastindex and match.lastindex >= 2:
                suffix = (match.group(2) or "")
            elif has_suffix and "k" in token.lower() and pattern is K_SUFFIX:
                suffix = "k"
            kobo = _to_kobo(match.group(1), suffix)
            if kobo is not None:
                found.append((start, end, token, kobo))

    for match in BARE.finditer(scrubbed):
        start, end = match.span()
        before = scrubbed[max(0, start - 16):start]
        after = scrubbed[end:end + 14]
        token = match.group(0)
        if _QUANTITY_BEFORE.search(before) or _UNIT_AFTER.search(after):
            continue
        if _PHONE.match(token.replace(" ", "")):
            continue
        if (_looks_like_a_year(token)
                and (_YEAR_BEFORE.search(before) or _YEAR_AFTER.search(after))):
            continue
        kobo = _to_kobo(match.group(1), "")
        if kobo is not None:
            found.append((start, end, token, kobo))

    found.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    accepted: list[tuple[int, int, str, int]] = []
    for start, end, token, kobo in found:
        if any(start < a_end and a_start < end for a_start, a_end, _, _ in accepted):
            continue
        accepted.append((start, end, token, kobo))
    return [(token, kobo) for _, _, token, kobo in accepted]


def check(reply: str, allowed: Allowed) -> Verdict:
    """Whether *reply* may be sent. First failing rule wins."""
    if not reply.strip():
        # Choosing silence is always permissible.
        return Verdict(ok=True)

    verdict = _check_money(reply, allowed)
    if verdict.blocked:
        return verdict

    verdict = _check_authority(reply, allowed)
    if verdict.blocked:
        return verdict

    verdict = _check_payment(reply, allowed)
    if verdict.blocked:
        return verdict

    return _check_availability(reply, allowed)


def _check_payment(reply: str, allowed: Allowed) -> Verdict:
    """A payment may be confirmed only by a provider record read this turn.

    The customer saying "I don pay" is not a record, a screenshot is not a
    record, and a figure the agent remembers is not a record. Nothing but
    `get_payment_status` sets `payment_confirmed`, and it reads our own
    database -- which the webhook wrote after verifying Paystack's signature.
    """
    if allowed.payment_confirmed:
        return Verdict(ok=True)
    for match in _PAYMENT.finditer(reply):
        if _negated(reply, match.start()):
            continue
        return Verdict(
            ok=False, rule="payment",
            reason="confirmed a payment that no provider record shows",
            detail={"phrase": match.group(0),
                    "context": _context(reply, match.start(), match.end())},
        )
    return Verdict(ok=True)


# ------------------------------------------------------------------- rules


# ponytail: rules 1 and 3 are threshold heuristics over regex, not a parser.
# Named ceilings, all three real:
#
#   * A bare three-digit price is not caught -- "It is 800". Lowering the floor
#     to three would fire on counts and sizes ("we have 500 in stock"), and
#     escalating correct replies is the failure that makes the agent look
#     broken. The gap is deliberate; prices this small are almost always
#     written with a marker, and markers are caught.
#   * Other four-digit identifiers that are not years, phones, quantities,
#     references or times would be blocked. A plot number is the likeliest.
#   * Rule 3's phrasing space is open. Only the phrasings listed above are
#     checked, and a novel way of claiming stock is not.
#
# Upgrade path, and it is strictly better than tightening these: have the model
# emit ₦{price:SKU} placeholders that the renderer substitutes from tool
# results. That makes the wrong thing *unrepresentable* rather than merely
# detected, and leaves this module as the backstop rather than the mechanism.
# Take it if guard_blocked on /insights is anything but near zero.
def _check_money(reply: str, allowed: Allowed) -> Verdict:
    ungrounded = [(token, kobo) for token, kobo in extract_money(reply)
                  if kobo not in allowed.money_kobo]
    if not ungrounded:
        return Verdict(ok=True)
    return Verdict(
        ok=False, rule="money",
        reason="a price that no lookup in this turn produced",
        detail={
            "tokens": [token for token, _ in ungrounded],
            "offending_kobo": [kobo for _, kobo in ungrounded],
            "allowed_kobo": sorted(allowed.money_kobo),
        },
    )


def _check_authority(reply: str, allowed: Allowed) -> Verdict:
    if allowed.authority:
        return Verdict(ok=True)
    for match in _AUTHORITY.finditer(reply):
        if _negated(reply, match.start()):
            continue
        return Verdict(
            ok=False, rule="authority",
            reason="offered a concession the agent cannot grant",
            detail={"phrase": match.group(0),
                    "context": _context(reply, match.start(), match.end())},
        )
    return Verdict(ok=True)


def _check_availability(reply: str, allowed: Allowed) -> Verdict:
    if allowed.availability:
        return Verdict(ok=True)
    for match in _AVAILABILITY.finditer(reply):
        if _negated(reply, match.start()):
            continue
        return Verdict(
            ok=False, rule="availability",
            reason="claimed something is buyable without checking stock",
            detail={"phrase": match.group(0),
                    "context": _context(reply, match.start(), match.end())},
        )
    return Verdict(ok=True)


# ----------------------------------------------------------------- helpers


def _scrub(text: str) -> str:
    """Blank the spans whose digits are never money.

    Replaced with spaces rather than removed so every remaining match keeps
    its original offset, which the quantity check depends on.
    """
    for pattern in (_URL, _ORDER_REF, _CLOCK):
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return text


def _to_kobo(amount: str, suffix: str) -> int | None:
    try:
        value = Decimal(amount.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    if suffix:
        value *= 1000
    if value <= 0:
        return None
    # Exact, because money is an integer count of kobo. A float here would
    # eventually compare 1250000.0000000002 against 1250000 and block a
    # correct reply.
    return int(value * 100)


def _negated(text: str, position: int) -> bool:
    """Whether a phrase at *position* sits under a negation.

    A heuristic, and deliberately a short-range one: "we do not offer
    discounts" is a refusal, while "no problem, I can offer a discount" is
    not, and only proximity distinguishes them.
    """
    window = text[max(0, position - _NEGATION_LOOKBACK):position]
    return bool(_NEGATION.search(window))


def _context(text: str, start: int, end: int, width: int = 40) -> str:
    return text[max(0, start - width):min(len(text), end + width)].strip()
