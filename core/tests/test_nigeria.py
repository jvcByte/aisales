"""Does the agent read Nigerian commercial intent, or just words?

This is the product's differentiator and the only test of *judgement* rather
than of mechanism. Everything else in the suite proves the machine cannot say
something unsafe; this proves it understands what it is being asked.

Two modes, one test body:

  * Offline, replaying `fixtures/intent.json` -- a *recording of the real
    model*, produced by `scripts/record_intent_fixture.py`. That is what keeps
    the offline run honest: a hand-written stub would only be testing itself.
  * Live, with `AISALES_LIVE=1` and a key, which re-records as it asserts.

Re-record after every prompt change. A drop in accuracy is a prompt
regression, and the confusion table names the intent that broke so the fix is
targeted rather than guessed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aisales import agent, prompts, providers

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "intent.json"

# (utterance, expected intent, expected slots)
#
# Every one is a pattern from the vision's own list or from Nigerian
# conversational commerce. The hard cases are the ones where a literal reading
# inverts the meaning -- "How much last?" is the clearest: it is not a question
# about the past, it is the opening move of a negotiation.
GOLDEN: list[tuple[str, str, dict]] = [
    ("Abeg how much?",                    "price_enquiry",      {}),
    ("How much last?",                    "discount_request",   {}),
    ("Last price?",                       "discount_request",   {}),
    ("Reduce am",                         "discount_request",   {}),
    ("Abeg reduce small",                 "discount_request",   {}),
    ("Send account",                      "payment_intent",     {}),
    ("Oya send the account number",       "payment_intent",     {}),
    ("Send me the link",                  "payment_intent",     {}),
    ("Chop my money, abeg send am",       "payment_intent",     {}),
    ("You get XL?",                       "availability_check", {"size": "XL"}),
    ("E dey size 42?",                    "availability_check", {"size": "42"}),
    ("Na how much be this one?",          "price_enquiry",      {}),
    ("Wetin be the price for the lace?",  "price_enquiry",      {"product_hint": "lace"}),
    ("I wan buy",                         "buy_intent",         {}),
    ("Give me two",                       "buy_intent",         {"qty": 2}),
    ("Abeg, e dey red?",                  "variant_check",      {"colour": "red"}),
    ("Na only this colour you get?",      "variant_check",      {}),
    ("Wetin you get for gown?",           "browse",             {"product_hint": "gown"}),
    ("Make I see am",                     "browse",             {}),
    ("Do you deliver to Abuja?",          "delivery_enquiry",   {"city": "Abuja"}),
    ("You dey do delivery?",              "delivery_enquiry",   {}),
    ("When you go deliver?",              "delivery_enquiry",   {}),
    ("How far, you don send am?",         "delivery_status",    {}),
    ("I don pay",                         "payment_claim",      {}),
    ("My money don go?",                  "payment_claim",      {}),
    ("₦12,500 na too much",               "price_objection",    {}),
    ("Sorry, e too cost",                 "price_objection",    {}),
    ("I no get cash now, next week",      "deferral",           {}),
    ("I go get back to you",              "deferral",           {}),
    ("Abeg hold am for me",               "reserve_request",    {}),
]

#: Utterances whose slot extraction is genuinely harder. Reported but not
#: gating, because failing the build on "Give me two" -> qty=2 teaches nobody
#: anything about whether the agent can sell.
SLOT_EXPECTED = {text for text, _, slots in GOLDEN if slots}


def run(chat) -> list[tuple[str, str, str, dict]]:
    """(utterance, expected, got, slots) for every line.

    The same function both modes use, so the fixture cannot drift from the
    code that is supposed to be reading it.
    """
    rows = []
    for text, want, _slots in GOLDEN:
        got = agent.classify_intent(chat, text)
        rows.append((text, want, got.name, got.slots))
    return rows


def confusion(rows) -> str:
    """Which intents the model confuses, not just how many it got wrong."""
    wrong = [r for r in rows if r[1] != r[2]]
    if not wrong:
        return ""
    lines = [f"{len(rows) - len(wrong)}/{len(rows)} correct", "", "  expected -> got"]
    tally: dict[tuple[str, str], list[str]] = {}
    for text, want, got, _ in wrong:
        tally.setdefault((want, got), []).append(text)
    for (want, got), examples in sorted(tally.items()):
        lines.append(f"  {want} -> {got}  ({len(examples)})")
        for example in examples[:3]:
            lines.append(f"      {example!r}")
    return "\n".join(lines)


def test_the_golden_set_is_well_formed() -> None:
    """Cheap structural checks, so a typo in the table cannot silently pass."""
    assert len(GOLDEN) >= 25, "the golden set is meant to hold at least 25 lines"
    for text, intent, _slots in GOLDEN:
        assert intent in prompts.INTENTS, f"{intent!r} is not in the closed set"
        assert text.strip()
    assert len({text for text, _, _ in GOLDEN}) == len(GOLDEN), "duplicate utterance"


@pytest.mark.skipif(not FIXTURE.exists(),
                    reason="no recording yet; run scripts/record_intent_fixture.py")
def test_intent_accuracy_offline() -> None:
    """No key, no quota, no spend: replays a recording of the real model."""
    recorded = json.loads(FIXTURE.read_text())
    chat = providers.ScriptedChat(by_text=recorded["responses"])
    rows = run(chat)
    assert not [r for r in rows if r[1] != r[2]], confusion(rows)


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("AISALES_LIVE") != "1",
                    reason="set AISALES_LIVE=1 to run against a real model")
def test_intent_accuracy_live() -> None:
    chat = providers.default_chat()
    rows = run(chat)
    missed = [r for r in rows if r[1] != r[2]]
    # Reported either way: the accuracy is the number worth watching, and a
    # pass that hides it is a pass that hides a regression.
    print("\n" + confusion(rows))
    assert len(missed) <= 2, confusion(rows)


@pytest.mark.skipif(not FIXTURE.exists(), reason="no recording yet")
def test_slots_are_reported_but_not_gating() -> None:
    recorded = json.loads(FIXTURE.read_text())
    chat = providers.ScriptedChat(by_text=recorded["responses"])
    rows = run(chat)
    missed = [(t, want, got) for t, want, got, slots in rows
              if t in SLOT_EXPECTED and slots != GOLDEN[
                  [g[0] for g in GOLDEN].index(t)][2]]
    if missed:
        print(f"\n  slot extraction missed on {len(missed)} of {len(SLOT_EXPECTED)}:")
        for text, want, got in missed:
            print(f"      {text!r} expected {want}, got {got}")
