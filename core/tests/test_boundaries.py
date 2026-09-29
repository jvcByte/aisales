"""The five places where a query legitimately crosses tenants.

Every other read in this codebase runs inside one business's tenant. These are
the exceptions, and an exception that is not named and tested is just a leak
nobody has noticed yet. Each one below is asserted twice: that it resolves the
right business, and that an identifier it does not know resolves to *nothing*
rather than to a default.

That second assertion is the one that matters. "Unknown phone number" falling
back to the pilot business is a plausible-looking convenience that silently
routes one business's customers into another's inbox.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest

from aisales import db, secrets

# `dsn` comes from conftest.


def _business(dsn: str, slug: str) -> db.Business:
    with db.connect(dsn) as conn:
        row = conn.execute(
            "insert into businesses (slug, name, settings) values (%s, %s, '{}') "
            "returning id, slug, name, settings", (slug, f"Shop {slug}")).fetchone()
    return db.Business.from_row(row)


def _integrate(dsn: str, business_id: str, phone_number_id: str, *,
               status: str = "active") -> None:
    with db.connect(dsn) as conn:
        conn.execute(
            """
            insert into business_integrations
                   (business_id, provider, external_account_id, status)
            values (%s, 'whatsapp', %s, %s)
            """,
            (business_id, phone_number_id, status),
        )


@pytest.fixture
def shop(dsn: str):
    tag = uuid.uuid4().hex[:8]
    business = _business(dsn, f"bnd-{tag}")
    yield business
    with db.connect(dsn) as conn:
        conn.execute("delete from businesses where id = %s", (business.id,))


# --------------------------------------------------- boundary 1: whatsapp


def test_a_phone_number_resolves_to_its_own_business(dsn: str, shop) -> None:
    number = f"pn-{uuid.uuid4().hex[:8]}"
    _integrate(dsn, shop.id, number)
    with db.connect(dsn) as conn:
        assert db.resolve_whatsapp_tenant(conn, number) == shop.id


def test_an_unknown_phone_number_resolves_to_nothing(dsn: str, shop) -> None:
    """Not to `shop`, even though it is the only business with an integration.

    A "sole business" fallback here is the single most dangerous convenience in
    a multi-tenant webhook: it makes every unrouteable delivery land somewhere,
    and the somewhere is arbitrary.
    """
    with db.connect(dsn) as conn:
        assert db.resolve_whatsapp_tenant(conn, "pn-does-not-exist") is None
        assert db.resolve_whatsapp_tenant(conn, "") is None
        assert db.resolve_whatsapp_tenant(conn, None) is None


def test_a_revoked_integration_stops_resolving(dsn: str, shop) -> None:
    """A lapsed WhatsApp connection must not keep receiving traffic."""
    number = f"pn-{uuid.uuid4().hex[:8]}"
    _integrate(dsn, shop.id, number, status="revoked")
    with db.connect(dsn) as conn:
        assert db.resolve_whatsapp_tenant(conn, number) is None


def test_two_businesses_cannot_claim_one_number(dsn: str, shop) -> None:
    """The unique index, which is what makes the resolver single-valued.

    Without it the resolution above would be a race between two rows and the
    answer would depend on disk order -- an ambiguity that no amount of care in
    the resolver could resolve correctly.
    """
    number = f"pn-{uuid.uuid4().hex[:8]}"
    _integrate(dsn, shop.id, number)
    other = _business(dsn, f"bnd2-{uuid.uuid4().hex[:8]}")
    try:
        import psycopg

        with pytest.raises(psycopg.errors.UniqueViolation):
            _integrate(dsn, other.id, number)
    finally:
        with db.connect(dsn) as conn:
            conn.execute("delete from businesses where id = %s", (other.id,))


# ---------------------------------------------------- boundary 2: paystack


def test_a_reference_resolves_to_the_business_that_owns_the_payment(
        dsn: str, shop) -> None:
    reference = f"ref-{uuid.uuid4().hex[:10]}"
    with db.tenant(dsn, shop.id) as conn:
        customer = conn.execute(
            "insert into customers (business_id, phone_e164) values (%s, %s) returning id",
            (shop.id, "+2348031234567")).fetchone()
        order = conn.execute(
            "insert into orders (business_id, customer_id, reference, total_kobo) "
            "values (%s, %s, %s, 2500000) returning id",
            (shop.id, customer["id"], f"ORD-{reference}")).fetchone()
        conn.execute(
            "insert into payments (business_id, order_id, paystack_reference, "
            "amount_kobo, status) values (%s, %s, %s, 2500000, 'pending')",
            (shop.id, order["id"], reference))

    with db.connect(dsn) as conn:
        assert db.resolve_payment_tenant(conn, reference) == shop.id
        assert db.resolve_payment_tenant(conn, "ref-never-seen") is None


# -------------------------------------------------  boundaries 3 and 4: queue


def test_claim_sees_every_tenant_and_finish_sees_one(dsn: str, shop) -> None:
    """The queue's exception is exactly two functions wide.

    `claim` and `reap` must see every business, because a worker takes whatever
    is next and does not know whose it is until it has it. Everything after the
    claim happens inside that job's tenant -- so a worker holding one job cannot
    finish, fail or heartbeat another business's.
    """
    other = _business(dsn, f"bnd3-{uuid.uuid4().hex[:8]}")
    try:
        q = db.Queue(dsn)
        with db.tenant(dsn, shop.id) as conn:
            mine = q.submit("agent_turn", business_id=shop.id, conn=conn)
        with db.tenant(dsn, other.id) as conn:
            theirs = q.submit("agent_turn", business_id=other.id, conn=conn)

        claimed = [q.claim("worker-a"), q.claim("worker-a")]
        assert all(c is not None for c in claimed), "claim could not see both tenants"
        assert {c.business_id for c in claimed} == {shop.id, other.id}
        assert {c.id for c in claimed} == {mine, theirs}
    finally:
        with db.connect(dsn) as conn:
            conn.execute("delete from businesses where id = %s", (other.id,))


# ----------------------------------------------------- boundary 5: operator


def test_the_operator_connection_refuses_without_an_explicit_dsn(
        dsn: str, monkeypatch) -> None:
    """The business API runs with no operator DSN set, and then has no way to
    open one -- not a disabled one, not a restricted one, none."""
    monkeypatch.delenv(db.OPERATOR_DSN_VAR, raising=False)
    with pytest.raises(RuntimeError, match=db.OPERATOR_DSN_VAR):
        with db.operator():
            pass


def test_exactly_one_api_module_may_open_the_operator_connection() -> None:
    """Requirement 8.3, narrowed deliberately when the console was built.

    This used to assert that *no* module in the API reached for the
    cross-tenant connection. That was right while there was no console, and
    wrong the moment there was one: somebody has to onboard a business, and
    the CLI cannot be the answer for a running deployment.

    So the assertion changed shape rather than going away. It now names the
    one file allowed to hold that connection, which is a stronger claim than
    the old one in the way that matters -- a *second* file reaching for it
    still fails here, and the reviewer's question is no longer "is this one of
    forty modules?" but "is this the one module?".
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "api" / "src"
    assert root.is_dir(), f"the API package moved: {root}"

    allowed = {"aisales_api/operator.py"}
    offenders = sorted({
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "db.operator(" in path.read_text()
        or db.OPERATOR_DSN_VAR in path.read_text()
    })
    assert set(offenders) <= allowed, (
        "these modules reach for the cross-tenant connection and are not the "
        "one that is allowed to: "
        + ", ".join(sorted(set(offenders) - allowed))
    )
    assert set(offenders) == allowed, (
        "the operator console no longer opens a cross-tenant connection at all "
        f"(found {offenders or 'nothing'}) -- either it moved, or it is doing "
        "its work through a business-scoped connection it should not have"
    )


