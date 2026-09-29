"""The model, behind a Protocol.

Ported from the existing convention: a fallback chain, reason-tagged errors,
retry classification that treats a spent free allowance as the ordinary way
this runs rather than as an exception, and a scripted implementation so the
whole pipeline is exercisable with no key, no quota and no spend.

One change from the precedent. `Writer.write(prompt) -> str` becomes
`Chat.chat(messages, tools) -> Turn`, because a sales agent has to call tools
and a prompt-in/text-out interface cannot express that.

**The sharpest risk in this system lives here.** The free-tier models were
chosen for writing prose, not for tool calling, and several OpenAI-compatible
endpoints accept a `tools` parameter and silently ignore it. A model that does
that answers from memory, confidently, with no price lookup behind it -- which
looks exactly like a working agent and is precisely the failure this product
cannot have. Three things guard against it, in order of reliability:

  1. The tool-call validation in `tools.py` -- a malformed call never runs.
  2. `ChainChat` learning which models reject tools, so they are not tried again.
  3. The guard, which blocks the *consequence* regardless of the cause.

`Turn.finish_reason` is kept so the diagnosis is possible after the fact, but
it cannot detect silent ignoring: a model that ignores `tools` and one that
correctly chooses to answer without them both return text with `stop`. No
response field distinguishes them; only the guard does.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from aisales.prompts import INTENT_MARKER

RETRIES = 3
BACKOFF_CAP_S = 30
# 503 "high demand" and 429 "rate limited" are routine on a free tier and
# clear on their own; anything else is a real error and should not be retried
# several times with backoff before surfacing.
TRANSIENT = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "overloaded",
             "rate limit")

GROQ_URL = "https://api.groq.com/openai/v1"
HUGGINGFACE_URL = "https://router.huggingface.co/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
GEMINI_OPENAI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

# Free tiers rename and retire models constantly, and every one of these was
# wrong when first written. Each list is overridable from the environment so a
# stale name does not need a code change.
#
# These were corrected against a live key, having been copied from a sibling
# project where they had gone stale: `gemini-3.8-flash` and friends do not
# exist, and every turn burned a round-trip failing over before reaching a
# provider that does. `gemini-flash-latest` is first because it is an alias
# that always resolves, so the head of the chain cannot rot.
# `gemini-2.5-flash` is deliberately absent: it answers 404 "no longer
# available to new users", so listing it costs a wasted request on every turn
# for as long as the process lives. A retired model is not a fallback.
GEMINI_MODELS = ("gemini-flash-latest", "gemini-3-flash-preview",
                 "gemini-3.1-flash-lite")
GROQ_MODELS = ("openai/gpt-oss-120b", "openai/gpt-oss-20b")
HUGGINGFACE_MODELS = ("meta-llama/Llama-3.3-70B-Instruct",)
OPENROUTER_MODELS = ("nvidia/nemotron-3.5-lightning:free", "qwen/qwen3.8-27b:free")

# Every one of these sits behind Cloudflare, which answers a request carrying
# no User-Agent -- urllib sends "Python-urllib/3.x" -- with a 403 and nothing
# else. Groq returns the bare string "error code: 1010", so the failure reads
# as a bad key rather than a missing header.
USER_AGENT = "aisales/0.1"

# Models to stop asking. Two things land here, and both are permanent for the
# life of the process rather than transient:
#
#   * a model that rejects a `tools` parameter
#   * a model that has been retired, answering 404
#
# Learning them at runtime matters because a free tier retires a name without
# notice, and a chain that keeps offering a dead model pays for it on every
# single turn -- three wasted round-trips each time, forever.
_DECLARED_NO_TOOLS = {"nvidia/nemotron-3.5-lightning:free"}
_SKIP: set[str] = set()


class ProviderError(RuntimeError):
    """A model call failed.

    ``reason`` is a short tag -- ``quota``, ``busy``, ``unsupported``,
    ``error`` -- so a caller can react without parsing prose, and so a failed
    job's error_code says something actionable rather than naming an exception.
    """

    def __init__(self, message: str, *, reason: str = "error"):
        super().__init__(message)
        self.reason = reason


def _error_text(exc: Exception) -> str:
    return str(getattr(exc, "body", None) or exc)


def is_transient(exc: Exception) -> bool:
    return any(marker in _error_text(exc) for marker in TRANSIENT)


def is_daily_quota(exc: Exception) -> bool:
    """A per-day quota will not clear while we wait, so retrying is pointless.

    The free tier's limit is per *model*, per day, so the useful response is
    to move to another model rather than to sleep.
    """
    text = _error_text(exc)
    return "PerDay" in text or "per day" in text.lower()


def retry_after(exc: Exception, attempt: int) -> float:
    """Wait the API asked for, or an exponential backoff if it did not say."""
    text = str(getattr(exc, "body", None) or exc)
    hinted = re.search(r"retry[Dd]elay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", text)
    if hinted:
        return min(float(hinted.group(1)) + 2, 70)
    return min(2.0 ** attempt, BACKOFF_CAP_S)


def summarise(failures: list[tuple[str, "ProviderError"]]) -> tuple[str, str]:
    """One short line per model failing, plus the overall reason.

    Every model failing for the same cause is *one* fact, not five, and it is
    the difference between a message someone can act on and a wall of json.
    """
    reasons = {exc.reason for _, exc in failures}
    if len(failures) == 1:
        name, exc = failures[0]
        if exc.reason == "quota":
            return (f"{name} is out of free-tier quota; it is per model, per day, "
                    "and resets daily", "quota")
        return str(exc)[:300], exc.reason
    if reasons == {"quota"}:
        return (f"all {len(failures)} models are out of free-tier quota; it is "
                "per model, per day, and resets daily", "quota")
    if len(reasons) == 1:
        only = reasons.pop()
        return f"every model failed: {only}", only
    detail = "; ".join(f"{model}: {exc}" for model, exc in failures)
    return detail[:300], "error"


# ------------------------------------------------------------------- types


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]

    def as_message(self) -> dict:
        """This call as the assistant turn that requested it."""
        return {"id": self.id, "type": "function",
                "function": {"name": self.name,
                             "arguments": json.dumps(self.args)}}


@dataclass(frozen=True)
class Turn:
    """One model response: something said, tools requested, or both."""

    text: str = ""
    calls: tuple[ToolCall, ...] = ()
    model: str = ""
    finish_reason: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.calls)


class Chat(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> Turn:
        """Respond to *messages*, optionally calling one of *tools*."""


# ------------------------------------------------------------------ parsing


def parse_turn(payload: dict, model: str) -> Turn:
    """An OpenAI-compatible completion into a Turn.

    `arguments` arrives as a JSON *string*, not an object, and is frequently
    empty or truncated on a weak model. A call whose arguments will not parse
    is dropped rather than guessed at: dispatching `create_order` with
    invented arguments is worse than not dispatching it.
    """
    try:
        choice = payload["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"unexpected reply: {str(payload)[:200]}") from exc

    calls: list[ToolCall] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = raw.get("function") or {}
        name = function.get("name") or ""
        if not name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            args = arguments
        else:
            try:
                args = json.loads(arguments or "{}")
            except (ValueError, TypeError):
                continue
        if not isinstance(args, dict):
            continue
        calls.append(ToolCall(id=str(raw.get("id") or f"call_{index}"),
                              name=name, args=args))

    return Turn(text=(message.get("content") or "").strip(),
                calls=tuple(calls), model=model,
                finish_reason=choice.get("finish_reason") or "", raw=payload)


def classify_http(status: int, detail: str) -> ProviderError:
    """An HTTP failure into one of the reasons the chain branches on.

    The distinction that matters is whether waiting helps. A per-minute rate
    limit clears; a per-day one does not, and treating it as busy would make
    the chain sleep through its whole retry budget for nothing.

    Status is read first: providers word their error bodies freely, and
    matching on the word "quota" ahead of the status reads a 500 as an
    allowance that comes back tomorrow.

    The `unsupported` branch is the one this file adds to the precedent. A 400
    saying tools are not supported is *permanent for that model*, so retrying
    it is pure waste -- and worse, it means a model that cannot call tools
    would otherwise keep being handed agent turns.
    """
    if status in (500, 502, 503, 504):
        return ProviderError(f"service unavailable ({status})", reason="busy")
    text = detail.lower()
    if status == 400 and any(w in text for w in ("tool", "function call", "function_call")):
        return ProviderError("model does not support tools", reason="unsupported")
    if status in (401, 403):
        return ProviderError(f"rejected the key ({status}): {detail[:120]}")
    if status == 404:
        # Permanent for this model, so the chain stops offering it rather than
        # paying for it on every turn.
        return ProviderError(f"model retired or unknown: {detail[:120]}",
                             reason="retired")
    if status == 429 or any(w in text for w in ("quota", "rate limit", "credit")):
        if any(w in text for w in ("per day", "daily", "quota", "credit")):
            return ProviderError("daily quota exhausted", reason="quota")
        return ProviderError("rate limited", reason="busy")
    return ProviderError(f"HTTP {status}: {detail[:160]}")


def _post_json(url: str, body: bytes, headers: dict, timeout: float) -> dict:
    """One HTTPS POST. Separate so a test can stand in for the network."""
    headers = {"User-Agent": USER_AGENT, **headers}
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


# ---------------------------------------------------------------- live chat


class OpenAICompatChat:
    """A Chat for any service that speaks the OpenAI chat API.

    Gemini, Groq, Hugging Face's router and OpenRouter all do, so one class
    covers four tiers rather than four classes covering one each. Nothing new
    is installed for it: a completion is one HTTPS POST.
    """

    def __init__(self, api_key: str, model: str, *, base_url: str,
                 fallbacks: tuple[str, ...] = (), label: str = "model",
                 timeout_s: float = 120.0, opener=None):
        if not api_key:
            raise ProviderError(f"an API key is required for {label}")
        self._key = api_key
        self._models = [model, *(m for m in fallbacks if m != model)]
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self.name = label
        self._timeout = timeout_s
        self._post = opener or _post_json

    def chat(self, messages: list[dict], tools: list[dict]) -> Turn:
        failures: list[tuple[str, ProviderError]] = []
        for index, model in enumerate(self._models):
            if model in _SKIP or model in _DECLARED_NO_TOOLS:
                failures.append((model, ProviderError("model is not usable",
                                                      reason="unsupported")))
                continue
            try:
                return self._ask(model, messages, tools)
            except ProviderError as exc:
                if exc.reason in ("unsupported", "retired"):
                    _SKIP.add(model)
                failures.append((model, exc))
        message, reason = summarise(failures)
        raise ProviderError(message, reason=reason)

    def _ask(self, model: str, messages: list[dict], tools: list[dict]) -> Turn:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        headers = {"Authorization": f"Bearer {self._key}",
                   "Content-Type": "application/json"}
        payload = self._send(json.dumps(body).encode(), headers)
        return parse_turn(payload, model)

    def _send(self, body: bytes, headers: dict) -> dict:
        for attempt in range(1, RETRIES + 1):
            try:
                return self._post(self._url, body, headers, self._timeout)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                error = classify_http(exc.code, detail)
                if error.reason != "busy" or attempt == RETRIES:
                    raise error from exc
                time.sleep(retry_after(exc, attempt))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == RETRIES:
                    raise ProviderError(f"could not reach {self.name}: {exc}",
                                        reason="busy") from exc
                time.sleep(retry_after(exc, attempt))
        raise ProviderError(f"failed after {RETRIES} attempts")


# --------------------------------------------------------------- the chain


class ChainChat:
    """Several providers standing in for one another.

    Not an error path: the ordinary way this runs is that the first free tier
    is spent and the next one answers. It reports only once every provider
    with a key has been tried, so the message says what is actually wrong
    rather than what happened to fail first.
    """

    def __init__(self, chats: list[tuple[str, Chat]]):
        if not chats:
            raise ProviderError("no model was configured")
        self._chats = chats

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self._chats]

    def chat(self, messages: list[dict], tools: list[dict]) -> Turn:
        failures: list[tuple[str, ProviderError]] = []
        for index, (name, chat) in enumerate(self._chats):
            try:
                return chat.chat(messages, tools)
            except ProviderError as exc:
                failures.append((name, exc))
                if index + 1 < len(self._chats):
                    print(f"  ! {name} unavailable ({exc.reason}), "
                          f"trying {self._chats[index + 1][0]}")
        message, reason = summarise(failures)
        raise ProviderError(message, reason=reason)


# ------------------------------------------------------------- scripted


def scripted_turn(text: str = "", calls: list[tuple[str, dict]] | None = None,
                  model: str = "scripted") -> Turn:
    """Build a scripted Turn. `calls` is [(tool_name, args), ...]."""
    return Turn(
        text=text,
        calls=tuple(ToolCall(id=f"sc-{i}", name=name, args=args)
                    for i, (name, args) in enumerate(calls or [])),
        model=model, finish_reason="tool_calls" if calls else "stop",
    )


class ScriptedChat:
    """Replays recorded responses, including tool calls.

    This is what makes the offline path the *real* path rather than a mock: a
    scripted first turn calls a tool, the real dispatcher runs the real SQL,
    and the scripted second turn sees that result -- so the guard's allowed set
    is populated exactly as it would be live.

    Two modes. In order, for a turn loop. Keyed on a substring of the newest
    user message, for the intent golden set, where one code path is called once
    per utterance and call order is incidental.
    """

    def __init__(self, *, turns: list[Turn] | None = None,
                 by_text: dict[str, str] | None = None, name: str = "scripted",
                 intent: str = "unknown", intent_confidence: float = 0.0):
        if turns is None and not by_text:
            raise ProviderError("ScriptedChat needs turns or by_text")
        self._turns = list(turns or [])
        self._by_text = dict(by_text or {})
        self._index = 0
        self.name = name
        self._intent = intent
        self._intent_confidence = intent_confidence

    def chat(self, messages: list[dict], tools: list[dict]) -> Turn:
        # Classifier calls are answered separately and do not consume the
        # script. A turn runs classification *first*, so an ordered script that
        # ignored this would hand the classifier the agent's opening tool call
        # and leave the turn with nothing to do -- and the resulting failure
        # looks like the agent, not the harness.
        system = next((str(m.get("content") or "") for m in messages
                       if m.get("role") == "system"), "")
        if INTENT_MARKER in system:
            # A recorded classification wins when one exists. This is what lets
            # the golden set replay what a real model actually said rather than
            # a hand-written stub -- which is the difference between testing
            # the prompt and testing the stub.
            if self._by_text:
                newest = next((str(m.get("content") or "") for m in reversed(messages)
                               if m.get("role") == "user"), "")
                for needle, reply in self._by_text.items():
                    if needle.lower() in newest.lower():
                        return Turn(text=reply, model=self.name, finish_reason="stop")
                # Unrecorded utterance. Empty text makes classify_intent return
                # `unknown`, which shows up as a failure rather than passing.
                return Turn(text="", model=self.name, finish_reason="stop")
            return Turn(
                text=json.dumps({"intent": self._intent,
                                 "confidence": self._intent_confidence,
                                 "slots": {}}),
                model=self.name, finish_reason="stop")

        if self._by_text:
            newest = ""
            for message in reversed(messages):
                if message.get("role") == "user":
                    newest = str(message.get("content") or "")
                    break
            for needle, reply in self._by_text.items():
                if needle.lower() in newest.lower():
                    return Turn(text=reply, model=self.name, finish_reason="stop")
            # No key matched. Returning empty text is the honest outcome: the
            # caller must treat it as a model that said nothing rather than
            # being handed an invented answer.
            return Turn(text="", model=self.name, finish_reason="stop")

        if self._index >= len(self._turns):
            raise ProviderError("the script is exhausted", reason="error")
        turn = self._turns[self._index]
        self._index += 1
        return turn


# ---------------------------------------------------------------- factories


def _models_from_env(var: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    """A free tier renames its models often; a stale name should not need a
    code change to fix. An empty or all-commas value is a typo, not an
    instruction to offer no models at all."""
    raw = os.environ.get(var, "").strip()
    return tuple(p.strip() for p in raw.split(",") if p.strip()) or defaults


def default_chat() -> Chat:
    """Every provider that has a key, in the order they are tried.

    Gemini first, then the tiers that answer a spent allowance -- which on a
    free daily limit is a normal thing to hit rather than an exceptional one.
    """
    if os.environ.get("AISALES_FAKES") == "1":
        # Refused rather than silently faked. Callers that want a fake build a
        # ScriptedChat themselves; this function's job is a real model, and
        # handing back a script because of an env var would make a live worker
        # look healthy while answering from a fixture.
        raise ProviderError(
            "AISALES_FAKES=1 is set, so no real model may be called. Build a "
            "ScriptedChat explicitly, or set AISALES_FAKES=0 in .env."
        )

    entries: list[tuple[str, Chat]] = []

    def add(label: str, key: str, models_var: str, defaults: tuple[str, ...],
            base_url: str) -> None:
        if not key:
            return
        models = _models_from_env(models_var, defaults)
        entries.append((label, OpenAICompatChat(
            key, models[0], base_url=base_url, fallbacks=models[1:], label=label)))

    add("gemini", os.environ.get("GEMINI_API_KEY", ""), "GEMINI_MODELS",
        GEMINI_MODELS, GEMINI_OPENAI_URL)
    add("groq", os.environ.get("GROQ_API_KEY", ""), "GROQ_MODELS",
        GROQ_MODELS, GROQ_URL)
    add("huggingface", os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGINGFACE_API_KEY", ""), "HF_MODELS",
        HUGGINGFACE_MODELS, HUGGINGFACE_URL)
    add("openrouter", os.environ.get("OPENROUTER_API_KEY", ""), "OPENROUTER_MODELS",
        OPENROUTER_MODELS, OPENROUTER_URL)

    if not entries:
        raise ProviderError(
            "no model key is configured. Set GEMINI_API_KEY, or GROQ_API_KEY, "
            "HF_TOKEN or OPENROUTER_API_KEY for a free fallback."
        )
    return entries[0][1] if len(entries) == 1 else ChainChat(entries)


def assert_not_mixed(*, fakes: bool, live_channel: bool) -> None:
    """Refuse to let a fake provider reach a real customer.

    A scripted model that happens to produce a plausible sentence, wired to a
    real WhatsApp channel, answers a real person with words no model ever
    produced. That must fail loudly at the boundary rather than being caught by
    a comment in a docstring.
    """
    if fakes and live_channel:
        raise ProviderError(
            "refusing to run: a fake provider is active on a live channel. "
            "Unset AISALES_FAKES, or point the channel at the simulator."
        )
