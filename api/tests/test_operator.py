"""The operator console, over real HTTP.

The console is the one surface in the API that reads and writes across every
tenant, so the properties worth testing are the ones that keep it from becoming
everybody's surface:

- a business owner cannot reach it, however they ask;
- an operator can, and onboarding through it produces an account that actually
  works;
- suspension through it stops the agent for real, not just in the response body;
- and when `AISALES_ADMIN_DSN` is unset the routes say so rather than 500ing.

Driven with `urllib` and a cookie jar against a live server, reusing the shape
from `test_cross_tenant_api.py`, because the session cookie is part of what is
being tested.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid

import psycopg
import pytest

from aisales import agent, auth, db

DSN = os.environ.get("AISALES_TEST_DSN", "postgresql:///aisales_test")
PASSWORD = "correct horse battery staple"
ADMIN_DSN = os.environ.get("AISALES_TEST_ADMIN_DSN", DSN)


@pytest.fixture(scope="module")
def base_url() -> str:
    try:
        with psycopg.connect(DSN):
            pass
    except psycopg.Error as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no test database at {DSN}: {exc}")

    import uvicorn

    from aisales_api.app import create_app

    server = uvicorn.Server(uvicorn.Config(
        create_app(DSN), host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        pytest.fail("the API server never started")

    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


class Session:
    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def __call__(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("content-type", "application/json")
        try:
            with self.opener.open(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"null")
            except ValueError:
                return exc.code, {"detail": raw.decode(errors="replace")}

    def post(self, path: str, body: dict | None = None):
        return self("POST", path, body)

    def get(self, path: str):
        return self("GET", path)

    def sign_in(self, email: str, password: str = PASSWORD) -> dict:
        status, payload = self.post("/auth/sign-in", {"email": email, "password": password})
        assert status == 200, f"sign-in failed: {status} {payload}"
        return payload["user"]


@pytest.fixture(autouse=True)
def admin_dsn(monkeypatch):
    """The console is off unless a deployment switches it on, so every test
    that uses it has to switch it on -- which is the real default."""
    monkeypatch.setenv(db.OPERATOR_DSN_VAR, ADMIN_DSN)
    os.environ.setdefault("AISALES_SECRET_KEY", "test-key-for-encryption")
    yield


@pytest.fixture
def operator(base_url: str):
    email = f"op-{uuid.uuid4().hex[:8]}@example.test"
    with db.connect(DSN) as conn:
        db.install_operator(DSN)
        auth.create_user(conn, email, PASSWORD, name="Operator", is_operator=True)
    session = Session(base_url)
    session.sign_in(email)
    yield session
    with db.connect(DSN) as conn:
        conn.execute("delete from users where email = %s", (email,))


@pytest.fixture
def owner(base_url: str):
    """An ordinary business owner, for the negative cases."""
    tag = uuid.uuid4().hex[:8]
    with db.connect(DSN) as conn:
        business = conn.execute(
            "insert into businesses (slug, name) values (%s, %s) returning id",
            (f"op-{tag}", f"Shop {tag}")).fetchone()
        email = f"owner-{tag}@example.test"
        user_id = auth.create_user(conn, email, PASSWORD, name="Owner")
        auth.add_member(conn, user_id, str(business["id"]), role="owner")
    session = Session(base_url)
    session.sign_in(email)
    yield {"session": session, "business_id": str(business["id"]), "email": email}
    with db.connect(DSN) as conn:
        conn.execute("delete from businesses where id = %s", (business["id"],))
        conn.execute("delete from users where email = %s", (email,))


# ------------------------------------------------------------- the boundary


OPERATOR_ROUTES = ("/operator/businesses", "/operator/audit")


@pytest.mark.parametrize("path", OPERATOR_ROUTES)
def test_a_business_owner_cannot_reach_the_console(owner, path: str) -> None:
    """403, not 404: the route exists and they are not allowed, which is a
    different fact from it not existing."""
    status, _ = owner["session"].get(path)
    assert status == 403


def test_a_business_owner_cannot_onboard_or_suspend(owner) -> None:
    session = owner["session"]
    assert session.post("/operator/businesses", {"slug": "x", "name": "X"})[0] == 403
    assert session.post(f"/operator/businesses/{owner['business_id']}/suspend")[0] == 403
    assert session.post(f"/operator/businesses/{owner['business_id']}/resume")[0] == 403


def test_no_session_is_refused(base_url: str) -> None:
    assert Session(base_url).get("/operator/businesses")[0] == 401


def test_an_operator_who_is_not_a_member_of_a_business_cannot_read_business_routes(
        operator) -> None:
    """The two guards are not interchangeable, in either direction.

    An operator sees across tenants *through the console*; business routes
    still require a membership, so the console cannot be used as a way to read
    one business's conversations.
    """
    assert operator.get("/conversations")[0] == 403
    assert operator.get("/leads")[0] == 403
    assert operator.get("/settings")[0] == 403


def test_auth_me_answers_for_an_operator(operator) -> None:
    """The regression that made the console unreachable.

    `/auth/me` was guarded by the business-membership check, so the one
    endpoint that has to work for everybody 403'd for the only account that
    uses it -- and the console's layout, which calls it to decide where to
    send you, concluded you were nobody. It reports who you are, not what
    business you belong to, and those are different questions.
    """
    status, payload = operator.get("/auth/me")
    assert status == 200
    assert payload["user"]["is_operator"] is True
    assert payload["user"]["business"] is None
    assert payload["user"]["role"] == "operator"


def test_auth_me_still_answers_for_a_business_owner(owner) -> None:
    status, payload = owner["session"].get("/auth/me")
    assert status == 200
    assert payload["user"]["is_operator"] is False
    assert payload["user"]["business"] is not None


def test_without_the_admin_dsn_the_console_says_so(base_url, monkeypatch) -> None:
    """503 with the reason, not a 500 and not a missing route.

    A deployment running one business has no operator DSN, and that is a
    correct configuration rather than a fault -- so the console has to be able
    to say which it is.
    """
    email = f"op2-{uuid.uuid4().hex[:8]}@example.test"
    with db.connect(DSN) as conn:
        auth.create_user(conn, email, PASSWORD, is_operator=True)
    try:
        session = Session(base_url)
        session.sign_in(email)
        monkeypatch.delenv(db.OPERATOR_DSN_VAR, raising=False)
        status, payload = session.get("/operator/businesses")
        assert status == 503
        assert db.OPERATOR_DSN_VAR in payload["detail"]
    finally:
        with db.connect(DSN) as conn:
            conn.execute("delete from users where email = %s", (email,))


# ---------------------------------------------------------------- onboarding


def test_onboarding_produces_an_account_that_works(base_url, operator) -> None:
    """The whole point of replacing the CLI: the business exists, its owner can
    sign in, and they land in their own empty dashboard rather than someone
    else's."""
    tag = uuid.uuid4().hex[:8]
    slug, email = f"new-{tag}", f"new-owner-{tag}@example.test"
    try:
        status, created = operator.post("/operator/businesses", {
            "slug": slug, "name": f"New Shop {tag}",
            "owner_email": email, "owner_password": PASSWORD, "owner_name": "New Owner",
        })
        assert status == 201, created
        assert created["slug"] == slug

        owner_session = Session(base_url)
        owner_session.sign_in(email)
        assert owner_session.get("/status")[0] == 200
        assert owner_session.get("/conversations")[0] == 200
        body = owner_session.get("/status")[1]
        assert body["business"] == f"New Shop {tag}"
        # A brand-new business answers no one until it is connected.
        assert body["suspended"] is False
    finally:
        with db.connect(DSN) as conn:
            conn.execute("delete from businesses where slug = %s", (slug,))
            conn.execute("delete from users where email = %s", (email,))


