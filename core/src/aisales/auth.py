"""People, passwords and sessions.

`hashlib.scrypt` is memory-hard and in the standard library, so this needs no
dependency. Memory hardness is the property that matters: a GPU can compute
billions of SHA-256 hashes a second, but scrypt's cost is dominated by RAM,
which is the resource an attacker cannot parallelise cheaply.

The hash string carries its own parameters --

    scrypt$<n>$<r>$<p>$<salt-hex>$<hash-hex>

-- so the cost can be raised later without invalidating existing passwords:
`verify` reads the parameters from the stored value and `needs_rehash` says
which ones are below current.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

import psycopg

# 128 * N * r bytes of memory. At these values that is 16 MiB per hash, which
# is slow enough to matter to an attacker and fast enough (~50ms) not to matter
# to a sign-in.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32

SESSION_TTL_HOURS = 24 * 14
SESSION_BYTES = 32

#: Failed sign-ins allowed per bucket before the next one is refused.
RATE_LIMIT_ATTEMPTS = 8
RATE_LIMIT_WINDOW_MINUTES = 15

#: A value that is not a valid hash, used to spend the same work when the
#: account does not exist. Without it, an unknown address returns in a
#: microsecond and a known one in 50 milliseconds, and the difference is a
#: reliable oracle for which addresses have accounts.
_DUMMY = f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${'00' * 16}${'00' * KEY_LEN}"


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class Principal:
    """Who is acting, and on whose behalf.

    `business_id` is None for an operator who belongs to no business, which is
    the ordinary shape of an operator. `role` is then "operator" rather than a
    business role, so a caller that asks "may this person do X for their
    business" gets a value that fails that question rather than a plausible
    default.
    """

    user_id: str
    email: str
    name: str | None
    business_id: str | None
    business_slug: str | None
    role: str
    is_operator: bool = False


# ------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    if not password:
        raise AuthError("a password is required")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                             p=SCRYPT_P, dklen=KEY_LEN, maxmem=64 * 1024 * 1024)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time comparison, and constant-*work* when the user is absent.

    `stored=None` means "no such account". Rather than returning immediately,
    this verifies against a dummy so the timing of the two cases matches.
    """
    target = stored or _DUMMY
    try:
        scheme, n, r, p, salt_hex, hash_hex = target.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                                 n=int(n), r=int(r), p=int(p),
                                 dklen=len(hash_hex) // 2,
                                 maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError, MemoryError):
        return False
    # Both halves always run: no early return that would leak which part failed.
    matches = hmac.compare_digest(derived.hex(), hash_hex)
    return matches and stored is not None


def needs_rehash(stored: str) -> bool:
    try:
        scheme, n, r, p, _, _ = stored.split("$")
    except (ValueError, AttributeError):
        return True
    return scheme != "scrypt" or int(n) < SCRYPT_N or int(r) < SCRYPT_R


# -------------------------------------------------------------- sessions


