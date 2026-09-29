"""Tenant isolation, against a database with RLS actually switched on.

This is the only module that calls `harden()`, and it turns it back off
afterwards, so the rest of the suite keeps testing the application rather than
the policy. The ordering matters: every other test writes through
`db.connect()`, which has no tenant context, and under RLS those writes would
vanish at the `with check` clause.

Every query here runs as the unprivileged app role, never as the session user.
That is not tidiness. This machine's database role is a superuser, and **a
superuser bypasses row level security entirely** -- `harden()` will refuse to
run for one. A test that queried as itself would pass against a policy that
does nothing at all.

Four distinct failures are possible, and they are not the same failure:

1. `set_config` never runs            -> every read returns nothing, which the
                                         application sees as missing data.
2. the context outlives the transaction -> the *next* job on that connection
                                         reads the previous tenant's rows.
3. the policy is missing `force`      -> the table owner, who in a small
                                         deployment is the app role, bypasses it.
4. no policy at all                   -> nothing is scoped.

Test 2 is the one worth staring at. It is invisible in a single-tenant test, in
a worker that opens a fresh connection per job, and only appears once anything
is pooled -- which is also when it is indistinguishable from a data breach.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import psycopg
import pytest
from psycopg.rows import dict_row

from aisales import agent, db, providers
from aisales.channels import Inbound, Outbound


class RecordingChannel:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def verify(self, body, headers) -> bool:
        return True

    def parse(self, body, headers):
        return []

    def send(self, message: Outbound) -> str:
        self.sent.append(message.text)
        return f"rec-{len(self.sent)}"


@pytest.fixture(scope="module", autouse=True)
def hardened(dsn: str) -> str:
    """RLS on for this module only, and off again even if a test fails."""
    role = db.harden(dsn)
    yield role
    db.unharden(dsn)


@pytest.fixture(scope="module")
def app_dsn(dsn: str, hardened: str) -> str:
    """The same database, reached as the app role rather than as the owner.

    `-c role=` in the connection options does this with no code change, and it
    is what a deployment does too -- the difference between a policy that is
    enforced and one that is decoration is which role is in the DSN.
    """
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}options=-c%20role%3D{hardened}"


@contextmanager
def as_tenant(dsn: str, business_id: str):
    """`db.tenant`, with the app role stepped into as well as the tenant.

    Both settings are transaction-scoped, and psycopg opens the transaction
    implicitly on the first execute and ends it when the connection closes --
    so neither outlives this block.
    """
    with psycopg.connect(dsn, autocommit=False, row_factory=dict_row) as conn:
        conn.execute(f"set local role {db.APP_ROLE}")  # noqa: S608 - a constant
        conn.execute("select set_config('aisales.business_id', %s, true)",
                     (str(business_id),))
        yield conn


def _seed(dsn: str, slug: str, phone: str) -> dict:
    """A business with one customer, one thread and one message, all written
    through the tenant path -- so the write side of the policy is exercised."""
    with db.connect(dsn) as conn:
        row = conn.execute(
            "insert into businesses (slug, name, settings) values (%s, %s, '{}') "
            "returning id, slug, name, settings",
            (slug, f"Business {slug}")).fetchone()
    business = db.Business.from_row(row)
    with as_tenant(dsn, business.id) as conn:
        customer = conn.execute(
            "insert into customers (business_id, phone_e164, name) values (%s, %s, %s) "
            "returning id", (business.id, phone, f"Customer of {slug}")).fetchone()
        conversation = conn.execute(
            "insert into conversations (business_id, customer_id) values (%s, %s) "
            "returning id", (business.id, customer["id"])).fetchone()
        conn.execute(
            "insert into messages (business_id, conversation_id, role, body) "
            "values (%s, %s, 'customer', %s)",
            (business.id, conversation["id"], f"hello from {slug}"))
    return {"business": business, "customer_id": str(customer["id"]),
            "conversation_id": str(conversation["id"]), "slug": slug,
            "phone": phone}


@pytest.fixture
def two_tenants(dsn: str):
    tag = uuid.uuid4().hex[:6]
    a = _seed(dsn, f"iso-a-{tag}", "+2348031110001")
    b = _seed(dsn, f"iso-b-{tag}", "+2348031110002")
    yield a, b
    with db.connect(dsn) as conn:
        conn.execute("delete from businesses where slug = any(%s)",
                     ([a["slug"], b["slug"]],))


# --------------------------------------------------------------- the policy


def test_a_tenant_sees_only_its_own_rows(dsn: str, two_tenants) -> None:
    a, _ = two_tenants
    with as_tenant(dsn, a["business"].id) as conn:
        phones = [r["phone_e164"] for r in
                  conn.execute("select phone_e164 from customers").fetchall()]
    assert "+2348031110001" in phones
    assert "+2348031110002" not in phones, "a tenant read another tenant's customers"


def test_naming_another_tenants_row_returns_nothing(dsn: str, two_tenants) -> None:
    """Not "the caller filters it out" -- the row is not visible at all."""
    a, b = two_tenants
    with as_tenant(dsn, a["business"].id) as conn:
        assert conn.execute("select id from customers where id = %s",
                            (b["customer_id"],)).fetchone() is None
        assert conn.execute("select count(*) as n from messages"
                            ).fetchone()["n"] == 1


def test_a_write_cannot_claim_another_tenant(dsn: str, two_tenants) -> None:
    """`with check` is a separate clause from `using`. Omitting it leaves the
    read side correct while letting a bug write into another tenant."""
    a, b = two_tenants
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_tenant(dsn, a["business"].id) as conn:
            conn.execute(
                "insert into customers (business_id, phone_e164) values (%s, %s)",
                (b["business"].id, "+2348039999999"))


def test_the_context_dies_with_the_transaction(dsn: str, two_tenants) -> None:
    """The mechanism behind the pooled-connection failure, asserted directly.

    `set_config(..., is_local => true)` is scoped to the transaction. If it
    ever became a session-level `set`, a pooled connection would carry one
    tenant's identity into the next tenant's job.

    Note what the setting reverts *to*: the empty string, not NULL. `set_config`
    defines a custom parameter, and its reset value is `''`. That is why the
    tenant predicate has to nullif it -- `''::uuid` raises, so a reused
    connection would fail every query with a type error instead of reading
    nothing. The query below is the assertion that matters; the string check is
    incidental.
    """
    a, _ = two_tenants
    with psycopg.connect(dsn, autocommit=False, row_factory=dict_row) as conn:
        # Committed immediately, so it is session-scoped state and not part of
        # the transaction under test.
        conn.execute(f"set role {db.APP_ROLE}")  # noqa: S608 - a constant
        conn.commit()

        with conn.transaction():
            conn.execute("select set_config('aisales.business_id', %s, true)",
                         (str(a["business"].id),))
            assert conn.execute("select count(*) as n from customers"
                                ).fetchone()["n"] == 1

        # Same connection, same role, context gone. It must read nothing --
        # not everything, and not raise.
        assert conn.execute("select count(*) as n from customers").fetchone()["n"] == 0


def test_an_unscoped_connection_reads_nothing(dsn: str, two_tenants, app_dsn: str) -> None:
    """Fail closed. An unset setting must not mean "every tenant".

    `current_setting(..., true)` returns NULL rather than raising, and
    `business_id = NULL` is never true, so a query with no context matches no
    rows instead of all of them.
    """
    with db.connect(app_dsn) as conn:
        assert conn.execute("select count(*) as n from customers").fetchone()["n"] == 0


def test_harden_refuses_a_role_it_cannot_constrain(dsn: str) -> None:
    """The failure that has no symptom.

    Installing policies for a superuser succeeds. The application then runs,
    every query returns every tenant's rows, and nothing anywhere complains.
    """
    who = db.connect(dsn).execute("select current_user as u").fetchone()["u"]
    with pytest.raises(db.UnscopedRoleError):
        db.harden(dsn, app_role=who)
    with pytest.raises(db.UnscopedRoleError):
        db.harden(dsn, app_role="aisales_queue")  # BYPASSRLS, by construction


# ---------------------------------------------------- the worker, A then B


def _scripted(count: int = 1) -> providers.ScriptedChat:
    """One scripted turn per job, because ScriptedChat raises when it runs dry
    and a raise here would look like a tenant-isolation failure."""
    return providers.ScriptedChat(turns=[
        providers.Turn(text="Hello, how can I help you today?", model="scripted",
                       finish_reason="stop")
    ] * count)


def _queue_a_turn(dsn: str, seeded: dict, phone: str | None = None) -> None:
    """One inbound message, and the turn it queues.

    `phone` distinguishes conversations. Two turns for one conversation would
    be refused by `jobs_one_turn_idx` rather than queued -- correct, but it
    would leave this helper silently doing nothing.
    """
    q = db.Queue(dsn)
    with as_tenant(dsn, seeded["business"].id) as conn:
        agent.record_inbound(conn, q, seeded["business"],
                             Inbound(channel="simulator",
                                     external_id=f"iso-{uuid.uuid4()}",
                                     from_phone=phone or seeded["phone"],
                                     text="Abeg how much?"))


def test_a_worker_turn_runs_under_its_own_job_tenant(app_dsn: str, two_tenants) -> None:
    from aisales_worker.runner import Worker  # noqa: PLC0415 - separate package

    a, _ = two_tenants
    _queue_a_turn(app_dsn, a)
    worker = Worker(dsn=app_dsn, channel=RecordingChannel(), chat=_scripted())
    assert worker.run_once() is True
    assert worker.channel.sent, "the turn produced no reply"


def test_a_then_b_then_a_leaks_nothing(app_dsn: str, two_tenants) -> None:
    """The pooled-connection failure, through the real worker.

    Three jobs, two tenants, one worker. If the tenant context survived a turn,
    B's job would look up B's conversation while wearing A's identity and find
    nothing -- so a real reply from all three is the assertion.

    The worker is what proves the *application* sets a context; the tests above
    prove what happens when it does not.
    """
    a, b = two_tenants
    from aisales_worker.runner import Worker  # noqa: PLC0415

    # Three conversations: A, then B, then A again. The third is a different
    # customer of the first business, because a second live turn for the same
    # conversation is refused by design.
    _queue_a_turn(app_dsn, a)
    _queue_a_turn(app_dsn, b)
    _queue_a_turn(app_dsn, a, phone="+2348031110003")

    channel = RecordingChannel()
    worker = Worker(dsn=app_dsn, channel=channel, chat=_scripted(3))
    for _ in range(3):
        assert worker.run_once() is True, "a job was left unclaimed"

    assert len(channel.sent) == 3, f"a turn saw no data: {channel.sent}"
    assert all(channel.sent), f"a turn produced an empty reply: {channel.sent}"


def test_the_queue_claims_across_tenants_but_works_inside_one(app_dsn: str,
                                                              two_tenants) -> None:
    """The boundary, stated as a test.

    Claiming is cross-tenant by necessity -- a worker takes whatever is next and
    does not know whose it is until it has it. If claim were tenant-scoped the
    queue would only ever drain one business.
    """
    a, b = two_tenants
    _queue_a_turn(app_dsn, a)
    _queue_a_turn(app_dsn, b)

    q = db.Queue(app_dsn)
    claimed = [q.claim("worker-a"), q.claim("worker-a")]
    assert all(c is not None for c in claimed)
    assert {c.business_id for c in claimed} == {a["business"].id, b["business"].id}


# ------------------------------------------- proving the test can fail

def test_the_isolation_test_catches_a_worker_that_sets_the_context_once(
        app_dsn: str, two_tenants, monkeypatch) -> None:
    """Requirement 5.4, and the reason to trust everything above it.

    A test for a leak that cannot fail is not a test. This one breaks the
    worker on purpose -- setting the tenant context at connect time, which is
    the natural-looking mistake and the one that produces a cross-tenant read
    only once a second job lands on the same connection -- and asserts that
    `test_a_then_b_then_a_leaks_nothing` above actually notices.

    Setting the context once is what a pooled connection does by accident: it
    looks correct for the first job, and the leak appears on the second.
    """
    from aisales import db as db_module
    from aisales_worker.runner import Worker  # noqa: PLC0415

    a, b = two_tenants
    _queue_a_turn(app_dsn, a)
    _queue_a_turn(app_dsn, b)

    real_tenant = db_module.tenant
    first = {"business_id": None}

    @contextmanager
    def sticky_tenant(dsn: str, business_id: str):
        # The bug: the first tenant sticks, and every later job inherits it.
        first["business_id"] = first["business_id"] or business_id
        with real_tenant(dsn, first["business_id"]) as conn:
            yield conn

    monkeypatch.setattr(db_module, "tenant", sticky_tenant)

    channel = RecordingChannel()
    worker = Worker(dsn=app_dsn, channel=channel, chat=_scripted(2))
    for _ in range(2):
        worker.run_once()

    # The worker reported two successes; the second was a lie. This is exactly
    # the failure the three-job test is shaped to catch: under the sticky
    # tenant, B's job resolves A's conversation and finds nothing.
    assert len(channel.sent) < 2, (
        "the sticky-tenant worker sent two replies, so the isolation test "
        "would pass against a broken worker and proves nothing"
    )
