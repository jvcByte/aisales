"""The guard.

Tested in both directions on purpose. A guard that blocks everything passes a
must-block table perfectly and makes the product useless, so every must-block
case here has a matching must-allow case proving the rule is looking at the
right thing. The must-allow table is the more valuable half.
"""

from __future__ import annotations

import pytest

from aisales.guard import Allowed, check, extract_money

# The seeded catalogue, plus the delivery fee from the approved facts.
LACE_KOBO = 1_250_000        # ₦12,500
DELIVERY_KOBO = 250_000      # ₦2,500

GROUNDED = Allowed(money_kobo=frozenset({LACE_KOBO, DELIVERY_KOBO}),
                   availability=True)
NO_STOCK_CHECK = Allowed(money_kobo=frozenset({LACE_KOBO, DELIVERY_KOBO}),
                         availability=False)


# --------------------------------------------------------------- extraction


@pytest.mark.parametrize("text", [
    "₦12,500",
    "NGN 12,500",
    "12,500 naira",
    "₦12500",
    "12.5k",
    "N12.5k",
])
def test_every_spelling_reads_as_the_same_amount(text: str) -> None:
    """Nigerians write prices all four ways. A guard that handles only the
    Naira symbol handles almost none of them in practice."""
    assert extract_money(text) == [(text, LACE_KOBO)]


def test_a_bare_number_is_still_money() -> None:
    assert extract_money("It is 12500") == [("12500", LACE_KOBO)]


def test_k_suffix_is_one_amount_not_two() -> None:
    """'₦5k' matches both the marked pattern and the k-suffix pattern. It is
    one price, and counting it twice would double it."""
    assert extract_money("₦5k") == [("₦5k", 500_000)]


@pytest.mark.parametrize("text", [
    "https://paystack.com/pay/lace125000",   # a URL
    "Order ORD-12500 is ready",              # an order reference
    "We close at 18:00",                     # a clock time
    "We have 5000 yards in stock",           # a measured quantity
    "Send 2000 pieces",                      # a count
])
def test_digits_that_are_not_money(text: str) -> None:
    assert extract_money(text) == [], f"read a non-price as money in {text!r}"


def test_a_price_beside_a_quantity_is_still_a_price() -> None:
    """"2 x 5000" means two at five thousand. The 5000 is a price and must
    be grounded -- the quantity escape deliberately does not cover it."""
    assert extract_money("2 x 5000") == [("5000", 500_000)]


@pytest.mark.parametrize("text", [
    "Call us on 08031234567",
    "You can reach the shop on +2348031234567",
    "We've been in business since 2015",
    "Our 2026 collection just arrived",
    "Est. 1998, same family",
])
def test_telephone_numbers_and_years_are_not_prices(text: str) -> None:
    """Both were found by attacking the guard rather than by writing it.

    Eleven bare digits -- a phone number an agent would naturally hand over --
    and any year, each blocked a correct reply. A guard that escalates those
    looks broken and gives the owner no way to tell why.
    """
    assert extract_money(text) == [], f"read a non-price as money in {text!r}"


def test_the_three_digit_gap_is_deliberate_and_recorded() -> None:
    """The bare-number rule starts at four digits, so "It is 800" is not
    caught. Recording the gap as a test rather than leaving it implicit: it is
    a real hole, and it is the smaller of two evils.

    Dropping the floor to three closes it and opens a worse one -- it starts
    firing on counts, and an agent that escalates "we have 500 in stock" is
    indistinguishable from an agent that is broken.
    """
    assert extract_money("we have 500 in stock") == []
    assert extract_money("It is 800") == []                     # the gap
    assert extract_money("It is 1,800") == [("1,800", 180_000)]  # closed above it


def test_the_year_escape_does_not_excuse_a_price() -> None:
    """The escape is marker-based deliberately. Blanket-escaping 1900-2099
    would also swallow ₦2,000, which is a very common price point and exactly
    the kind of round number a model invents."""
    assert extract_money("It is 2000") == [("2000", 200_000)]
    assert extract_money("That one is 1,900") == [("1,900", 190_000)]