def _fingerprint(token: str) -> str:
    """Sessions are stored hashed.

    A plain digest is right here, unlike for passwords: the token is 32 bytes
    of `secrets` randomness, so there is no dictionary to attack and nothing
    for a slow hash to buy.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def issue_session(conn: psycopg.Connection, user_id: str) -> str:
    token = secrets.token_urlsafe(SESSION_BYTES)
    conn.execute(
        """
        insert into sessions (user_id, token_hash, expires_at)
        values (%s, %s, now() + make_interval(hours => %s))
        """,
        (user_id, _fingerprint(token), SESSION_TTL_HOURS),
    )
    return token


def revoke_session(conn: psycopg.Connection, token: str) -> None:
    """Server-side sign-out. Clearing the cookie alone leaves a live token."""
    conn.execute(
        "update sessions set revoked_at = now() where token_hash = %s",
        (_fingerprint(token),),
    )


def principal_for(conn: psycopg.Connection, token: str) -> Principal | None:
    """The session's user and the business it may act for.

    Returns None for an unknown, expired or revoked token -- one answer for
    all three, so a caller cannot distinguish "wrong token" from "expired".
    """
    if not token:
        return None
    row = conn.execute(
        """
        select u.id as user_id, u.email, u.name, u.disabled_at, u.is_operator,
               b.id as business_id, b.slug, m.role
          from sessions s
          join users u on u.id = s.user_id
          -- LEFT, not INNER: an operator need not belong to any business, and
          -- an inner join would resolve them to NULL -- the same answer as an
          -- invalid token, so they could never sign in at all.
          left join business_members m on m.user_id = u.id
          left join businesses b on b.id = m.business_id
         where s.token_hash = %s
           and s.revoked_at is null
           and s.expires_at > now()
           and u.disabled_at is null
           -- A user with no business and no operator flag is nobody: the
           -- membership is what makes an ordinary account mean anything.
           and (m.id is not null or u.is_operator)
         order by m.created_at
         limit 1
        """,
        (_fingerprint(token),),
    ).fetchone()
    if row is None:
        return None
    business_id = str(row["business_id"]) if row["business_id"] else None
    return Principal(
        user_id=str(row["user_id"]), email=row["email"], name=row["name"],
        business_id=business_id,
        business_slug=row["slug"] if business_id else None,
        role=row["role"] or ("operator" if row["is_operator"] else "staff"),
        is_operator=bool(row["is_operator"]),
    )


# ---------------------------------------------------------- rate limiting


def record_attempt(conn: psycopg.Connection, bucket: str, *, succeeded: bool) -> None:
    conn.execute("insert into auth_attempts (bucket, succeeded) values (%s, %s)",
                 (bucket, succeeded))


def too_many_attempts(conn: psycopg.Connection, bucket: str) -> bool:
    """Whether this bucket has burned through its allowance.

    Counted in the database rather than in memory, so it survives a restart and
    works across more than one API process.

    ponytail: no cleanup job, so auth_attempts grows without bound -- one row
    per sign-in attempt. At a few thousand rows a day that is years before it
    matters. Upgrade path is a scheduled `delete from auth_attempts where
    created_at < now() - interval '30 days'`, which belongs with whatever
    retention job purge_conversations_before ends up in.
    """
    row = conn.execute(
        """
        select count(*) as n from auth_attempts
         where bucket = %s and not succeeded
           and created_at > now() - make_interval(mins => %s)
        """,
        (bucket, RATE_LIMIT_WINDOW_MINUTES),
    ).fetchone()
    return row["n"] >= RATE_LIMIT_ATTEMPTS


# ------------------------------------------------------------- the oracle


def sign_in(conn: psycopg.Connection, email: str, password: str, *,
            source: str = "") -> tuple[str | None, str]:
    """Returns (token, reason). Token is None when the sign-in failed.

    Every failure returns the same reason to the caller. Distinguishing "no
    such account" from "wrong password" tells an attacker which addresses are
    registered, which is half of what they need.
    """
    email = (email or "").strip().lower()
    account_bucket = f"account:{email}"
    ip_bucket = f"ip:{source}"

    if too_many_attempts(conn, account_bucket) or (source and too_many_attempts(conn, ip_bucket)):
        return None, "too many attempts"

    row = conn.execute(
        "select id, password_hash from users where email = %s and disabled_at is null",
        (email,),
    ).fetchone()

    ok = verify_password(password, row["password_hash"] if row else None)
    record_attempt(conn, account_bucket, succeeded=ok)
    if source:
        record_attempt(conn, ip_bucket, succeeded=ok)

    if not ok or row is None:
        return None, "invalid credentials"

    if needs_rehash(row["password_hash"]):
        conn.execute("update users set password_hash = %s where id = %s",
                     (hash_password(password), row["id"]))

    return issue_session(conn, str(row["id"])), ""


# ------------------------------------------------------------- provisioning


def create_user(conn: psycopg.Connection, email: str, password: str,
                *, name: str | None = None, is_operator: bool = False) -> str:
    """Add a user. Raises AuthError if the address is taken or the password
    is unusable -- both are mistakes worth stopping on, not conditions to
    handle quietly."""
    clean = (email or "").strip().lower()
    if "@" not in clean:
        raise AuthError(f"{email!r} is not an email address")
    existing = conn.execute("select 1 from users where email = %s", (clean,)).fetchone()
    if existing:
        raise AuthError(f"{clean} already has an account")
    row = conn.execute(
        "insert into users (email, name, password_hash, is_operator) "
        "values (%s, %s, %s, %s) returning id",
        (clean, name or None, hash_password(password), is_operator),
    ).fetchone()
    return str(row["id"])


def add_member(conn: psycopg.Connection, user_id: str, business_id: str,
               *, role: str = "owner") -> None:
    if role not in ("owner", "staff"):
        raise AuthError(f"{role!r} is not a role")
    conn.execute(
        """
        insert into business_members (user_id, business_id, role) values (%s, %s, %s)
        on conflict (business_id, user_id) do update set role = excluded.role
        """,
        (user_id, business_id, role),
    )
