"""The messaging boundary.

Everything a platform says arrives as an `Inbound` and everything we say
leaves as an `Outbound`, so no agent code names a channel. The point is not
tidiness: there is no WhatsApp Business API access for this build, and the
whole product still has to be developable and testable. Swapping the simulator
for Meta must touch this file and nothing else.

Responsibilities are split on risk. `verify` and `download` are the
platform-specific, security-relevant parts -- signature algorithms and two-hop
media fetches -- so they stay inside the adapter where a test can pin them.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid

import psycopg
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Protocol

# Nigerian mobile ranges. The three-digit prefix is the right level of
# strictness: it accepts every allocated range and rejects obviously-wrong
# numbers, without pinning the NCC's ever-changing four-digit allocation
# list, which would need a code change every time a range is added.
_MOBILE_PREFIXES = ("070", "071", "080", "081", "090", "091")

_NATIONAL_LENGTH = 10


class ChannelError(RuntimeError):
    """A delivery could not be verified, parsed, or sent."""


def normalise_phone(raw: str) -> str:
    """Any Nigerian spelling of a mobile number -> '+2348031234567'.

    Accepts the four forms that actually arrive, and raises on everything
    else rather than guessing a country code. A wrong guess does not produce a
    visibly broken number: it produces a valid number belonging to a stranger,
    and then messages them.

    Refusing to guess is also why a foreign number raises. E.164 has no way to
    say "probably Nigeria", and this system only ever serves Nigerian
    businesses.
    """
    if not isinstance(raw, str):
        raise ValueError(f"phone must be a string, got {type(raw).__name__}")

    text = raw.strip()
    if not text:
        raise ValueError("phone is empty")

    # WhatsApp identifies senders as JIDs: '2348031234567@s.whatsapp.net'.
    text = text.split("@", 1)[0]

    # Strip formatting: spaces, dashes, dots, parens, and a leading +.
    digits = re.sub(r"\D", "", text)
    if not digits:
        raise ValueError(f"phone has no digits: {raw!r}")

    if len(digits) == _NATIONAL_LENGTH + 3 and digits.startswith("234"):
        national = digits[3:]
    elif len(digits) == _NATIONAL_LENGTH + 1 and digits.startswith("0"):
        national = digits[1:]
    elif len(digits) == _NATIONAL_LENGTH:
        # Written without the trunk zero, which is common. Unambiguous here
        # because this product only serves Nigerian numbers.
        national = digits
    else:
        raise ValueError(f"not a Nigerian mobile number: {raw!r}")

    if not ("0" + national).startswith(_MOBILE_PREFIXES):
        raise ValueError(f"not a Nigerian mobile number: {raw!r}")

    return f"+234{national}"


def to_wa_id(phone_e164: str) -> str:
    """'+2348031234567' -> '2348031234567', the form Meta sends and expects."""
    if not phone_e164.startswith("+234"):
        raise ValueError(f"not a Nigerian number: {phone_e164!r}")
    return phone_e164[1:]


@dataclass(frozen=True)
class Inbound:
    """One customer message, normalised.

    Every channel produces this and nothing else. `external_id` is the
    provider's own message id and is the de-duplication key -- a redelivered
    webhook must not produce a second turn.
    """

    channel: str
    external_id: str
    from_phone: str
    text: str
    kind: Literal["text", "image", "voice", "document", "location", "other"] = "text"
    media_id: str | None = None
    mime_type: str | None = None
    received_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def phone_e164(self) -> str:
        return normalise_phone(self.from_phone)


@dataclass(frozen=True)
class Outbound:
    to_phone: str
    text: str
    # Always 'text' for now. A payment link is a URL in the body, which the
    # 24-hour service window permits; an interactive button needs a
    # Meta-approved template, which is a different project.
    kind: Literal["text"] = "text"


class Channel(Protocol):
    name: str

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        """Whether this delivery is genuinely from the channel."""

    def parse(self, body: bytes, headers: Mapping[str, str]) -> list[Inbound]:
        """Normalise a delivery into zero or more messages.

        Zero, because a delivery can be a status update ('read', 'delivered')
        rather than a message, and because Meta batches several together.
        """

    def send(self, message: Outbound) -> str:
        """Deliver, returning the channel's own id for it, or ''.

        Raises ChannelError on failure. A send that failed must not be
        recorded as sent, so this is called before the outbound row is
        written rather than after.
        """


class SimulatorChannel:
    """The developer's channel: a page that talks to the real agent turn.

    Not a stub. It calls the same `run_turn` the worker calls, so a working
    simulator is evidence about production behaviour rather than about a test
    harness.
    """

    name = "simulator"

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        # Nothing to verify: it is reachable only on the developer's own
        # machine, and requiring a signature would mean inventing one.
        return True

    def parse(self, body: bytes, headers: Mapping[str, str]) -> list[Inbound]:
        import json

        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChannelError(f"simulator payload is not JSON: {exc}") from exc

        text = (payload.get("text") or "").strip()
        sender = payload.get("from") or ""
        if not text or not sender:
            # A delivery with no message produces no message, exactly as a
            # read receipt does on a real channel.
            return []

        kind = payload.get("kind") or "text"
        return [Inbound(
            channel=self.name,
            external_id=str(payload.get("external_id") or f"sim-{uuid.uuid4().hex}"),
            from_phone=sender,
            text=text,
            kind=kind if kind in ("text", "image", "voice", "document",
                                  "location", "other") else "other",
            media_id=payload.get("media_id"),
            mime_type=payload.get("mime_type"),
            received_at=datetime.now(timezone.utc),
            raw=payload,
        )]

    def send(self, message: Outbound) -> str:
        # The message row is written by the turn, and the simulator page reads
        # the thread back from the API. Writing here too would give the
        # conversation two sources of truth.
        return f"sim-out-{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------ WhatsApp

GRAPH = "https://graph.facebook.com/v21.0"

_MEDIA_KINDS = {"image": "image", "audio": "voice", "voice": "voice",
                "document": "document", "location": "location",
                "sticker": "image", "video": "other"}


class MetaCloudChannel:
    """WhatsApp Cloud API.

    Two things differ from the simulator in ways that are easy to get wrong:

    **The signature algorithm.** Meta signs with HMAC-SHA256 over the raw body
    and sends `X-Hub-Signature-256: sha256=<hex>`. Paystack signs with SHA-512
    over the same idea. The two adapters look alike, and copying the Paystack
    verification into this one produces a verifier that never accepts anything
    -- or, if the comparison is loosened to make it pass, one that accepts
    everything. `test_channels.py` pins both directions.

    **A delivery is not a message.** Meta batches, and sends read and delivery
    receipts through the same endpoint. `parse` returns zero messages for
    those rather than inventing one.
    """

    name = "whatsapp"

    def __init__(self, *, token: str, phone_number_id: str, app_secret: str,
                 timeout_s: float = 30.0):
        self._token = token
        self._phone_number_id = phone_number_id
        self._secret = app_secret
        self._timeout = timeout_s

    # ------------------------------------------------------------ inbound

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        import hashlib
        import hmac

        header = headers.get("x-hub-signature-256") or headers.get("X-Hub-Signature-256")
        if not self._secret or not header:
            return False
        expected = "sha256=" + hmac.new(self._secret.encode(), body,
                                        hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, header)

    def parse(self, body: bytes, headers: Mapping[str, str]) -> list[Inbound]:
        import json

        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChannelError(f"whatsapp payload is not JSON: {exc}") from exc

        found: list[Inbound] = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                phone_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
                name = ((value.get("contacts") or [{}])[0].get("profile") or {}).get("name")
                for message in value.get("messages") or []:
                    inbound = self._one(message, phone_id, name)
                    if inbound is not None:
                        found.append(inbound)
                # `statuses` (sent/delivered/read) carries no message and
                # deliberately produces nothing.
        return found

    def _one(self, message: dict, phone_id: str, name: str | None) -> Inbound | None:
        sender = message.get("from")
        if not sender:
            return None
        kind = str(message.get("type") or "other")
        mapped = _MEDIA_KINDS.get(kind, "text" if kind == "text" else "other")

        text, media_id, mime = "", None, None
        if kind == "text":
            text = str((message.get("text") or {}).get("body") or "")
        elif kind == "interactive":
            # A button or list reply arrives as an id plus a title; the title
            # is what a person would have typed.
            interactive = message.get("interactive") or {}
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            text = str(reply.get("title") or "")
            mapped = "text"
        elif kind in ("image", "audio", "voice", "document", "sticker", "video"):
            media = message.get(kind) or {}
            media_id = media.get("id")
            mime = media.get("mime_type")
            text = str(media.get("caption") or "")
        elif kind == "location":
            location = message.get("location") or {}
            text = f"location: {location.get('latitude')},{location.get('longitude')}"
        elif kind == "button":
            text = str((message.get("button") or {}).get("text") or "")

        timestamp = message.get("timestamp")
        try:
            received = (datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
                        if timestamp else None)
        except (TypeError, ValueError, OSError):
            received = None

        return Inbound(
            channel=self.name,
            external_id=str(message.get("id") or ""),
            from_phone=str(sender),
            text=text,
            kind=mapped,  # type: ignore[arg-type]
            media_id=media_id,
            mime_type=mime,
            received_at=received,
            raw={"phone_number_id": phone_id, "name": name, "type": kind,
                 "message": message},
        )

    # ----------------------------------------------------------- outbound

    def send(self, message: Outbound) -> str:
        body = json.dumps({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_wa_id(normalise_phone(message.to_phone)),
            "type": "text",
            "text": {"preview_url": True, "body": message.text},
        }).encode()

        request = urllib.request.Request(
            f"{GRAPH}/{self._phone_number_id}/messages", data=body, method="POST",
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            # 131047 is the one worth naming: the 24-hour service window has
            # closed and only a template may be sent. It is a policy answer,
            # not a bug, and the follow-up scheduler is meant to prevent it.
            if "131047" in detail:
                raise ChannelError(
                    "the 24-hour service window is closed; only an approved "
                    f"template may be sent now ({detail[:120]})") from exc
            raise ChannelError(f"whatsapp refused the send ({exc.code}): {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ChannelError(f"could not reach WhatsApp: {exc}") from exc

        messages = payload.get("messages") or []
        return str(messages[0].get("id") or "") if messages else ""

    def download(self, media_id: str) -> tuple[bytes, str]:
        """Media in two hops: the id resolves to a URL, the URL to bytes.

        Not used by the wedge -- voice notes are transcribed by a `Transcriber`
        rather than by the channel -- but it is the shape any media support
        needs, and it is where the auth header must be repeated.
        """
        meta_request = urllib.request.Request(
            f"{GRAPH}/{media_id}",
            headers={"Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(meta_request, timeout=self._timeout) as response:
                info = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise ChannelError(f"could not resolve media {media_id}: {exc}") from exc

        url = info.get("url")
        if not url:
            raise ChannelError(f"no url for media {media_id}")
        media_request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(media_request, timeout=self._timeout) as response:
                return response.read(), str(info.get("mime_type") or "")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ChannelError(f"could not fetch media {media_id}: {exc}") from exc


# ------------------------------------------------- per-business channels


def for_business(conn: psycopg.Connection, *, business_id: str,
                 fallback: Channel) -> Channel:
    """The channel this business actually sends on.

    The environment holds one WhatsApp token, which is correct for one pilot
    and wrong the moment there are two: every business's replies would go out
    of the same number, over the same account. This reads the credentials back
    from the business's own rows instead, and falls back to the simulator when
    there is no integration at all -- which is the pilot's state, and the state
    a business is in until its WhatsApp onboarding finishes.

    `fallback` is a parameter rather than a hardcoded simulator because the
    caller is the one that knows what "no integration" should mean for it. The
    worker passes the simulator; a test passes a recorder.
    """
    from aisales import db, secrets

    row = conn.execute(
        """
        select external_account_id, status from business_integrations
         where business_id = %s and provider = 'whatsapp' and status <> 'revoked'
         order by created_at limit 1
        """,
        (business_id,),
    ).fetchone()
    if row is None:
        return fallback

    token = secrets.get(conn, business_id=business_id, provider="whatsapp",
                        secret_type="access_token")
    app_secret = secrets.get(conn, business_id=business_id, provider="whatsapp",
                             secret_type="app_secret")
    if not token or not app_secret:
        # Half-configured is not configured. Sending anyway would use the
        # platform's number, and the customer would get a reply from a business
        # they never contacted.
        return fallback

    return MetaCloudChannel(token=token, phone_number_id=row["external_account_id"],
                            app_secret=app_secret)


def record_send_failure(conn: psycopg.Connection, *, business_id: str,
                        provider: str, reason: str) -> None:
    """Mark an integration as failing, so a lapsed connection is visible.

    Requirement 6.6. Without this a revoked WhatsApp token produces a silent
    retry loop: every send fails, the job is retried on backoff, and nothing
    anywhere says the connection is dead. The dashboard reads this status.
    """
    conn.execute(
        """
        update business_integrations
           set status = 'error',
               -- The text cast is not decoration: jsonb_build_object is
               -- variadic "any", so without it Postgres cannot infer the
               -- parameter's type and refuses the statement outright. (No
               -- placeholder is written out in this comment on purpose --
               -- psycopg binds client-side and would count it as one.)
               metadata = metadata || jsonb_build_object('last_error', %s::text,
                                                         'last_error_at', now())
         where business_id = %s and provider = %s and status = 'active'
        """,
        (reason[:300], business_id, provider),
    )