# ------------------------------------------------------- rule 1: the money


@pytest.mark.parametrize("reply", [
    "The lace is ₦15,000.",
    "I can do it for 9,999.",
    "It's 20k.",
    "That will be 99,999 naira.",
    "How about ₦1,000?",
    "The agbada is 4800000",          # right product, wrong unit: kobo read as naira
])
def test_an_amount_no_lookup_produced_is_blocked(reply: str) -> None:
    verdict = check(reply, GROUNDED)
    assert verdict.blocked, f"let through an ungrounded price: {reply!r}"
    assert verdict.rule == "money"
    assert verdict.detail["allowed_kobo"], "the audit row must carry the allowed set"


@pytest.mark.parametrize("reply", [
    "The Swiss Voile Lace is ₦12,500 and we have it in black.",
    "Delivery within Lagos is ₦2,500.",
    "It is ₦12,500. Shall I send you a payment link?",
    "Yes, it's available.",
    "It is 12,500 naira in total.",
    "We have 3 left in wine.",              # a quantity, not a price
    "The gele is ₦3,200.",                  # grounded once 320000 is allowed
])
def test_a_grounded_amount_is_never_blocked(reply: str) -> None:
    """The must-allow half of the money rule. This is the half that decides
    whether the product is usable: a guard that blocks correct replies makes
    the agent look broken, and it fails silently -- the only symptom is an
    escalation the owner cannot explain."""
    allowed = Allowed(money_kobo=frozenset({LACE_KOBO, DELIVERY_KOBO, 320_000}),
                      availability=True)
    assert check(reply, allowed).ok, f"blocked a correct reply: {reply!r}"


def test_a_single_ungrounded_amount_blocks_the_whole_reply() -> None:
    """One bad figure among good ones is still a bad figure. The customer
    reads the message, not the parts of it that were grounded."""
    reply = "The lace is 12,500 naira, and the gele is 3,200 naira."
    verdict = check(reply, GROUNDED)
    assert verdict.blocked
    assert verdict.detail["tokens"] == ["3,200 naira"]


def test_a_comma_formatted_price_under_ten_thousand_is_caught() -> None:
    """The hole that four-consecutive-digits left open. "9,999" is not four
    consecutive digits, so without the comma branch every four-figure price
    written the way prices are actually written passes ungrounded."""
    for reply in ("I can do it for 9,999.", "It's 1,250.", "That's 8,500 naira."):
        assert check(reply, GROUNDED).blocked, f"let through {reply!r}"


# --------------------------------------------------- rule 2: the authority


@pytest.mark.parametrize("reply", [
    "I can give you a 10% discount.",
    "Let me reduce it for you.",
    "I'll waive the delivery fee.",
    "I can do a special price for you.",
    "How about a refund?",
])
def test_a_concession_with_no_authority_is_blocked(reply: str) -> None:
    """This is the rule that protects actual money, and it fires on the most
    common utterance in the whole corpus: 'Reduce am'."""
    verdict = check(reply, GROUNDED)
    assert verdict.blocked, f"allowed an unauthorised concession: {reply!r}"
    assert verdict.rule == "authority"


@pytest.mark.parametrize("reply", [
    "We do not offer discounts, but I can ask the owner for you.",
    "Sorry, no discount on cut fabric.",
    "I cannot reduce the price, but the lace is ₦12,500.",
    "I'm not able to waive the delivery fee.",
])
def test_refusing_a_discount_is_not_offering_one(reply: str) -> None:
    """The business's own policy says 'we do not offer discounts'. An agent
    that quotes its policy must not be blocked for it -- a guard that cannot
    tell an offer from a refusal escalates every correct answer."""
    assert check(reply, GROUNDED).ok, f"blocked a refusal: {reply!r}"


def test_an_authorised_concession_passes() -> None:
    allowed = Allowed(authority=True, availability=True)
    assert check("I can give you a 10% discount.", allowed).ok


# ----------------------------------------------- rule 3: the availability


