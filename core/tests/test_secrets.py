"""A credential must never be able to reach `businesses.settings`.

That column is **public by design**: it holds the tone, the approved facts, the
follow-up policy -- everything the dashboard shows the owner -- and the settings
endpoint returns it verbatim to any member of the business. There is no
redaction step and there is not meant to be one, because a redaction step is a
list somebody has to remember to add to.

The defence is therefore structural, in three parts, and this file tests all
three rather than trusting any one of them:

1. `business_secrets` exists, so there is somewhere else to put a credential.
2. The settings endpoint writes through an allow-list, so an unknown key is
   refused by name instead of stored.
3. This file, which fails if a secret-shaped key is added to that allow-list.

Part 3 is the one that catches the mistake nobody makes on purpose: adding
`paystack_secret_key` to `EDITABLE` because the settings form needs it.
"""

from __future__ import annotations

import re

import pytest

from aisales_api import views

#: Names that mean "this is a credential" in every convention this codebase
#: uses or is likely to meet. Matched case-insensitively against every key in
#: the settings allow-list and against the contents of a stored blob.
SECRET_SHAPED = re.compile(
    r"secret|token|password|passwd|api[_-]?key|private[_-]?key|credential"
    r"|access[_-]?key|client[_-]?secret|webhook[_-]?secret|signature|bearer",
    re.IGNORECASE,
)


def test_the_settings_allow_list_holds_nothing_secret_shaped() -> None:
    """A credential added to `EDITABLE` would be stored in a column the
    settings endpoint returns verbatim, with no redaction anywhere."""
    offenders = sorted(k for k in views.EDITABLE if SECRET_SHAPED.search(k))
    assert not offenders, (
        f"{offenders} are writable through the settings endpoint, which returns "
        f"businesses.settings in full to every member. Credentials belong in "
        f"business_secrets -- see aisales.secrets."
    )


def test_an_unknown_key_is_refused_by_name(dsn: str, business) -> None:
    """Refused, not dropped. A settings form that accepts a key and silently
    ignores it is one nobody can trust to have saved anything."""
    with __import__("aisales").db.connect(dsn) as conn:
        updated, refused = views.update_settings(
            conn, business.id, {"paystack_secret_key": "sk_live_oops", "tone": "warm"})

    assert refused == ["paystack_secret_key"]
    assert "paystack_secret_key" not in updated, "a refused key was stored anyway"
    assert updated["tone"] == "warm", "the valid key in the same patch was dropped too"


def test_a_refused_key_never_reaches_the_row(dsn: str, business) -> None:
    """The patch is filtered before the UPDATE, not after it."""
    from aisales import db

    with db.connect(dsn) as conn:
        views.update_settings(conn, business.id,
                              {"whatsapp_token": "EAAG-nope", "tone": "warm"})
        stored = conn.execute("select settings from businesses where id = %s",
                              (business.id,)).fetchone()["settings"]

    assert "whatsapp_token" not in stored
    assert "EAAG-nope" not in str(stored)


def test_the_settings_response_carries_no_secret_field(dsn: str, business,
                                                       monkeypatch) -> None:
    """Requirement 7.7. Even with a secret stored for this business, the
    settings payload must not contain it -- not encrypted, not truncated, not
    as a key with a null value."""
    from aisales import db, secrets

    monkeypatch.setenv("AISALES_SECRET_KEY", "test-key")
    marker = "EAAG-do-not-return-me"
    with db.tenant(dsn, business.id) as conn:
        secrets.put(conn, business_id=business.id, provider="whatsapp",
                    secret_type="access_token", value=marker)
        payload = views.settings(conn, business.id)

    body = str(payload)
    assert marker not in body
    assert "access_token" not in body
    assert "ciphertext" not in body
    assert set(payload) == {"id", "slug", "name", "settings"}, (
        "the settings payload gained a field; check it is not a credential"
    )


def test_storing_an_empty_secret_is_refused(dsn: str, business, monkeypatch) -> None:
    """An empty credential is indistinguishable from a missing one at the
    point of use, and the difference matters: one means "not connected yet"
    and the other means "connected, and nothing works"."""
    from aisales import db, secrets

    monkeypatch.setenv("AISALES_SECRET_KEY", "test-key")
    with db.tenant(dsn, business.id) as conn:
        with pytest.raises(secrets.SecretError, match="empty"):
            secrets.put(conn, business_id=business.id, provider="paystack",
                        secret_type="secret_key", value="")


def test_rotation_re_encrypts_rather_than_invalidating(dsn: str, business,
                                                       monkeypatch) -> None:
    """A key change must be walkable. `key_version` is what makes it a pass
    over the rows instead of a flag day where everything stops decrypting."""
    from aisales import db, secrets

    monkeypatch.setenv("AISALES_SECRET_KEY", "test-key")
    with db.tenant(dsn, business.id) as conn:
        secrets.put(conn, business_id=business.id, provider="paystack",
                    secret_type="secret_key", value="sk_test_rotate_me")
        # Stand in for a row written before the version was bumped.
        conn.execute("update business_secrets set key_version = 0")
        assert secrets.rotate(conn) == 1
        assert secrets.get(conn, business_id=business.id, provider="paystack",
                           secret_type="secret_key") == "sk_test_rotate_me"
        assert conn.execute("select key_version from business_secrets"
                            ).fetchone()["key_version"] == secrets.CURRENT_KEY_VERSION
