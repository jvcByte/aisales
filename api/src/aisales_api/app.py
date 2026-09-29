"""The HTTP surface: channel webhooks, the dashboard API, and the simulator.

Three kinds of route, and they are separated by how they establish a tenant:

- **Authenticated routes.** A session cookie names a user, the user names a
  business, and every query runs inside `db.tenant()` for that business. There
  is no `slug` parameter anywhere: a business is never chosen by the caller,
  only by the session. That is the difference between a filter and a boundary.
- **Webhooks.** No session -- Meta and Paystack cannot hold one. They are
  authenticated by signature instead, and then resolve their own tenant from
  the payload's `phone_number_id` or `reference`.
- **`/healthz`.** No tenant, and therefore no tenant data.

The webhook contract is the part worth stating: verify, parse, store, enqueue,
return success -- and do all of it before any model runs. A platform that waits
too long for a webhook retries it, and repeated retries degrade the sending
number's quality rating, which is a slow and hard-to-reverse failure.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import date

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from aisales import agent, auth, db, dotenv, paystack as paystack_mod
from aisales.channels import Channel, ChannelError, Outbound, SimulatorChannel

from . import insights, operator as operator_mod, views
from .sim import PAGE

#: Used when no real key is configured, so offline mode can sign and verify
#: with the same algorithm against a known secret.
FAKE_SECRET = "sk_test_fake"

#: httpOnly, so a script injected into the dashboard cannot read it, and Lax,
#: so it rides along on the dashboard's own requests and not on a form post
#: from somewhere else.
SESSION_COOKIE = "aisales_session"


def default_dsn() -> str:
    dotenv.load()
    return os.environ.get("AISALES_DSN", "postgresql:///aisales")


def _paystack_secret() -> str:
    return os.environ.get("PAYSTACK_SECRET_KEY") or FAKE_SECRET


# Adding a channel is one entry here. Nothing in the agent, the tools, the
# guard or the prompts knows a channel's name.
CHANNELS: dict[str, Channel] = {
    "simulator": SimulatorChannel(),
}


def create_app(dsn: str | None = None) -> FastAPI:
    target = dsn or default_dsn()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.install(target)
        app.state.dsn = target
        app.state.queue = db.Queue(target)
        yield

    app = FastAPI(title="AI Sales Employee", lifespan=lifespan)
    app.state.dsn = target

    def conn():
        return db.connect(app.state.dsn)

    def scoped(who: auth.Principal):
        """The connection every authenticated route runs on."""
        return db.tenant(app.state.dsn, who.business_id)

    def owned(c: psycopg.Connection, conversation_id: str, who: auth.Principal) -> None:
        """404 unless this conversation is the session business's.

        A uuid in a URL is untrusted input like any other. `db.tenant` scopes
        the connection, but the tenant is a property of the *session* and this
        is the point where the two have to be shown to agree -- which is also
        the only place a caller could have supplied the wrong one.
        """
        found = c.execute(
            "select 1 from conversations where id = %s and business_id = %s",
            (conversation_id, who.business_id),
        ).fetchone()
        if found is None:
            # The same 404 a made-up uuid gets. A different status would say
            # which ids exist, which is the whole of what an attacker wants.
            raise HTTPException(404, "no such conversation")

    # ----------------------------------------------------------------- auth

    def signed_in(request: Request) -> auth.Principal:
        """The signed-in user, or 401. No statement about what they may see."""
        token = request.cookies.get(SESSION_COOKIE) or _bearer(request)
        if not token:
            raise HTTPException(401, "sign in required")
        with conn() as c:
            who = auth.principal_for(c, token)
        if who is None:
            # Unknown, expired and revoked are one answer on purpose.
            raise HTTPException(401, "session expired")
        return who

    def current(request: Request) -> auth.Principal:
        """The signed-in user *as a member of a business*, or 401/403.

        This is the default guard for every business route, deliberately: a
        route that reaches for the wrong dependency gets this one, which
        cannot see across tenants. An operator belongs to no business, so the
        two are not interchangeable and the safe one is the easier one to
        reach for.
        """
        who = signed_in(request)
        if who.business_id is None:
            # Signed in, but not a member of anything: an operator. 403 rather
            # than 401, because signing in again would not help.
            raise HTTPException(403, "this account is not a member of a business")
        return who

    def current_operator(request: Request) -> auth.Principal:
        """The signed-in user *as an operator*, or 401/403."""
        who = signed_in(request)
        if not who.is_operator:
            # The same 403 shape a business-less member gets, so a probe
            # cannot tell "you are not an operator" from "you are nothing".
            raise HTTPException(403, "this account cannot reach the operator console")
        return who

    @app.post("/auth/sign-in")
    async def sign_in(payload: dict, request: Request, response: Response):
        with conn() as c:
            token, reason = auth.sign_in(
                c, str(payload.get("email") or ""), str(payload.get("password") or ""),
                source=request.client.host if request.client else "")
            if token is None:
                raise HTTPException(401, reason)
            who = auth.principal_for(c, token)
        response.set_cookie(
            SESSION_COOKIE, token, httponly=True, samesite="lax",
            secure=_behind_tls(request), path="/",
            max_age=auth.SESSION_TTL_HOURS * 3600)
        return {"user": _principal(who)}

    @app.post("/auth/sign-out")
    async def sign_out(request: Request, response: Response):
        with conn() as c:
            auth.revoke_session(c, request.cookies.get(SESSION_COOKIE) or "")
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/auth/me")
    async def me(who: auth.Principal = Depends(signed_in)):
        """Who is signed in. The one route that must answer for everybody.

        `signed_in`, not `current`: this says who you are, not what business
        you belong to, and an operator belongs to none. Guarding it with the
        business check made the console's own layout 403 on the call it uses
        to work out where to send you.
        """
        return {"user": _principal(who)}

    # ------------------------------------------------------------- paystack
    #
    # Registered BEFORE `/webhooks/{name}`, and the order is load-bearing:
    # FastAPI matches routes in declaration order, so declaring the generic
    # channel route first makes this one unreachable -- it returns "unknown
    # channel 'paystack'" with a 404, and every real callback is dropped.

    def handle_paystack(body: bytes, signature: str | None) -> dict:
        """Verify, then apply. The one path a callback can take.

        Shared by the real webhook and the dev charge endpoint on purpose: a
        dev endpoint that called `apply_callback` directly would skip
        signature verification, and the verification code would then have no
        test exercising it through the route that uses it.
        """
        if not paystack_mod.verify_webhook(_paystack_secret(), body, signature):
            raise HTTPException(401, "signature verification failed")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "callback body is not JSON") from exc

        # A callback arrives with no session. It names its tenant through the
        # reference on the payment row, and `apply_callback` is one of the two
        # places allowed to look across businesses to find it.
        with conn() as c:
            outcome = paystack_mod.apply_callback(
                c, app.state.dsn,
                event=str(payload.get("event") or ""), data=payload.get("data") or {})
        # Always 200, including for a duplicate and for an ignored event:
        # anything else makes Paystack retry a delivery we have already handled.
        return {"received": True, "handled": outcome.handled,
                "idempotent": outcome.idempotent, "reason": outcome.reason}

    @app.post("/webhooks/paystack")
    async def paystack_webhook(request: Request):
        return handle_paystack(await request.body(),
                               request.headers.get("x-paystack-signature"))

    if os.environ.get("AISALES_FAKES") == "1":
        # Mounted only in offline mode, and that is a security decision rather
        # than tidiness. This endpoint signs callbacks with the server's own
        # secret, so its signature check passes by construction -- exposed
        # publicly it would let anyone mark any order paid by naming its
        # reference. With a real key configured the route does not exist.
        @app.post("/dev/paystack/charge")
        async def dev_charge(payload: dict):
            fake = paystack_mod.FakePaystack(_paystack_secret())
            body, signature = fake.callback(
                reference=str(payload.get("reference") or ""),
                amount_kobo=int(payload.get("amount_kobo") or 0),
                event=str(payload.get("event") or paystack_mod.CHARGE_SUCCESS),
            )
            return handle_paystack(body, signature)

    # ---------------------------------------------------- channel webhooks

    @app.post("/webhooks/{name}")
    async def webhook(name: str, request: Request):
        channel = CHANNELS.get(name)
        if channel is None:
            # No default channel: silently accepting an unknown delivery would
            # hide a misconfigured URL behind a 200.
            raise HTTPException(404, f"unknown channel {name!r}")

        body = await request.body()
        headers = dict(request.headers)
        if not channel.verify(body, headers):
            raise HTTPException(401, "signature verification failed")

        try:
            messages = channel.parse(body, headers)
        except ChannelError as exc:
            raise HTTPException(400, str(exc)) from exc

        recorded = []
        for message in messages:
            # Resolution is unscoped by necessity -- a delivery says which
            # business it is for, and nothing else does. Everything after it
            # runs inside that business's tenant.
            with conn() as c:
                business = _resolve_business(c, message, app.state.dsn)
            if business is None:
                raise HTTPException(404, "no business for this delivery")

            with db.tenant(app.state.dsn, business.id) as c:
                result = agent.record_inbound(c, app.state.queue, business, message)
            recorded.append({"duplicate": result.duplicate,
                             "conversation_id": result.conversation_id,
                             "enqueued": result.job_id is not None})

        # Always 200, including for a duplicate: the platform must stop
        # retrying, and a duplicate is not an error.
        return {"received": True, "processed": recorded}

    # ------------------------------------------------------------ simulator
    #
    # A development tool, so it is signed in like everything else rather than
    # left open: it runs the real turn function against real customer rows.

    @app.post("/sim/messages")
    async def sim_send(payload: dict, who: auth.Principal = Depends(current)):
        channel = CHANNELS["simulator"]
        messages = channel.parse(_as_body(payload), {})
        if not messages:
            raise HTTPException(400, "no message in payload")

        with scoped(who) as c:
            business = agent.business_by_id(c, who.business_id)
            result = agent.record_inbound(c, app.state.queue, business, messages[0])
            rows = agent.thread(c, result.conversation_id)

        return {"conversation_id": result.conversation_id,
                "slug": business.slug,
                "duplicate": result.duplicate,
                "enqueued": result.job_id is not None,
                "messages": [_wire(r) for r in rows]}

    @app.get("/sim", response_class=HTMLResponse)
    async def sim_page(who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            business = agent.business_by_id(c, who.business_id)
        return HTMLResponse(PAGE.replace("__BUSINESS__", business.name)
                                .replace("__SLUG__", business.slug))

    # -------------------------------------------------------------- reading

    @app.get("/conversations/{conversation_id}/messages")
    async def messages(conversation_id: str, who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            owned(c, conversation_id, who)
            return {"messages": [_wire(r) for r in agent.thread(c, conversation_id)]}

    @app.get("/conversations")
    async def conversations(who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            rows = c.execute(
                """
                select cv.id, cv.status, cv.needs_attention, cv.attention_reason,
                       cv.last_message_at, cu.phone_e164,
                       (select body from messages m where m.conversation_id = cv.id
                         order by m.id desc limit 1) as last_body
                  from conversations cv
                  join customers cu on cu.id = cv.customer_id
                 where cv.business_id = %s
                   and cv.status <> 'closed'
                 order by cv.needs_attention desc, cv.last_message_at desc
                """,
                (who.business_id,),
            ).fetchall()
        return {"conversations": [
            {**r, "id": str(r["id"]), "last_message_at": r["last_message_at"].isoformat()}
            for r in rows]}

    # ------------------------------------------------------- human takeover

    @app.post("/conversations/{conversation_id}/takeover")
    async def takeover(conversation_id: str, payload: dict | None = None,
                       who: auth.Principal = Depends(current)):
        """A person takes the thread. The AI goes silent from here."""
        # The state machine lives in `agent` so it can be tested without a
        # server; these routes are wrappers and translate errors to statuses.
        with scoped(who) as c:
            owned(c, conversation_id, who)
            try:
                return agent.take_over(c, conversation_id,
                                       str((payload or {}).get("as") or "staff"))
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

    @app.post("/conversations/{conversation_id}/handback")
    async def handback(conversation_id: str, who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            owned(c, conversation_id, who)
            try:
                return agent.hand_back(c, app.state.queue, conversation_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

    @app.post("/conversations/{conversation_id}/messages")
    async def staff_message(conversation_id: str, payload: dict,
                            who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            owned(c, conversation_id, who)
            try:
                return agent.staff_reply(c, CHANNELS["simulator"], conversation_id,
                                         str(payload.get("text") or ""))
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except PermissionError as exc:
                raise HTTPException(409, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

    # -------------------------------------------------------------- insights

    @app.get("/insights/daily")
    async def insights_daily(day: str | None = None,
                             who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            return insights.daily(c, who.business_id,
                                  day=date.fromisoformat(day) if day else None)

    @app.get("/insights/overview")
    async def insights_overview(days: int = 7, who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            return insights.overview(c, who.business_id, days=max(1, min(days, 90)))

    @app.get("/activity")
    async def activity(limit: int = 20, who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            return insights.activity(c, who.business_id, limit=max(1, min(limit, 100)))

    @app.get("/follow-ups")
    async def follow_ups(who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            return insights.follow_ups(c, who.business_id)

    @app.get("/leads")
    async def leads(who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            rows = c.execute(
                """
                select l.id, l.tier, l.reason, l.intent, l.confidence, l.updated_at,
                       cu.phone_e164, cu.name, l.status
                  from leads l join customers cu on cu.id = l.customer_id
                 where l.business_id = %s
                 order by case l.tier when 'hot' then 0 when 'warm' then 1 else 2 end,
                          l.updated_at desc
                 limit 200
                """,
                (who.business_id,),
            ).fetchall()
        return {"leads": [{**r, "id": str(r["id"]),
                           "updated_at": r["updated_at"].isoformat()} for r in rows]}

    @app.get("/orders")
    async def orders(who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            rows = c.execute(
                """
                select o.id, o.reference, o.status, o.total_kobo, o.paid_at,
                       o.created_at, cu.phone_e164, cu.name,
                       (select count(*) from order_items i where i.order_id = o.id) as items
                  from orders o join customers cu on cu.id = o.customer_id
                 where o.business_id = %s
                 order by o.created_at desc limit 200
                """,
                (who.business_id,),
            ).fetchall()
        return {"orders": [{**r, "id": str(r["id"]),
                            "created_at": r["created_at"].isoformat(),
                            "paid_at": r["paid_at"].isoformat() if r["paid_at"] else None}
                           for r in rows]}

    # --------------------------------------------------- the Meta handshake

    @app.get("/webhooks/whatsapp")
    async def whatsapp_challenge(request: Request):
        """Meta verifies a webhook by echoing a challenge, before any traffic.

        Unauthenticated by protocol rather than by omission: Meta calls this
        before any session could exist, and the shared token is the credential.
        """
        params = request.query_params
        expected = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        if (params.get("hub.mode") == "subscribe" and expected
                and params.get("hub.verify_token") == expected):
            return PlainTextResponse(params.get("hub.challenge") or "")
        raise HTTPException(403, "verification failed")

    # ------------------------------------------------ the list screens

    @app.get("/catalogue")
    async def catalogue(who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            return views.catalogue(c, who.business_id)

    @app.get("/customers")
    async def customers(who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            return views.customers(c, who.business_id)

    @app.get("/payments")
    async def payments(who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            return views.payments(c, who.business_id)

    @app.get("/search")
    async def search(q: str = "", who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            return views.search(c, who.business_id, q)

    @app.get("/settings")
    async def get_settings(who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            return views.settings(c, who.business_id)

    @app.patch("/settings")
    async def patch_settings(payload: dict, who: auth.Principal = Depends(current)):
        with scoped(who) as c:
            updated, refused = views.update_settings(c, who.business_id, payload)
            if refused:
                # Named rather than silently dropped: a settings form that
                # accepts a key and does nothing with it is one nobody trusts.
                raise HTTPException(
                    400, f"these settings cannot be changed here: {', '.join(refused)}")
            return {"slug": who.business_slug, "settings": updated}

    @app.get("/status")
    async def status(who: auth.Principal = Depends(current)):
        """What the running system actually is.

        Read from the environment rather than by constructing a Chat, because
        constructing one raises when no key is present -- and a status endpoint
        that 500s when the thing it reports on is missing is useless exactly
        when it is needed.
        """
        with scoped(who) as c:
            business = agent.business_by_id(c, who.business_id)
            attention = c.execute(
                "select count(*) as n from conversations where business_id = %s "
                "and needs_attention and status <> 'closed'",
                (who.business_id,),
            ).fetchone()["n"]
            who_owns = views.owner(c, who.business_id)
            is_offline = views.offline(c, who.business_id)

        models = [label for label, var in (
            ("gemini", "GEMINI_API_KEY"), ("groq", "GROQ_API_KEY"),
            ("huggingface", "HF_TOKEN"), ("huggingface", "HUGGINGFACE_API_KEY"),
            ("openrouter", "OPENROUTER_API_KEY"),
        ) if os.environ.get(var)]
        # Deduplicated: HF_TOKEN and HUGGINGFACE_API_KEY are the same provider.
        seen, chain = set(), []
        for name in models:
            if name not in seen:
                seen.add(name)
                chain.append(name)

        return {"business": business.name, "slug": business.slug,
                "channel": os.environ.get("AISALES_CHANNEL", "simulator"),
                "models": chain, "attention": attention,
                "payments": bool(os.environ.get("PAYSTACK_SECRET_KEY")),
                "owner": who_owns, "offline": is_offline,
                "suspended": business.suspended,
                "suspended_reason": business.suspended_reason,
                "user": {"email": who.email, "name": who.name, "role": who.role,
                         "is_operator": who.is_operator}}

    # ------------------------------------------------------- the operator
    #
    # Mounted from its own module so the cross-tenant connection has one home,
    # which `test_boundaries.py` asserts by name.
    app.include_router(operator_mod.build_router(app_dsn=target,
                                                 current_operator=current_operator))

    @app.get("/healthz")
    async def healthz():
        try:
            with conn() as c:
                c.execute("select 1")
        except psycopg.Error as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=503)
        # No job counts here. A count across businesses is a cross-tenant
        # read, and healthz has no tenant -- it is reachable without a session
        # by design, so it reports only that the database answers.
        return {"ok": True}

    return app


def _as_body(payload: dict) -> bytes:
    return json.dumps(payload).encode()


def _bearer(request: Request) -> str:
    """A token in a header, for scripts and for `curl`.

    The browser uses the cookie; this is what makes the API usable from a
    terminal without a cookie jar, which is how the end-to-end script drives
    it.
    """
    header = request.headers.get("authorization") or ""
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def _behind_tls(request: Request) -> bool:
    """Only mark the cookie Secure when the request actually arrived over TLS.

    A Secure cookie on a plain-http development server is a cookie the browser
    silently discards, which presents as "signing in does nothing".
    """
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return proto == "https"


def _principal(who: auth.Principal) -> dict:
    return {"id": who.user_id, "email": who.email, "name": who.name, "role": who.role,
            "business": who.business_slug, "is_operator": who.is_operator}


def _resolve_business(c: psycopg.Connection, message,
                      dsn: str) -> db.Business | None:
    """Which business does this delivery belong to?

    On WhatsApp this is the payload's `phone_number_id`, and the unique index
    on `(provider, external_account_id)` is what makes the answer single-valued
    rather than a guess. The simulator names the business in its payload; a
    delivery that names nothing resolves to nothing, because guessing a tenant
    from an unauthenticated body is how one business's message lands in
    another's inbox.
    """
    phone_id = (message.raw or {}).get("phone_number_id")
    business_id = db.resolve_whatsapp_tenant(c, phone_id)
    if business_id is None:
        return None
    # Read after resolution, inside the tenant it just named -- so the row that
    # comes back is the one the policy allows, not merely the one the id said.
    with db.tenant(dsn, business_id) as scoped:
        return agent.business_by_id(scoped, business_id)


def _wire(row: dict) -> dict:
    return {"id": row["id"], "role": row["role"], "body": row["body"],
            "created_at": row["created_at"].isoformat(),
            "meta": row.get("meta") or {}}