@pytest.mark.parametrize("reply", [
    "Yes, we have it in stock.",
    "It's available in black and wine.",
    "The gele is available.",
])
def test_an_availability_claim_needs_a_stock_lookup(reply: str) -> None:
    verdict = check(reply, NO_STOCK_CHECK)
    assert verdict.blocked, f"claimed stock without checking: {reply!r}"
    assert verdict.rule == "availability"


@pytest.mark.parametrize("reply", [
    "The Kaftan is not available right now.",
    "Sorry, we don't have that in XL.",
    "That one has finished.",
])
def test_saying_something_is_unavailable_is_always_allowed(reply: str) -> None:
    """Being honest about having nothing costs nothing and must never be
    blocked, or the agent cannot say no."""
    assert check(reply, Allowed()).ok, f"blocked an honest refusal: {reply!r}"


@pytest.mark.parametrize("reply", [
    "E dey cost ₦12,500.",
    "We'll get back to you shortly.",
])
def test_phrasings_that_look_like_availability_but_are_not(reply: str) -> None:
    """'e dey' means 'it is', and 'we get back to you' means we will reply.
    Both are obvious phrasings to reach for and both are wrong."""
    assert check(reply, GROUNDED).ok, f"false positive: {reply!r}"


# --------------------------------------------------- rule 4: the payment


@pytest.mark.parametrize("reply", [
    "Thank you! Payment received, we ship today.",
    "Your payment has been confirmed.",
    "Payment successful — your order is on its way.",
    "We have received your money.",
    "I don see your payment, thank you.",
])
def test_a_payment_claim_with_no_provider_record_is_blocked(reply: str) -> None:
    """The vision's own example of what must never be believed. Customers say
    'I don pay' before the money lands, and sometimes it never does -- so the
    agent confirming it hands over goods for free."""
    verdict = check(reply, GROUNDED)
    assert verdict.blocked, f"confirmed a payment that did not happen: {reply!r}"
    assert verdict.rule == "payment", f"wrong rule for {reply!r}"


@pytest.mark.parametrize("reply", [
    "Let me confirm that payment first — one moment.",
    "I can't see your payment yet. Let me check.",
    "Your payment has not been confirmed yet.",
    "Once you pay, we will ship the same day.",
])
def test_checking_a_payment_is_not_confirming_one(reply: str) -> None:
    """"Let me confirm that payment" is the correct reply, and must never be
    blocked -- it is the exact sentence the agent is supposed to say."""
    assert check(reply, GROUNDED).ok, f"blocked a correct reply: {reply!r}"


def test_a_verified_provider_record_permits_confirmation() -> None:
    """And the other direction: once Paystack's webhook has written the row,
    the agent must be able to tell the customer."""
    confirmed = Allowed(money_kobo=frozenset({LACE_KOBO}), availability=True,
                        payment_confirmed=True)
    assert check("Thank you! Payment received.", confirmed).ok


# --------------------------------------------------------- the whole point


def test_prose_cannot_authorise_a_figure() -> None:
    """The rolling summary is text. A price in it must not become quotable by
    having been written down, which is why the allowed set is built from SQL
    and tool results only and never from anything the model produced.

    This is a property of the design rather than a rule in the code, so it is
    asserted as one: an allowed set that cannot be constructed from prose.
    """
    summary = "Earlier the customer was told the lace costs ₦9,000."
    # Nothing in the guard reads `summary`; the only way 900000 enters the
    # allowed set is a tool result. So a reply quoting it is blocked.
    assert check("As discussed, the lace is ₦9,000.", GROUNDED).blocked


def test_silence_is_always_permitted() -> None:
    """The no_reply tool must never be blocked."""
    assert check("", Allowed()).ok
    assert check("   ", Allowed()).ok


def test_the_verdict_carries_enough_to_audit() -> None:
    """Requirement 4.8: the block, the offending tokens and the allowed set."""
    verdict = check("The lace is ₦9,000.", GROUNDED)
    assert verdict.detail["tokens"] == ["₦9,000"]
    assert verdict.detail["offending_kobo"] == [900_000]
    assert LACE_KOBO in verdict.detail["allowed_kobo"]