def test_every_operator_route_is_gated_on_the_operator_dependency() -> None:
    """A route added later without the guard fails here, not in production.

    The console reads across every tenant, so a missing dependency is not a
    missing feature -- it is every business's data behind an ordinary session.
    Checking the dependency set per route is the only way to notice, since the
    route still works perfectly without it.
    """
    from aisales_api import operator as operator_mod

    assert operator_mod.build_router, "the console module moved"
    source = pathlib.Path(operator_mod.__file__).read_text()
    decorated = [line for line in source.splitlines() if line.strip().startswith("@router.")]
    guarded = [line for line in source.splitlines() if "current_operator" in line]
    assert decorated, "no routes found -- did the decorator style change?"
    # Every route decorator must be followed by a signature carrying the guard;
    # counting them is enough to catch a route added without one.
    assert len(guarded) >= len(decorated), (
        f"{len(decorated)} operator routes but only {len(guarded)} references to "
        f"current_operator -- a route is reachable without being an operator"
    )


# ----------------------------------------------------------- boundary 5b: secrets


def test_a_secret_round_trips_and_is_unreadable_without_the_key(
        dsn: str, shop, monkeypatch) -> None:
    """Encryption at rest, and the failure mode that matters.

    A wrong key must raise. `pgp_sym_decrypt` returns NULL rather than raising,
    so a caller that treated NULL as "no secret configured" would silently fall
    back to whatever default it had -- which is how one business's messages go
    out over another's account.
    """
    monkeypatch.setenv("AISALES_SECRET_KEY", "key-one")
    with db.tenant(dsn, shop.id) as conn:
        secrets.put(conn, business_id=shop.id, provider="whatsapp",
                    secret_type="access_token", value="EAAG-secret-value")
        assert secrets.get(conn, business_id=shop.id, provider="whatsapp",
                           secret_type="access_token") == "EAAG-secret-value"
        assert secrets.get(conn, business_id=shop.id, provider="whatsapp",
                           secret_type="never_stored") is None

        monkeypatch.setenv("AISALES_SECRET_KEY", "key-two")
        with pytest.raises(secrets.SecretError, match="different AISALES_SECRET_KEY"):
            secrets.get(conn, business_id=shop.id, provider="whatsapp",
                        secret_type="access_token")


