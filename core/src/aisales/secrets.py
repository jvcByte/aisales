"""Per-business credentials, encrypted at rest.

Two decisions carry this file, and both are about what happens on failure.

**The key is a bound parameter, never statement text.** `pgp_sym_encrypt` takes
the key as an argument, so passing it as `%s` keeps it out of `pg_stat_activity`,
out of the query text Postgres logs, and out of any statement log. Interpolating
it would put a live credential in three places at once.

**A decryption failure raises; it never falls back.** The tempting fallback is
to the platform's own key -- and then one business's WhatsApp messages go out
over another business's account. There is no safe default for "whose credential
is this", so there is no default. `pgp_sym_decrypt` raises rather than
returning NULL when the key is wrong, which is caught and re-raised as
`SecretError` naming the likely cause.

What this does not protect against, stated plainly: the key travels to the
database on every read, so a Postgres configured with `log_statement = 'all'`
or `log_min_duration_statement = 0` writes it to the log. `SECRET_KEY`
documented in `.env.example` names both settings for that reason. Encryption at
rest defends against a stolen backup or a leaked `pg_dump`; it does not defend
against a host that is already compromised, and claiming otherwise would be the
more dangerous thing to write down.
"""

from __future__ import annotations

import os

import psycopg

#: Bumped when the platform key changes. Rows keep the version they were
#: written with, so rotation is a walk over the old rows rather than a flag day
#: where every secret stops decrypting at once.
CURRENT_KEY_VERSION = 1


class SecretError(RuntimeError):
    """A secret could not be read. Never caught and defaulted over."""


def platform_key() -> str:
    """The key every business's secrets are encrypted with.

    Deliberately not per-business: a per-business key has to be stored
    somewhere, and everywhere it could be stored is either the same database
    (no gain) or a file that becomes the real single secret (no gain, more
    parts). The upgrade path when a customer requires it is a KMS-held key,
    which is a change to this function and nothing else.
    """
    key = os.environ.get("AISALES_SECRET_KEY", "")
    if not key:
        raise SecretError(
            "AISALES_SECRET_KEY is not set, so no business secret can be "
            "encrypted or read. Set it in .env; changing it later makes "
            "existing secrets unreadable, which is what key_version is for."
        )
    return key


def put(conn: psycopg.Connection, *, business_id: str, provider: str,
        secret_type: str, value: str) -> None:
    """Store or replace one secret, inside the owning tenant's transaction.

    The caller is responsible for that: `conn` must carry the business's tenant
    context, or row level security refuses the write.
    """
    if not value:
        raise SecretError(f"refusing to store an empty {secret_type} for {provider}")
    conn.execute(
        """
        insert into business_secrets
               (business_id, provider, secret_type, ciphertext, key_version, rotated_at)
        values (%s, %s, %s, pgp_sym_encrypt(%s, %s), %s, now())
        on conflict (business_id, provider, secret_type) do update
           set ciphertext = excluded.ciphertext,
               key_version = excluded.key_version,
               rotated_at = now()
        """,
        (business_id, provider, secret_type, value, platform_key(),
         CURRENT_KEY_VERSION),
    )


def get(conn: psycopg.Connection, *, business_id: str, provider: str,
        secret_type: str) -> str | None:
    """One secret, or None when it was never stored.

    None is a real answer -- "this business has not connected WhatsApp yet" --
    and is different from a failure to decrypt, which raises. Collapsing the
    two is how a missing credential becomes a shared one.
    """
    try:
        row = conn.execute(
            """
            select pgp_sym_decrypt(ciphertext, %s) as value
              from business_secrets
             where business_id = %s and provider = %s and secret_type = %s
            """,
            (platform_key(), business_id, provider, secret_type),
        ).fetchone()
    except psycopg.errors.ExternalRoutineInvocationException as exc:
        # pgp_sym_decrypt raises "Wrong key or corrupt data" rather than
        # returning NULL, so this -- not a None check -- is where a rotated or
        # mistyped AISALES_SECRET_KEY surfaces. Translated because the raw
        # error names neither the key nor the row, and the person reading it
        # will be looking at a stack trace from a WhatsApp send.
        raise SecretError(
            f"could not decrypt {provider}/{secret_type}: the row was encrypted "
            f"with a different AISALES_SECRET_KEY"
        ) from exc
    return None if row is None else str(row["value"])


def rotate(conn: psycopg.Connection, *, business_id: str | None = None) -> int:
    """Re-encrypt rows written under an older key. Returns how many moved.

    Runs inside whatever tenant context the caller opened -- pass `business_id`
    only to document intent; the policy is what actually scopes it, which is
    the point of doing rotation as an ordinary read and write rather than as a
    cross-tenant sweep.
    """
    key = platform_key()
    rows = conn.execute(
        "select id, ciphertext, key_version from business_secrets "
        "where key_version <> %s", (CURRENT_KEY_VERSION,)
    ).fetchall()
    for row in rows:
        conn.execute(
            "update business_secrets set ciphertext = pgp_sym_encrypt("
            "  pgp_sym_decrypt(ciphertext, %s), %s), key_version = %s, rotated_at = now() "
            "where id = %s",
            (key, key, CURRENT_KEY_VERSION, row["id"]),
        )
    return len(rows)