def test_a_duplicate_slug_is_refused_and_leaves_nothing_behind(operator) -> None:
    """A half-onboarded business with no owner is worse than none."""
    tag = uuid.uuid4().hex[:8]
    slug = f"dup-{tag}"
    email = f"dup-owner-{tag}@example.test"
    try:
        assert operator.post("/operator/businesses", {
            "slug": slug, "name": "First", "owner_email": email,
            "owner_password": PASSWORD})[0] == 201

        status, payload = operator.post("/operator/businesses", {
            "slug": slug, "name": "Second",
            "owner_email": f"other-{tag}@example.test", "owner_password": PASSWORD})
        assert status == 409

        with db.connect(DSN) as conn:
            n = conn.execute("select count(*) as n from businesses where slug = %s",
                             (slug,)).fetchone()["n"]
        assert n == 1
    finally:
        with db.connect(DSN) as conn:
            conn.execute("delete from businesses where slug = %s", (slug,))
            conn.execute("delete from users where email = %s", (email,))


def test_onboarding_requires_every_field(operator) -> None:
    assert operator.post("/operator/businesses", {"slug": "x", "name": "X"})[0] == 400
    assert operator.post("/operator/businesses", {})[0] == 400


def test_the_list_reports_each_business_and_its_integrations(operator) -> None:
    tag = uuid.uuid4().hex[:8]
    slug = f"listed-{tag}"
    try:
        operator.post("/operator/businesses", {
            "slug": slug, "name": f"Listed {tag}",
            "owner_email": f"listed-{tag}@example.test", "owner_password": PASSWORD,
            "whatsapp_number_id": f"pn-{tag}"})

        status, payload = operator.get("/operator/businesses")
        assert status == 200
        entry = next(b for b in payload["businesses"] if b["slug"] == slug)
        assert entry["suspended"] is False
        assert entry["members"] == 1
        assert entry["integrations"]["whatsapp"]["account"] == f"pn-{tag}"
        assert entry["messages_30d"] == 0
    finally:
        with db.connect(DSN) as conn:
            conn.execute("delete from businesses where slug = %s", (slug,))
            conn.execute("delete from users where email = %s", (f"listed-{tag}@example.test",))