def test_the_plaintext_never_reaches_the_table(dsn: str, shop, monkeypatch) -> None:
    """The value must not be findable in the stored bytes, in the settings
    blob, or in anything else a query without the key could return."""
    monkeypatch.setenv("AISALES_SECRET_KEY", "key-one")
    marker = f"plaintext-{uuid.uuid4().hex[:12]}"
    with db.tenant(dsn, shop.id) as conn:
        secrets.put(conn, business_id=shop.id, provider="paystack",
                    secret_type="secret_key", value=marker)

    with db.connect(dsn) as conn:
        row = conn.execute("select ciphertext from business_secrets").fetchall()
        assert row, "the secret was not stored at all"
        assert all(marker.encode() not in r["ciphertext"] for r in row)


# ------------------------------------------------ boundary 6: outbound routing


def test_a_business_with_no_integration_sends_on_the_fallback(dsn: str, shop,
                                                              monkeypatch) -> None:
    """The pilot's state, and every business's state until WhatsApp onboarding
    finishes: no integration, so the simulator -- not a shared WhatsApp
    number."""
    from aisales import channels

    monkeypatch.setenv("AISALES_SECRET_KEY", "test-key")
    fallback = channels.SimulatorChannel()
    with db.tenant(dsn, shop.id) as conn:
        assert channels.for_business(conn, business_id=shop.id,
                                     fallback=fallback) is fallback


