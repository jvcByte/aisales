"""The messaging boundary. No database, no network.

The phone table is the important part. Customer identity is the normalised
number, so a normaliser that accepts one spelling and rejects another silently
splits one customer into two -- two conversations, two lead records, and an
agent that has forgotten what it was told ten minutes ago.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from aisales.channels import (
    ChannelError,
    Inbound,
    MetaCloudChannel,
    Outbound,
    SimulatorChannel,
    normalise_phone,
    to_wa_id,
)

# Every one of these is the same Lagos mobile number, written the way people
# and platforms actually write it.
SAME_NUMBER = [
    "08031234567",
    "0803 123 4567",
    "0803-123-4567",
    "+2348031234567",
    "2348031234567",
    "2348031234567@s.whatsapp.net",
    "+234 (803) 123-4567",
    "  +234-803-123-4567  ",
    "8031234567",  # trunk zero omitted, common in writing
]

NOT_NUMBERS = [
    "",
    "   ",
    "abc",
    "0803",                 # too short
    "0803123456",           # ten digits starting 08 -- not a national form
    "080312345678",         # twelve digits
    "06031234567",          # 060 is not an allocated mobile range
    "447700900123",         # UK
    "+1 555 0100",          # US
    "23480312345678",       # one digit too many for +234
]


@pytest.mark.parametrize("raw", SAME_NUMBER)
def test_every_spelling_is_one_customer(raw: str) -> None:
    assert normalise_phone(raw) == "+2348031234567"


@pytest.mark.parametrize("raw", NOT_NUMBERS)
def test_foreign_and_malformed_numbers_are_refused(raw: str) -> None:
    """Raising is the correct outcome: a guessed country code messages a
    stranger, and the failure would look like success."""
    with pytest.raises(ValueError):
        normalise_phone(raw)


def test_non_string_is_refused() -> None:
    with pytest.raises(ValueError):
        normalise_phone(None)  # type: ignore[arg-type]


def test_wa_id_round_trips() -> None:
    """Meta sends '2348031234567' and expects the same form back."""
    assert to_wa_id("+2348031234567") == "2348031234567"
    assert normalise_phone(to_wa_id("+2348031234567")) == "+2348031234567"


def test_inbound_normalises_its_sender() -> None:
    msg = Inbound(channel="simulator", external_id="1",
                  from_phone="0803 123 4567", text="Abeg how much?")
    assert msg.phone_e164 == "+2348031234567"


# ------------------------------------------------------------ the simulator


def test_simulator_parses_a_message() -> None:
    channel = SimulatorChannel()
    body = json.dumps({"from": "08031234567", "text": "Abeg how much?",
                       "external_id": "fixed-id"}).encode()
    messages = channel.parse(body, {})
    assert len(messages) == 1
    assert messages[0].text == "Abeg how much?"
    assert messages[0].phone_e164 == "+2348031234567"
    assert messages[0].external_id == "fixed-id"
    assert messages[0].channel == "simulator"


def test_simulator_delivery_without_a_message_yields_none() -> None:
    """A read receipt carries no message and must not create one. Same rule
    as a real channel's status callbacks."""
    channel = SimulatorChannel()
    for payload in ({"from": "08031234567"}, {"from": "08031234567", "text": "  "},
                    {"text": "hello"}):
        assert channel.parse(json.dumps(payload).encode(), {}) == []


def test_simulator_rejects_broken_json() -> None:
    with pytest.raises(ChannelError):
        SimulatorChannel().parse(b"{not json", {})


def test_simulator_mints_an_external_id_when_absent() -> None:
    """Without an id there is no de-duplication key, so one is minted rather
    than the message being dropped."""
    channel = SimulatorChannel()
    raw = json.dumps({"from": "08031234567", "text": "hi"}).encode()
    first = channel.parse(raw, {})[0].external_id
    second = channel.parse(raw, {})[0].external_id
    assert first and second and first != second


def test_simulator_send_returns_an_id() -> None:
    assert SimulatorChannel().send(Outbound(to_phone="+2348031234567", text="hi"))


# ------------------------------------------------------------------ WhatsApp

SECRET = "app-secret"
META = MetaCloudChannel(token="t", phone_number_id="123", app_secret=SECRET)


def _delivery(messages: list[dict], *, contacts=None) -> bytes:
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "123", "display_phone_number": "234"},
            "contacts": contacts or [{"profile": {"name": "Ada"}, "wa_id": "2348031234567"}],
            "messages": messages,
        }}]}],
    }).encode()


def _text_message(body: str = "Abeg how much?") -> dict:
    return {"from": "2348031234567", "id": "wamid.TEST1", "timestamp": "1759000000",
            "type": "text", "text": {"body": body}}


