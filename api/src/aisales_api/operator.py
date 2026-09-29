"""The console for whoever runs the deployment.

This module is the only place in the API allowed to open a cross-tenant
connection, and `test_boundaries.py` asserts exactly that -- the file list it
permits is a set with one member. Everything else reaches a business through a
session, and this reaches all of them through `AISALES_ADMIN_DSN`.

Three rules, and they are the whole design:

**One module.** The boundary is a file, so it can be read in one sitting and
tested by name. A second module reaching for the operator connection fails the
suite rather than passing review.

**Every route is gated on `current_operator`, and every action is audited.**
The audience is the platform operator, never a business's own staff. A console
whose use is invisible is one nobody can review, and "who looked at this
customer's messages" is a question a business is entitled to an answer to.

**Unconfigured is a state, not an error.** With `AISALES_ADMIN_DSN` unset the
routes exist and answer 503 saying so. That is the correct state for a
deployment that runs one business, and it is much easier to diagnose than a
500 or a route that is simply absent.
"""

from __future__ import annotations

import os

import psycopg
from fastapi import APIRouter, Depends, HTTPException

from aisales import agent, auth, db, secrets

#: Where the reads happen. Cross-tenant by construction.
def _admin_dsn() -> str:
    dsn = os.environ.get(db.OPERATOR_DSN_VAR) or ""
    if not dsn:
        # 503 rather than 500: nothing is broken, the console is switched off.
        raise HTTPException(
            503,
            f"the operator console is not configured: {db.OPERATOR_DSN_VAR} is "
            f"unset. That is the right state for a deployment that runs one "
            f"business. Set it to a DSN for a role that is a member of "
            f"{db.OPERATOR_ROLE} to switch this on.",
        )
    return dsn


