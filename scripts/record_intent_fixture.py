"""Record what the real model says for each golden-set utterance.

    python scripts/record_intent_fixture.py

Writes `core/tests/fixtures/intent.json`. Re-run after every prompt change:
the fixture is a recording, so a prompt edit that is not re-recorded leaves
the offline test asserting against a model that no longer exists.

Costs one request per utterance. The free tiers are per model per day, so a
run may exhaust the first model and fall through the chain -- which is the
chain working, not a failure.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core" / "src"))

from aisales import agent, dotenv, prompts, providers  # noqa: E402
from aisales.providers import ProviderError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "core" / "tests" / "fixtures" / "intent.json"
GOLDEN = ROOT / "core" / "tests" / "test_nigeria.py"


def utterances() -> list[tuple[str, str]]:
    """Import the golden set out of the test module, so the two cannot drift.

    Imported rather than parsed: the obvious text-slicing version finds the
    first `]` in the file, which is the one closing the type annotation, and
    fails on the first run.
    """
    spec = importlib.util.spec_from_file_location("_golden", GOLDEN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [(text, intent) for text, intent, _slots in module.GOLDEN]


def main() -> int:
    dotenv.load()
    try:
        chat = providers.default_chat()
    except ProviderError as exc:
        print(f"no model available: {exc}")
        return 1

    responses: dict[str, str] = {}
    correct = 0
    cases = utterances()
    print(f"recording {len(cases)} utterances\n")

    for index, (text, expected) in enumerate(cases, 1):
        # The raw completion is what gets recorded, not the parsed intent: the
        # offline test must exercise `classify_intent`'s own parsing, or a
        # parser regression would be invisible.
        try:
            turn = chat.chat(
                [{"role": "system", "content": prompts.intent_prompt()},
                 {"role": "user", "content": text}], [])
            raw = turn.text.strip()
        except ProviderError as exc:
            print(f"  {index:2}. {text!r} -- model failed: {exc.reason}")
            return 1

        responses[text] = raw
        got = agent.classify_intent(providers.ScriptedChat(by_text={text: raw}), text)
        mark = "ok " if got.name == expected else "!! "
        if got.name == expected:
            correct += 1
        print(f"  {mark}{index:2}. {text!r} -> {got.name}"
              + ("" if got.name == expected else f"  (expected {expected})"))

    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps({
        "model": getattr(chat, "names", ["unknown"]),
        "accuracy": correct / len(cases),
        "responses": responses,
    }, indent=2, ensure_ascii=False) + "\n")

    print(f"\n{correct}/{len(cases)} correct -> {FIXTURE.relative_to(ROOT)}")
    return 0 if correct == len(cases) else 2


if __name__ == "__main__":
    sys.exit(main())