# ---------------------------------------------------------------- suspension


def test_suspend_stops_the_agent_and_resume_starts_it_again(base_url, operator) -> None:
    """Through the console, end to end, with the database checked directly --
    a response body saying "suspended": true is not evidence."""
    tag = uuid.uuid4().hex[:8]
    slug, email = f"susp-{tag}", f"susp-{tag}@example.test"
    try:
        _, created = operator.post("/operator/businesses", {
            "slug": slug, "name": f"Susp {tag}",
            "owner_email": email, "owner_password": PASSWORD})
        business_id = created["id"]

        status, payload = operator.post(
            f"/operator/businesses/{business_id}/suspend", {"reason": "unpaid"})
        assert status == 200 and payload["suspended"] is True

        with db.tenant(DSN, business_id) as conn:
            row = conn.execute(
                "select suspended_at, suspended_reason from businesses where id = %s",
                (business_id,)).fetchone()
        assert row["suspended_at"] is not None
        assert row["suspended_reason"] == "unpaid"

        # And it actually stops a turn.
        owner_session = Session(base_url)
        owner_session.sign_in(email)
        q = db.Queue(DSN)
        with db.tenant(DSN, business_id) as conn:
            business = agent.business_by_id(conn, business_id)
            assert business.suspended is True
            from aisales.channels import Inbound
            result = agent.record_inbound(conn, q, business, Inbound(
                channel="simulator", external_id=f"susp-{tag}",
                from_phone="+2348031234567", text="Hello?"))
            assert result.job_id is None, "a suspended business queued a turn"

        status, resumed = operator.post(f"/operator/businesses/{business_id}/resume")
        assert status == 200 and resumed["suspended"] is False
        with db.tenant(DSN, business_id) as conn:
            assert agent.business_by_id(conn, business_id).suspended is False
    finally:
        with db.connect(DSN) as conn:
            conn.execute("delete from businesses where slug = %s", (slug,))
            conn.execute("delete from users where email = %s", (email,))


def test_suspending_a_business_that_does_not_exist_is_not_found(operator) -> None:
    """A typo must not read as a success."""
    assert operator.post(f"/operator/businesses/{uuid.uuid4()}/suspend")[0] == 404
    assert operator.post(f"/operator/businesses/{uuid.uuid4()}/resume")[0] == 404


def test_the_business_understands_it_is_suspended(base_url, operator) -> None:
    """The banner's data. Without it the owner sees a normal dashboard that
    silently answers nobody, and concludes the product is broken."""
    tag = uuid.uuid4().hex[:8]
    slug, email = f"banner-{tag}", f"banner-{tag}@example.test"
    try:
        _, created = operator.post("/operator/businesses", {
            "slug": slug, "name": f"Banner {tag}",
            "owner_email": email, "owner_password": PASSWORD})
        owner_session = Session(base_url)
        owner_session.sign_in(email)
        assert owner_session.get("/status")[1]["suspended"] is False

        operator.post(f"/operator/businesses/{created['id']}/suspend", {"reason": "unpaid"})
        payload = owner_session.get("/status")[1]
        assert payload["suspended"] is True
        assert payload["suspended_reason"] == "unpaid"
    finally:
        with db.connect(DSN) as conn:
            conn.execute("delete from businesses where slug = %s", (slug,))
            conn.execute("delete from users where email = %s", (email,))


# -------------------------------------------------------------------- audit


def test_every_operator_action_is_recorded(operator) -> None:
    """The console is only reviewable if its use is visible."""
    tag = uuid.uuid4().hex[:8]
    slug = f"audit-{tag}"
    try:
        operator.post("/operator/businesses", {
            "slug": slug, "name": f"Audit {tag}",
            "owner_email": f"audit-{tag}@example.test", "owner_password": PASSWORD})

        status, payload = operator.get("/operator/audit")
        assert status == 200
        created = next(a for a in payload["audit"]
                       if a["action"] == "business.created" and a["slug"] == slug)
        assert created["actor"].startswith("op-")
    finally:
        with db.connect(DSN) as conn:
            conn.execute("delete from businesses where slug = %s", (slug,))
            conn.execute("delete from users where email = %s", (f"audit-{tag}@example.test",))
