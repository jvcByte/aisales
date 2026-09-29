"""The provider chain.

No network: `OpenAICompatChat` takes an `opener`, so these test the decisions
the chain makes rather than anybody's uptime. The decisions are the part that
matters -- which failures are worth retrying, which mean "never ask this again",
and what happens to a tool call that arrives malformed.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from aisales import providers


def http_error(status: int, detail: str = "") -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError("https://example.test", status, detail,
                                  {}, io.BytesIO(detail.encode()))


def reply(text: str = "hello", tool_calls: list | None = None) -> dict:
    return {"choices": [{"message": {"content": text, "tool_calls": tool_calls},
                         "finish_reason": "tool_calls" if tool_calls else "stop"}]}


@pytest.fixture(autouse=True)
def clean_skip_set():
    """`_SKIP` is process-global by design -- it is a memory of what the chain
    has learned -- so a test must not inherit another's."""
    providers._SKIP.clear()
    yield
    providers._SKIP.clear()


# ------------------------------------------------------- what is retryable


def test_a_busy_provider_is_transient_and_a_quota_is_not() -> None:
    assert providers.is_transient(RuntimeError("503 UNAVAILABLE"))
    assert providers.is_transient(RuntimeError("429 rate limit"))
    assert providers.is_daily_quota(RuntimeError("PerDay quota exceeded"))
    assert not providers.is_daily_quota(RuntimeError("503 UNAVAILABLE"))


def test_a_busy_status_is_classified_as_busy() -> None:
    assert providers.classify_http(503, "").reason == "busy"
    assert providers.classify_http(429, "slow down").reason == "busy"


def test_a_daily_quota_is_not_treated_as_busy() -> None:
    """Waiting does not refill a per-day allowance, so retrying one is pure
    delay. The chain must move to another model instead."""
    assert providers.classify_http(429, "quota exceeded, per day").reason == "quota"


def test_a_rejected_key_is_not_retried() -> None:
    assert providers.classify_http(401, "invalid key").reason == "error"


def test_a_400_about_tools_is_permanent_for_that_model() -> None:
    error = providers.classify_http(400, "tools are not supported by this model")
    assert error.reason == "unsupported"


def test_a_404_is_a_retired_model_not_a_generic_error() -> None:
    assert providers.classify_http(404, "no longer available").reason == "retired"


# ---------------------------------------------- models that never work again


def test_a_retired_model_is_not_asked_twice() -> None:
    """A free tier retires a name without notice. A chain that keeps offering
    it pays three wasted round-trips on every turn, forever."""
    calls: list[str] = []

    def opener(url, body, headers, timeout):
        model = json.loads(body)["model"]
        calls.append(model)
        if model == "dead-model":
            raise http_error(404, "model is no longer available to new users")
        return reply("ok")

    chat = providers.OpenAICompatChat("k", "dead-model", base_url="https://x.test",
                                      fallbacks=("live-model",), opener=opener)
    assert chat.chat([{"role": "user", "content": "hi"}], []).text == "ok"
    assert calls == ["dead-model", "live-model"]

    calls.clear()
    chat.chat([{"role": "user", "content": "again"}], [])
    assert calls == ["live-model"], "the retired model was asked a second time"


def test_a_model_that_cannot_call_tools_is_not_asked_twice() -> None:
    calls: list[str] = []

    def opener(url, body, headers, timeout):
        model = json.loads(body)["model"]
        calls.append(model)
        if model == "no-tools":
            raise http_error(400, "this model does not support tools")
        return reply("ok")

    chat = providers.OpenAICompatChat("k", "no-tools", base_url="https://x.test",
                                      fallbacks=("tool-capable",), opener=opener)
    tools = [{"type": "function", "function": {"name": "x"}}]
    chat.chat([{"role": "user", "content": "hi"}], tools)
    calls.clear()
    chat.chat([{"role": "user", "content": "hi"}], tools)
    assert calls == ["tool-capable"]