def build_router(*, app_dsn: str, current_operator) -> APIRouter:
    """The routes, bound to the guards they share with the rest of the app."""
    router = APIRouter(prefix="/operator", tags=["operator"])

    # ------------------------------------------------------------- reading

    @router.get("/businesses")
    async def businesses(who: auth.Principal = Depends(current_operator)):
        """Every business, with what you would want to know about it."""
        with db.operator(_admin_dsn(), actor=who.email) as conn:
            rows = conn.execute(
                """
                select b.id, b.slug, b.name, b.created_at,
                       b.suspended_at, b.suspended_reason,
                       (select count(*) from business_members m
                         where m.business_id = b.id) as members,
                       (select count(*) from conversations c
                         where c.business_id = b.id and c.status <> 'closed') as open_threads,
                       (select count(*) from messages msg
                         where msg.business_id = b.id
                           and msg.created_at > now() - interval '30 days') as messages_30d,
                       (select count(*) from leads l
                         where l.business_id = b.id) as leads,
                       (select count(*) from orders o
                         where o.business_id = b.id) as orders
                  from businesses b
                 order by b.created_at
                """
            ).fetchall()
            integrations = conn.execute(
                """
                select business_id, provider, external_account_id, status
                  from business_integrations order by provider
                """
            ).fetchall()

        by_business: dict[str, dict] = {}
        for row in integrations:
            by_business.setdefault(str(row["business_id"]), {})[row["provider"]] = {
                "account": row["external_account_id"], "status": row["status"]}

        return {"businesses": [
            {
                "id": str(row["id"]), "slug": row["slug"], "name": row["name"],
                "created_at": row["created_at"].isoformat(),
                "suspended": row["suspended_at"] is not None,
                "suspended_at": row["suspended_at"].isoformat() if row["suspended_at"] else None,
                "suspended_reason": row["suspended_reason"],
                "members": row["members"], "open_threads": row["open_threads"],
                "messages_30d": row["messages_30d"], "leads": row["leads"],
                "orders": row["orders"],
                "integrations": by_business.get(str(row["id"]), {}),
            }
            for row in rows]}

    @router.get("/audit")
    async def audit_log(who: auth.Principal = Depends(current_operator),
                        limit: int = 50):
        """What operators have done, newest first."""
        with db.operator(_admin_dsn(), actor=who.email) as conn:
            rows = conn.execute(
                """
                select a.actor, a.note, a.business_id, a.detail, a.at, b.slug
                  from operator_audit a
                  left join businesses b on b.id = a.business_id
                 order by a.at desc limit %s
                """,
                (max(1, min(limit, 200)),),
            ).fetchall()
        return {"audit": [
            {"actor": row["actor"], "action": row["note"], "slug": row["slug"],
             "detail": row["detail"] or {}, "at": row["at"].isoformat()}
            for row in rows]}

    # ------------------------------------------------------------ onboarding

    @router.post("/businesses", status_code=201)
    async def onboard(payload: dict, who: auth.Principal = Depends(current_operator)):
        """Create a business, its owner, and whatever integrations were given.

        All of it on the operator connection, because the business does not
        exist yet and there is no tenant to open. The one flow that is
        cross-tenant from beginning to end.
        """
        slug = str(payload.get("slug") or "").strip().lower()
        name = str(payload.get("name") or "").strip()
        email = str(payload.get("owner_email") or "").strip()
        password = str(payload.get("owner_password") or "")
        if not slug or not name or not email or not password:
            raise HTTPException(
                400, "slug, name, owner_email and owner_password are all required")

        dsn = _admin_dsn()
        with db.operator(dsn, actor=who.email) as conn:
            exists = conn.execute("select 1 from businesses where slug = %s",
                                  (slug,)).fetchone()
            if exists:
                raise HTTPException(409, f"{slug!r} already exists")

            business = conn.execute(
                "insert into businesses (slug, name) values (%s, %s) "
                "returning id, slug, name", (slug, name)).fetchone()
            business_id = str(business["id"])

            try:
                user_id = auth.create_user(conn, email, password,
                                           name=payload.get("owner_name"))
            except auth.AuthError as exc:
                # The business row is in the same transaction, so it goes back
                # too -- a half-onboarded business with no owner is worse than
                # no business.
                raise HTTPException(409, str(exc)) from exc
            auth.add_member(conn, user_id, business_id, role="owner")

            for provider, account in (
                ("whatsapp", payload.get("whatsapp_number_id")),
                ("paystack", payload.get("paystack_account")),
            ):
                if account:
                    conn.execute(
                        "insert into business_integrations "
                        "(business_id, provider, external_account_id) values (%s, %s, %s)",
                        (business_id, provider, str(account).strip()))

            db.operator_action(conn, actor=who.email, action="business.created",
                               business_id=business_id,
                               detail={"slug": slug, "owner": email})

        # Secrets after the business exists, on a tenant connection, so they
        # are written by the same path the worker reads them through.
        stored = _store_secrets(app_dsn, business_id, payload)
        return {"id": business_id, "slug": business["slug"],
                "name": business["name"], "owner": email, "secrets_stored": stored}

    def _store_secrets(target_dsn: str, business_id: str, payload: dict) -> list[str]:
        pairs = [
            ("whatsapp", "access_token", payload.get("whatsapp_token")),
            ("whatsapp", "app_secret", payload.get("whatsapp_app_secret")),
            ("paystack", "secret_key", payload.get("paystack_secret_key")),
        ]
        given = [(p, t, v) for p, t, v in pairs if v]
        if not given:
            return []
        if not os.environ.get("AISALES_SECRET_KEY"):
            raise HTTPException(
                400,
                "credentials were given but AISALES_SECRET_KEY is not set, so "
                "nothing can be encrypted. Set it, or onboard without them.",
            )
        with db.tenant(target_dsn, business_id) as conn:
            for provider, secret_type, value in given:
                secrets.put(conn, business_id=business_id, provider=provider,
                            secret_type=secret_type, value=str(value))
        return [f"{p}/{t}" for p, t, _ in given]

    # ------------------------------------------------------------- suspension

    @router.post("/businesses/{business_id}/suspend")
    async def suspend(business_id: str, payload: dict | None = None,
                      who: auth.Principal = Depends(current_operator)):
        """Stop the agent answering. Customers are still recorded."""
        reason = str((payload or {}).get("reason") or "")
        # The tenant connection is the one that acts, so the 404 belongs here:
        # a business that does not exist has no tenant to open, and
        # `db.tenant` would happily set a context for a uuid naming nothing.
        with db.tenant(app_dsn, business_id) as conn:
            _require_business(conn, business_id)
            outcome = agent.suspend_business(conn, business_id, reason=reason)
        _note(who, "business.suspended", business_id, {"reason": reason})
        return {"suspended": True, **outcome}

    @router.post("/businesses/{business_id}/resume")
    async def resume(business_id: str, who: auth.Principal = Depends(current_operator)):
        """Start answering again, and catch up inside the service window."""
        with db.tenant(app_dsn, business_id) as conn:
            _require_business(conn, business_id)
            outcome = agent.resume_business(conn, db.Queue(app_dsn), business_id)
        # After the work, not before: an audit row for something that then
        # 404'd would be a record of an action nobody took.
        _note(who, "business.resumed", business_id, outcome)
        return {"suspended": False, **outcome}

    def _note(who: auth.Principal, action: str, business_id: str,
              detail: dict | None = None) -> None:
        with db.operator(_admin_dsn(), actor=who.email) as conn:
            db.operator_action(conn, actor=who.email, action=action,
                               business_id=business_id, detail=detail)

    return router


def _require_business(conn: psycopg.Connection, business_id: str) -> None:
    """404 for an id that names nothing, so a typo is not a silent success."""
    found = conn.execute("select 1 from businesses where id = %s", (business_id,)).fetchone()
    if found is None:
        raise HTTPException(404, "no such business")