def test_a_half_configured_integration_falls_back_rather_than_sending(
        dsn: str, shop, monkeypatch) -> None:
    """An integration row with no stored token is not a configured business.

    Sending anyway would use the platform's number, and the customer would get
    a reply from a business they never messaged.
    """
    from aisales import channels, secrets

    monkeypatch.setenv("AISALES_SECRET_KEY", "test-key")
    _integrate(dsn, shop.id, f"pn-{uuid.uuid4().hex[:8]}")

    fallback = channels.SimulatorChannel()
    with db.tenant(dsn, shop.id) as conn:
        assert channels.for_business(conn, business_id=shop.id,
                                     fallback=fallback) is fallback

        # With both credentials present it becomes a real WhatsApp channel.
        secrets.put(conn, business_id=shop.id, provider="whatsapp",
                    secret_type="access_token", value="EAAG-token")
        secrets.put(conn, business_id=shop.id, provider="whatsapp",
                    secret_type="app_secret", value="app-secret")
        channel = channels.for_business(conn, business_id=shop.id, fallback=fallback)
    assert channel is not fallback
    assert channel.name == "whatsapp"


def test_one_businesses_channel_is_not_anothers(dsn: str, shop, monkeypatch) -> None:
    """Two businesses, two numbers. The whole point of resolving per turn."""
    from aisales import channels, secrets

    monkeypatch.setenv("AISALES_SECRET_KEY", "test-key")
    other = _business(dsn, f"bnd4-{uuid.uuid4().hex[:8]}")
    try:
        for business, token in ((shop, "EAAG-shop"), (other, "EAAG-other")):
            with db.tenant(dsn, business.id) as conn:
                _integrate(dsn, business.id, f"pn-{uuid.uuid4().hex[:8]}")
                secrets.put(conn, business_id=business.id, provider="whatsapp",
                            secret_type="access_token", value=token)
                secrets.put(conn, business_id=business.id, provider="whatsapp",
                            secret_type="app_secret", value=f"{token}-secret")

        fallback = channels.SimulatorChannel()
        with db.tenant(dsn, shop.id) as conn:
            mine = channels.for_business(conn, business_id=shop.id, fallback=fallback)
            theirs_by_id = channels.for_business(conn, business_id=other.id,
                                                 fallback=fallback)
        # Different businesses, different numbers, and neither is the fallback.
        assert mine is not fallback and theirs_by_id is not fallback
        assert mine._phone_number_id != theirs_by_id._phone_number_id
    finally:
        with db.connect(dsn) as conn:
            conn.execute("delete from businesses where id = %s", (other.id,))


def test_a_business_with_no_paystack_account_gets_none(dsn: str, shop,
                                                       monkeypatch) -> None:
    """Not the platform's client. A business with no Paystack account must not
    take money through somebody else's."""
    from aisales import paystack

    monkeypatch.setenv("AISALES_SECRET_KEY", "test-key")
    platform = paystack.FakePaystack("sk_platform")
    with db.tenant(dsn, shop.id) as conn:
        assert paystack.for_business(conn, business_id=shop.id,
                                     fallback=platform) is platform

        _integrate(dsn, shop.id, f"acct-{uuid.uuid4().hex[:8]}")
        with db.connect(dsn) as c:
            c.execute("update business_integrations set provider = 'paystack' "
                      "where business_id = %s", (shop.id,))
        # Still no secret, so still no client of its own.
        assert paystack.for_business(conn, business_id=shop.id,
                                     fallback=platform) is platform


def test_a_failed_send_marks_the_integration_rather_than_retrying_silently(
        dsn: str, shop, monkeypatch) -> None:
    """Requirement 6.6. Without this, a revoked token is a retry loop with no
    symptom anywhere the owner can see."""
    from aisales import channels

    _integrate(dsn, shop.id, f"pn-{uuid.uuid4().hex[:8]}")
    with db.tenant(dsn, shop.id) as conn:
        channels.record_send_failure(conn, business_id=shop.id, provider="whatsapp",
                                     reason="Meta rejected the token (401)")
        row = conn.execute(
            "select status, metadata from business_integrations where business_id = %s",
            (shop.id,)).fetchone()
    assert row["status"] == "error"
    assert "401" in str(row["metadata"]["last_error"])