def test_every_model_failing_reports_one_actionable_line() -> None:
    """Five copies of a 429 body is what turns an error into a wall of json."""
    chat = providers.OpenAICompatChat(
        "k", "a", base_url="https://x.test", fallbacks=("b", "c"),
        opener=lambda *a: (_ for _ in ()).throw(http_error(429, "per day quota")))
    with pytest.raises(providers.ProviderError) as caught:
        chat.chat([{"role": "user", "content": "hi"}], [])
    assert caught.value.reason == "quota"
    assert "per model, per day" in str(caught.value)


# ------------------------------------------------------------- parsing


def test_tool_calls_are_parsed_from_a_string_arguments_field() -> None:
    """OpenAI-compatible APIs send `arguments` as a JSON *string*, not an
    object. Treating it as an object silently produces zero tool calls."""
    payload = reply(tool_calls=[{
        "id": "call_1", "type": "function",
        "function": {"name": "search_products", "arguments": '{"query": "lace"}'}}])
    turn = providers.parse_turn(payload, "m")
    assert turn.wants_tools
    assert turn.calls[0].name == "search_products"
    assert turn.calls[0].args == {"query": "lace"}


def test_a_malformed_tool_call_is_dropped_rather_than_guessed_at() -> None:
    """Dispatching `create_order` with invented arguments is worse than not
    dispatching it. A call whose arguments will not parse is not repaired."""
    payload = reply(tool_calls=[
        {"id": "1", "function": {"name": "create_order", "arguments": "{broken"}},
        {"id": "2", "function": {"name": "search_products",
                                 "arguments": '{"query": "ok"}'}},
        {"id": "3", "function": {"name": "", "arguments": "{}"}},
    ])
    turn = providers.parse_turn(payload, "m")
    assert [c.name for c in turn.calls] == ["search_products"]


def test_a_reply_with_no_choices_is_an_error_not_an_empty_answer() -> None:
    with pytest.raises(providers.ProviderError):
        providers.parse_turn({"error": "something"}, "m")


def test_an_empty_completion_is_text_not_a_tool_call() -> None:
    turn = providers.parse_turn(reply(""), "m")
    assert turn.text == "" and not turn.wants_tools


# ------------------------------------------------------------- scripted


def test_scripted_chat_replays_tool_calls_in_order() -> None:
    """This is what makes the offline path the real path: the second turn sees
    the first turn's tool call dispatched against real data."""
    chat = providers.ScriptedChat(turns=[
        providers.scripted_turn(calls=[("search_products", {"query": "lace"})]),
        providers.scripted_turn(text="It is ₦12,500."),
    ])
    first = chat.chat([{"role": "user", "content": "how much?"}], [])
    assert first.calls[0].name == "search_products"
    second = chat.chat([{"role": "user", "content": "?"}], [])
    assert second.text == "It is ₦12,500."


def test_a_classifier_call_does_not_consume_the_script() -> None:
    """A turn classifies first, so a script that ignored this would hand the
    classifier the agent's opening tool call and leave the turn with nothing
    to do -- a failure that looks like the agent rather than the harness."""
    from aisales import prompts

    chat = providers.ScriptedChat(turns=[providers.scripted_turn(text="the reply")],
                                 intent="price_enquiry", intent_confidence=0.9)
    classified = chat.chat(
        [{"role": "system", "content": prompts.intent_prompt()},
         {"role": "user", "content": "Abeg how much?"}], [])
    assert json.loads(classified.text)["intent"] == "price_enquiry"

    turn = chat.chat([{"role": "system", "content": "you are a shop"},
                      {"role": "user", "content": "Abeg how much?"}], [])
    assert turn.text == "the reply", "the classifier ate the agent's script"


def test_an_exhausted_script_raises_rather_than_inventing_a_reply() -> None:
    chat = providers.ScriptedChat(turns=[providers.scripted_turn(text="only one")])
    chat.chat([{"role": "user", "content": "a"}], [])
    with pytest.raises(providers.ProviderError):
        chat.chat([{"role": "user", "content": "b"}], [])


def test_the_interlock_refuses_a_fake_on_a_live_channel() -> None:
    """A scripted model answering a real customer looks completely healthy."""
    providers.assert_not_mixed(fakes=True, live_channel=False)
    providers.assert_not_mixed(fakes=False, live_channel=True)
    with pytest.raises(providers.ProviderError):
        providers.assert_not_mixed(fakes=True, live_channel=True)
