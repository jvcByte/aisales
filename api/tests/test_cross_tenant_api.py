"""Two businesses, one API, over real HTTP.

Every other test in this repo calls functions. This one starts a server and
speaks to it, because the property under test is not in any function -- it is
in the wiring: which tenant a request resolves to, and whether there is any
input a caller can supply that changes the answer.

The `slug` query parameter that used to be on every route is gone, and that is
the point. A business is named by the session and by nothing else, so there is
no parameter to guess. `test_a_slug_parameter_cannot_select_another_business`
asserts it stays that way: FastAPI ignores unknown query parameters, so the
test can only fail by the response containing the other business's rows.

Driven with `urllib` and a cookie jar rather than a test client, because the
cookie is part of what is being tested -- httpOnly, set by the API, carried by
the browser, and the only thing standing between a session and its business.
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

from aisales import auth, db

DSN = os.environ.get("AISALES_TEST_DSN", "postgresql:///aisales_test")
PASSWORD = "correct horse battery staple"


# ------------------------------------------------------------------ server


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
    else:  # pragma: no cover - startup is deterministic
        pytest.fail("the API server never started")

    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


class Session:
    """A browser, near enough: it keeps cookies and sends them back."""

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def __call__(self, method: str, path: str, body: dict | None = None,
                 *, token: str | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("content-type", "application/json")
        if token:
            request.add_header("authorization", f"Bearer {token}")
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read()
                return response.status, json.loads(raw or b"null")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"null")
            except ValueError:
                return exc.code, {"detail": raw.decode(errors="replace")}

    def get(self, path: str, **kw):
        return self("GET", path, **kw)

    def post(self, path: str, body: dict | None = None, **kw):
        return self("POST", path, body, **kw)

    def patch(self, path: str, body: dict | None = None, **kw):
        return self("PATCH", path, body, **kw)

    def sign_in(self, email: str, password: str = PASSWORD) -> dict:
        status, payload = self.post("/auth/sign-in",
                                    {"email": email, "password": password})
        assert status == 200, f"sign-in failed: {status} {payload}"
        return payload["user"]

    @property
    def cookie_names(self) -> set[str]:
        return {c.name for c in self.jar}


# ------------------------------------------------------------------- tenants


def _phone(tag: str) -> str:
    """A valid Nigerian mobile number, distinct per tag.

    Not `+234803{tag}`: that is hex, and `normalise_phone` correctly refuses
    it. A fixture that invents an invalid number tests the normaliser rather
    than the thing it meant to test.
    """
    return "+234803" + str(int(tag, 16)).ljust(7, "0")[:7]


def _seed(tag: str) -> dict:
    """A business with one of everything, written through the tenant path so
    this fixture works whether or not RLS happens to be switched on."""
    slug = f"iso-{tag}"
    with db.connect(DSN) as conn:
        row = conn.execute(
            "insert into businesses (slug, name, settings) values (%s, %s, '{}') "
            "returning id, slug, name, settings", (slug, f"Shop {tag}")).fetchone()
    business = db.Business.from_row(row)

    with db.tenant(DSN, business.id) as conn:
        customer = conn.execute(
            "insert into customers (business_id, phone_e164, name) values (%s, %s, %s) "
            "returning id", (business.id, _phone(tag), f"Buyer {tag}")).fetchone()
        conversation = conn.execute(
            "insert into conversations (business_id, customer_id) values (%s, %s) "
            "returning id", (business.id, customer["id"])).fetchone()
        conn.execute(
            "insert into messages (business_id, conversation_id, role, body) "
            "values (%s, %s, 'customer', %s)",
            (business.id, conversation["id"], f"secret-{tag}"))
        conn.execute(
            """
            insert into products (business_id, sku, name, price_kobo, stock_qty,
                                  variants, description, search_text)
            values (%s, %s, %s, 1250000, 4, '[]', %s, %s)
            """,
            (business.id, f"SKU-{tag}", f"Lace {tag}", f"lace {tag}",
             f"lace {tag} sku-{tag}"))
        conn.execute(
            """
            insert into leads (business_id, customer_id, tier, reason, intent, status)
            values (%s, %s, 'hot', %s, 'price_enquiry', 'open')
            """,
            (business.id, customer["id"], f"reason-{tag}"))

    email = f"owner-{tag}@example.test"
    with db.connect(DSN) as conn:
        user_id = auth.create_user(conn, email, PASSWORD, name=f"Owner {tag}")
        auth.add_member(conn, user_id, business.id, role="owner")

    return {"business": business, "email": email, "tag": tag,
            "customer_id": str(customer["id"]),
            "conversation_id": str(conversation["id"])}


@pytest.fixture(scope="module")
def tenants(base_url: str):
    tag_a, tag_b = uuid.uuid4().hex[:6], uuid.uuid4().hex[:6]
    a, b = _seed(tag_a), _seed(tag_b)
    yield a, b
    with db.connect(DSN) as conn:
        conn.execute("delete from businesses where slug = any(%s)",
                     ([a["business"].slug, b["business"].slug],))
        conn.execute("delete from users where email = any(%s)", ([a["email"], b["email"]],))


@pytest.fixture
def as_a(base_url: str, tenants):
    session = Session(base_url)
    session.sign_in(tenants[0]["email"])
    return session


# --------------------------------------------------------------- the reads


LIST_ROUTES = ("/conversations", "/customers", "/leads", "/orders", "/payments",
               "/catalogue", "/follow-ups", "/settings", "/status", "/activity",
               "/insights/overview", "/insights/daily")


def test_every_list_route_returns_only_the_signed_in_business(as_a, tenants) -> None:
    """Not "filters out the other tenant" -- the other tenant's rows are not
    in the response to filter."""
    a, b = tenants
    for route in LIST_ROUTES:
        status, payload = as_a.get(route)
        assert status == 200, f"{route} -> {status} {payload}"
        body = json.dumps(payload)
        for marker in (f"secret-{b['tag']}", f"SKU-{b['tag']}", f"reason-{b['tag']}",
                       _phone(b["tag"]), b["business"].slug, b["business"].id):
            assert marker not in body, f"{route} leaked {marker!r}"


def test_a_slug_parameter_cannot_select_another_business(as_a, tenants) -> None:
    """The parameter that used to exist. It must be inert.

    FastAPI ignores query parameters a route does not declare, so this cannot
    fail by erroring -- it can only fail by returning the other business's
    rows, which is exactly the regression worth catching.
    """
    a, b = tenants
    for route in ("/conversations", "/leads", "/catalogue", "/settings"):
        status, payload = as_a.get(f"{route}?slug={b['business'].slug}")
        assert status == 200
        assert b["business"].id not in json.dumps(payload)
        assert f"secret-{b['tag']}" not in json.dumps(payload)


def test_another_businesss_conversation_is_not_found(as_a, base_url, tenants) -> None:
    """A uuid is not a capability. Naming a row that exists must be
    indistinguishable from naming one that does not -- so this is the same 404
    a made-up id gets, rather than an empty thread or a 403."""
    _, b = tenants
    status, payload = as_a.get(f"/conversations/{b['conversation_id']}/messages")
    made_up, _ = as_a.get(f"/conversations/{uuid.uuid4()}/messages")
    assert status == 404, f"another tenant's transcript was readable: {payload}"
    assert status == made_up


def test_taking_over_another_businesss_conversation_is_not_found(as_a, tenants) -> None:
    """404 rather than 403, and the same 404 a made-up id gets: a refusal that
    differs from "no such row" is an oracle for which ids exist."""
    _, b = tenants
    status, _ = as_a.post(f"/conversations/{b['conversation_id']}/takeover",
                          {"as": "staff"})
    made_up, _ = as_a.post(f"/conversations/{uuid.uuid4()}/takeover", {"as": "staff"})
    assert status == 404
    assert status == made_up


def test_the_simulator_answers_as_the_session_business(base_url, as_a, tenants) -> None:
    """The simulator used to take a `slug` in its body. It takes the session's
    business now, so a payload cannot redirect it."""
    a, b = tenants
    status, payload = as_a.post("/sim/messages", {
        "from": _phone(a["tag"]),
        "text": "Abeg how much?",
        "slug": b["business"].slug,      # ignored, and that is the assertion
    })
    assert status == 200
    assert payload["slug"] == a["business"].slug

    # `db.tenant` sets the context; only a policy or this predicate acts on
    # it. Scoping the query explicitly is what makes the count meaningful
    # whether or not RLS is switched on -- which is the same reason the routes
    # carry their own `where business_id = ...`.
    with db.tenant(DSN, b["business"].id) as conn:
        n = conn.execute("select count(*) as n from messages where business_id = %s",
                         (b["business"].id,)).fetchone()["n"]
    assert n == 1, "the simulator wrote into the business its payload named"


# --------------------------------------------------------------- the session


@pytest.mark.parametrize("route", ["/conversations", "/leads", "/status",
                                   "/settings", "/insights/overview"])
def test_no_session_is_refused(base_url: str, route: str) -> None:
    status, _ = Session(base_url).get(route)
    assert status == 401, f"{route} answered without a session"


def test_a_made_up_token_is_refused(base_url: str) -> None:
    status, _ = Session(base_url).get("/conversations", token="not-a-real-token")
    assert status == 401


def test_the_session_cookie_is_httponly(base_url: str, tenants) -> None:
    """An injected script must not be able to read the session it is riding."""
    session = Session(base_url)
    session.sign_in(tenants[0]["email"])
    cookie = next(c for c in session.jar if c.name == "aisales_session")
    assert cookie.has_nonstandard_attr("HttpOnly") or cookie.has_nonstandard_attr("httponly")


def test_signing_out_invalidates_the_token(base_url: str, tenants) -> None:
    """Revoked means revoked: the token stops working even if it is kept."""
    session = Session(base_url)
    session.sign_in(tenants[0]["email"])
    token = next(c.value for c in session.jar if c.name == "aisales_session")
    assert session.post("/auth/sign-out")[0] == 200
    status, _ = Session(base_url).get("/conversations", token=token)
    assert status == 401, "a signed-out session still worked"


def test_a_bearer_token_works_for_scripts(base_url: str, tenants) -> None:
    """What makes the API drivable from a terminal without a cookie jar."""
    session = Session(base_url)
    session.sign_in(tenants[0]["email"])
    token = next(c.value for c in session.jar if c.name == "aisales_session")
    status, payload = Session(base_url).get("/auth/me", token=token)
    assert status == 200
    assert payload["user"]["email"] == tenants[0]["email"]