def _meta_signature(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_meta_verifies_its_own_signature() -> None:
    body = _delivery([_text_message()])
    assert META.verify(body, {"x-hub-signature-256": _meta_signature(body)})


def test_meta_rejects_a_paystack_style_sha512_signature() -> None:
    """The trap this adapter exists to avoid. Meta signs with SHA-256 and
    Paystack with SHA-512 over the same body, and the two verifiers look alike
    enough that a copy between them produces one that never accepts anything --
    or, loosened until it passes, one that accepts everything."""
    body = _delivery([_text_message()])
    paystack_style = hmac.new(SECRET.encode(), body, hashlib.sha512).hexdigest()
    assert not META.verify(body, {"x-hub-signature-256": paystack_style})
    assert not META.verify(body, {"x-hub-signature-256": "sha256=" + paystack_style})
    # And the bare digest without its prefix is not the same string.
    assert not META.verify(body, {"x-hub-signature-256": _meta_signature(body)[7:]})


def test_meta_rejects_a_signature_from_another_secret() -> None:
    """A correct signature is only correct for the secret that made it."""
    body = _delivery([_text_message()])
    other = MetaCloudChannel(token="t", phone_number_id="1", app_secret="different")
    signed_by_other = "sha256=" + hmac.new(b"different", body,
                                           hashlib.sha256).hexdigest()

    assert not META.verify(body, {"x-hub-signature-256": signed_by_other})
    assert other.verify(body, {"x-hub-signature-256": signed_by_other})
    # And each accepts only its own.
    assert other.verify(body, {"x-hub-signature-256": _meta_signature(body)}) is False


def test_meta_refuses_verification_with_no_secret_configured() -> None:
    """An unconfigured secret must fail closed, not accept everything."""
    body = _delivery([_text_message()])
    unconfigured = MetaCloudChannel(token="t", phone_number_id="1", app_secret="")
    assert not unconfigured.verify(body, {"x-hub-signature-256": "sha256=anything"})


def test_meta_parses_a_text_message() -> None:
    found = META.parse(_delivery([_text_message()]), {})
    assert len(found) == 1
    message = found[0]
    assert message.text == "Abeg how much?"
    assert message.external_id == "wamid.TEST1"
    assert message.phone_e164 == "+2348031234567"
    assert message.raw["phone_number_id"] == "123"
    assert message.raw["name"] == "Ada"
    assert message.received_at is not None


def test_meta_parses_several_messages_in_one_delivery() -> None:
    """Meta batches. Three messages in one POST is three turns' worth of input,
    not one."""
    first, second, third = (_text_message("one"), _text_message("two"), _text_message("three"))
    first["id"], second["id"], third["id"] = "w1", "w2", "w3"
    found = META.parse(_delivery([first, second, third]), {})
    assert [m.text for m in found] == ["one", "two", "three"]


def test_meta_reads_a_voice_note_as_media_not_text() -> None:
    """A voice note has no body until something transcribes it. Reporting it
    as empty text would have the agent answer nothing."""
    found = META.parse(_delivery([{
        "from": "2348031234567", "id": "wamid.V1", "timestamp": "1759000001",
        "type": "audio", "audio": {"id": "media-1", "mime_type": "audio/ogg"}}]), {})
    assert len(found) == 1
    assert found[0].kind == "voice"
    assert found[0].media_id == "media-1"
    assert found[0].mime_type == "audio/ogg"
    assert found[0].text == ""


def test_meta_reads_an_image_caption() -> None:
    found = META.parse(_delivery([{
        "from": "2348031234567", "id": "wamid.I1", "timestamp": "1759000002",
        "type": "image", "image": {"id": "m2", "mime_type": "image/jpeg",
                                   "caption": "This one, in black"}}]), {})
    assert found[0].kind == "image"
    assert found[0].text == "This one, in black"


def test_meta_reads_a_button_reply_as_text() -> None:
    """A tapped button arrives as an id and a title. The title is what a
    person would have typed, and is what the agent should read."""
    found = META.parse(_delivery([{
        "from": "2348031234567", "id": "wamid.B1", "timestamp": "1759000003",
        "type": "interactive",
        "interactive": {"type": "button_reply",
                        "button_reply": {"id": "yes", "title": "Yes please"}}}]), {})
    assert found[0].text == "Yes please"


def test_a_status_receipt_produces_no_messages() -> None:
    """Read and delivery receipts arrive on the same endpoint. Turning one into
    a message would have the agent answer a receipt."""
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "123"},
            "statuses": [{"id": "wamid.TEST1", "status": "read",
                          "timestamp": "1759000004", "recipient_id": "2348031234567"}],
        }}]}],
    }).encode()
    assert META.parse(body, {}) == []


def test_a_message_with_no_sender_is_dropped_rather_than_guessed_at() -> None:
    """Without a `from` there is no customer to attribute it to, and a guessed
    identity means answering the wrong person."""
    assert META._one({"from": "2348031234567", "type": "text",
                      "text": {"body": "hi"}}, "123", None) is not None
    assert META._one({"type": "text", "text": {"body": "hi"}}, "123", None) is None
