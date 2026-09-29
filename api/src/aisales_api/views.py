"""Read models for the dashboard's list screens.

Deliberately separate from `insights.py`, which is about *aggregates* -- sums,
counts, trends. These are the plain lists behind Catalogue, Customers, Payments
and the search box, and mixing the two would make one module that answers two
different kinds of question.

Everything here is business-scoped. Every query carries `business_id`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg


def _naira(kobo: int | None) -> int:
    return int(kobo or 0)


def catalogue(conn: psycopg.Connection, business_id: str) -> dict:
    rows = conn.execute(
        """
        select p.id, p.sku, p.name, p.description, p.price_kobo, p.stock_qty,
               p.variants, p.image_url, p.active,
               (select coalesce(sum(i.qty), 0)::int from order_items i
                 join orders o on o.id = i.order_id
                where i.product_id = p.id and o.status in ('paid', 'fulfilled'))
                 as sold
          from products p
         where p.business_id = %s
         order by p.active desc, p.name
        """,
        (business_id,),
    ).fetchall()
    return {"products": [
        {"id": str(r["id"]), "sku": r["sku"], "name": r["name"],
         "description": r["description"], "price_kobo": r["price_kobo"],
         "stock_qty": r["stock_qty"], "variants": r["variants"] or [],
         "image_url": r["image_url"], "active": r["active"], "sold": r["sold"]}
        for r in rows]}


def customers(conn: psycopg.Connection, business_id: str) -> dict:
    rows = conn.execute(
        """
        select cu.id, cu.phone_e164, cu.name, cu.notes, cu.first_seen_at,
               cu.last_seen_at,
               (select count(*) from conversations c where c.customer_id = cu.id)
                 as conversations,
               (select count(*) from orders o where o.customer_id = cu.id) as orders,
               (select coalesce(sum(o.total_kobo), 0)::bigint from orders o
                 where o.customer_id = cu.id and o.status in ('paid', 'fulfilled'))
                 as spent_kobo,
               (select l.tier from leads l
                 where l.customer_id = cu.id and l.status = 'open' limit 1) as tier
          from customers cu
         where cu.business_id = %s
         order by cu.last_seen_at desc limit 200
        """,
        (business_id,),
    ).fetchall()
    return {"customers": [
        {"id": str(r["id"]), "phone_e164": r["phone_e164"], "name": r["name"],
         "notes": r["notes"] or {}, "conversations": r["conversations"],
         "orders": r["orders"], "spent_kobo": _naira(r["spent_kobo"]),
         "tier": r["tier"],
         "first_seen_at": r["first_seen_at"].isoformat(),
         "last_seen_at": r["last_seen_at"].isoformat()}
        for r in rows]}


def payments(conn: psycopg.Connection, business_id: str) -> dict:
    rows = conn.execute(
        """
        select p.id, p.paystack_reference, p.amount_kobo, p.status, p.channel,
               p.verified_at, p.created_at, o.reference as order_reference,
               cu.phone_e164, cu.name
          from payments p
          left join orders o on o.id = p.order_id
          left join customers cu on cu.id = p.customer_id
         where p.business_id = %s
         order by p.created_at desc limit 200
        """,
        (business_id,),
    ).fetchall()
    settled = sum(r["amount_kobo"] for r in rows if r["status"] == "success")
    pending = sum(r["amount_kobo"] for r in rows if r["status"] == "pending")
    return {
        "payments": [
            {"id": str(r["id"]), "reference": r["paystack_reference"],
             "amount_kobo": r["amount_kobo"], "status": r["status"],
             "channel": r["channel"],
             "order_reference": r["order_reference"],
             "phone_e164": r["phone_e164"], "name": r["name"],
             "created_at": r["created_at"].isoformat(),
             "verified_at": r["verified_at"].isoformat() if r["verified_at"] else None}
            for r in rows],
        "settled_kobo": settled,
        "pending_kobo": pending,
    }


def search(conn: psycopg.Connection, business_id: str, query: str) -> dict:
    """One box across customers, orders and products.

    Deliberately three small queries rather than one union: the result shapes
    differ, and a union would return a column set no screen can render without
    re-splitting it.
    """
    term = f"%{query.strip().lower()}%"
    if not query.strip():
        return {"customers": [], "orders": [], "products": []}

    # Digits only, for a phone or an order reference.
    digits = "".join(ch for ch in query if ch.isdigit())

    found_customers = conn.execute(
        """
        select id, phone_e164, name from customers
         where business_id = %s
           and (lower(coalesce(name, '')) like %s
                or (%s <> '' and phone_e164 like %s))
         order by last_seen_at desc limit 6
        """,
        (business_id, term, digits, f"%{digits}%"),
    ).fetchall()

    found_orders = conn.execute(
        """
        select o.id, o.reference, o.status, o.total_kobo, cu.phone_e164
          from orders o join customers cu on cu.id = o.customer_id
         where o.business_id = %s and lower(o.reference) like %s
         order by o.created_at desc limit 6
        """,
        (business_id, term),
    ).fetchall()

    found_products = conn.execute(
        """
        select id, sku, name, price_kobo, stock_qty from products
         where business_id = %s and active and search_text like %s
         order by name limit 6
        """,
        (business_id, term),
    ).fetchall()

    return {
        "customers": [{"id": str(r["id"]), "phone_e164": r["phone_e164"],
                       "name": r["name"]} for r in found_customers],
        "orders": [{"id": str(r["id"]), "reference": r["reference"],
                    "status": r["status"], "total_kobo": r["total_kobo"],
                    "phone_e164": r["phone_e164"]} for r in found_orders],
        "products": [{"id": str(r["id"]), "sku": r["sku"], "name": r["name"],
                      "price_kobo": r["price_kobo"], "stock_qty": r["stock_qty"]}
                     for r in found_products],
    }


#: The settings the dashboard may change. A closed set, because the alternative
#: is a PATCH endpoint that writes any key a caller names into the blob the
#: agent reads its behaviour from -- which is a way to turn the AI off, or to
#: point it at a different business, from a form.
EDITABLE = frozenset({
    "tone", "confidence_threshold", "approved_facts", "privacy_notice",
    "owner_name", "escalate_on", "flag_on", "followup",
})


def settings(conn: psycopg.Connection, business_id: str) -> dict:
    row = conn.execute(
        "select id, slug, name, settings from businesses where id = %s", (business_id,)
    ).fetchone()
    return {"id": str(row["id"]), "slug": row["slug"], "name": row["name"],
            "settings": row["settings"] or {}}


def update_settings(conn: psycopg.Connection, business_id: str,
                    patch: dict) -> tuple[dict, list[str]]:
    """Merge the editable subset. Returns the new settings and what was refused."""
    refused = sorted(k for k in patch if k not in EDITABLE)
    clean = {k: v for k, v in patch.items() if k in EDITABLE}
    if not clean:
        return settings(conn, business_id)["settings"], refused

    row = conn.execute(
        """
        update businesses set settings = settings || %s::jsonb
         where id = %s returning settings
        """,
        (psycopg.types.json.Json(clean), business_id),
    ).fetchone()
    return row["settings"] or {}, refused


def owner(conn: psycopg.Connection, business_id: str) -> dict:
    """Who owns the shop, for the header.

    Read from settings rather than invented: a dashboard that greets a
    hardcoded name is greeting somebody who may not exist. Falls back to the
    business itself and derives initials from whatever it finds.
    """
    row = conn.execute(
        "select name, settings from businesses where id = %s", (business_id,)
    ).fetchone()
    configured = str((row["settings"] or {}).get("owner_name") or "").strip()
    display = configured or row["name"]
    parts = [p for p in display.replace("'", " ").split() if p]
    initials = "".join(p[0].upper() for p in parts[:2]) or "?"
    return {"name": display, "business": row["name"], "initials": initials,
            "configured": bool(configured)}


def offline(conn: psycopg.Connection, business_id: str) -> bool:
    """Whether anything external is actually connected.

    Used for the "Simulator (Offline)" badge. Reported rather than assumed, so
    it stops saying Offline the moment a channel is wired up.
    """
    import os

    return (os.environ.get("AISALES_CHANNEL", "simulator") != "whatsapp"
            or not os.environ.get("WHATSAPP_TOKEN"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
