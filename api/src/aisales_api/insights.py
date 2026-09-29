"""The daily digest: SQL and a sentence, not a model call.

Every figure here is a count or a sum over rows that were written for another
reason, which is what makes it trustworthy -- it cannot report something that
did not happen. The one narrative part, "why they did not buy", is text the
model already wrote at the time (`leads.reason`), so the digest is arranging
facts rather than generating them.

ponytail: the ceiling is that it can only notice patterns somebody thought to
COUNT. It will never say "people keep asking for a size you do not stock"
unless a query was written for it. Upgrade path is one summarisation call over
the day's rows, run through the guard so it cannot invent a figure the SQL did
not produce. Worth doing once the pilot shows which questions the owner
actually asks.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg

LAGOS = ZoneInfo("Africa/Lagos")


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """A Lagos day as a UTC half-open interval.

    Computed with a real timezone rather than a fixed offset. Lagos has no DST
    today, which is exactly why someone will hardcode +1 and be wrong the day
    that changes -- and a digest whose day boundary is an hour out quietly
    attributes sales to the wrong date.
    """
    start = datetime.combine(day, datetime.min.time(), tzinfo=LAGOS)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def overview(conn: psycopg.Connection, business_id: str, *, days: int = 7) -> dict:
    """Everything the dashboard home needs, in one call.

    One endpoint rather than four because the page renders them together: four
    round-trips would give four independent loading states for one screen, and
    the owner would watch the numbers arrive at different times.
    """
    today = datetime.now(LAGOS).date()
    # The window is [start of the first day, start of tomorrow).
    #
    # `_day_bounds` returns a half-open interval, so the *second* element of
    # today's bounds is exactly the end. Taking the first element of a
    # different day's bounds instead -- as this did -- spans one day rather
    # than seven, and the chart renders empty while every figure beside it
    # stays correct, which is the hardest kind of wrong to notice.
    start, _ = _day_bounds(today - timedelta(days=days - 1))
    _, end_of_today = _day_bounds(today)
    previous_start, _ = _day_bounds(today - timedelta(days=2 * days - 1))

    def counts(from_ts, to_ts) -> dict:
        row = conn.execute(
            """
            select
              (select count(*) from conversations
                where business_id = %(b)s and created_at >= %(f)s and created_at < %(t)s)
                as conversations,
              (select count(*) from leads
                where business_id = %(b)s and created_at >= %(f)s and created_at < %(t)s)
                as leads,
              (select count(*) from orders
                where business_id = %(b)s and created_at >= %(f)s and created_at < %(t)s)
                as orders,
              (select count(*) from payments
                where business_id = %(b)s and status = 'success'
                  and verified_at >= %(f)s and verified_at < %(t)s)
                as payments,
              -- Still outstanding: quoted, linked, not yet settled. This is the
              -- number a shop owner actually chases, so it is named plainly
              -- rather than left to be inferred from a total.
              (select coalesce(sum(total_kobo), 0)::bigint from orders
                where business_id = %(b)s and status = 'awaiting_payment'
                  and created_at >= %(f)s and created_at < %(t)s)
                as pending_kobo,
              (select coalesce(sum(amount_kobo), 0)::bigint from payments
                where business_id = %(b)s and status = 'success'
                  and verified_at >= %(f)s and verified_at < %(t)s)
                as collected_kobo
            """,
            {"b": business_id, "f": from_ts, "t": to_ts},
        ).fetchone()
        return {k: (int(v) if v is not None else 0) for k, v in row.items()}

    now_counts = counts(start, end_of_today)
    before_counts = counts(previous_start, start)

    series = conn.execute(
        """
        select d::date as day,
          (select count(*) from conversations c
            where c.business_id = %(b)s and c.created_at >= d and c.created_at < d + interval '1 day')
            as conversations,
          (select count(*) from leads l
            where l.business_id = %(b)s and l.created_at >= d and l.created_at < d + interval '1 day')
            as leads,
          (select count(*) from orders o
            where o.business_id = %(b)s and o.created_at >= d and o.created_at < d + interval '1 day')
            as orders
        -- The endpoint is start-of-tomorrow, and generate_series is inclusive,
        -- so a second is subtracted to stop at today. Without it the chart
        -- grows a trailing empty bar for a day that has not happened.
        from generate_series(%(f)s::timestamptz,
                             %(t)s::timestamptz - interval '1 second',
                             interval '1 day') d
        order by d
        """,
        {"b": business_id, "f": start, "t": end_of_today},
    ).fetchall()

    top = conn.execute(
        """
        select i.name, sum(i.qty)::int as sold,
               sum(i.line_total_kobo)::bigint as kobo
          from order_items i join orders o on o.id = i.order_id
         where i.business_id = %s and o.status in ('paid', 'fulfilled')
           and o.created_at >= %s
         group by i.name order by sold desc, i.name limit 5
        """,
        (business_id, start),
    ).fetchall()

    attention = conn.execute(
        "select count(*) as n from conversations "
        "where business_id = %s and needs_attention and status <> 'closed'",
        (business_id,),
    ).fetchone()["n"]

    # The two questions the vision asks the owner to be able to answer, over
    # the same window as everything else on the page -- a seven-day headline
    # sitting above a one-day list invites exactly the wrong comparison.
    most_asked = conn.execute(
        """
        select meta->>'intent' as intent, count(*) as n from messages
         where business_id = %s and role = 'customer' and created_at >= %s
           and meta->>'intent' is not null and meta->>'intent' <> 'unknown'
         group by 1 order by n desc, intent limit 5
        """,
        (business_id, start),
    ).fetchall()

    why_not = conn.execute(
        """
        select reason, count(*) as n from leads
         where business_id = %s and status = 'lost' and updated_at >= %s
         group by 1 order by n desc, reason limit 5
        """,
        (business_id, start),
    ).fetchall()

    blocked = conn.execute(
        "select count(*) as n from audit_log where business_id = %s "
        "and action = 'guard_blocked' and created_at >= %s",
        (business_id, start),
    ).fetchone()["n"]

    return {
        "days": days,
        "since": (today - timedelta(days=days - 1)).isoformat(),
        "now": now_counts,
        "before": before_counts,
        # The comparison period named in words, not as a bare percentage: a
        # delta with no baseline is not information.
        "compared_with": f"the previous {days} days",
        "series": [{"day": r["day"].isoformat(), "conversations": r["conversations"],
                    "leads": r["leads"], "orders": r["orders"]} for r in series],
        "top_products": [{"name": r["name"], "sold": r["sold"], "kobo": r["kobo"]}
                         for r in top],
        "needing_attention": attention,
        "most_asked": [{"intent": r["intent"], "count": r["n"]} for r in most_asked],
        "why_they_did_not_buy": [{"reason": r["reason"], "count": r["n"]}
                                 for r in why_not],
        "guard_blocked": blocked,
    }


def activity(conn: psycopg.Connection, business_id: str, *, limit: int = 20) -> dict:
    """What the AI has actually done, newest first.

    Read straight from the audit log rather than from a separate feed table:
    the log is already append-only and already written on every consequential
    action, so a second record would be a second thing to keep in step.
    """
    rows = conn.execute(
        """
        select a.id, a.action, a.actor, a.detail, a.model, a.created_at,
               a.conversation_id, cu.phone_e164
          from audit_log a
          left join conversations cv on cv.id = a.conversation_id
          left join customers cu on cu.id = cv.customer_id
         where a.business_id = %s
         order by a.id desc limit %s
        """,
        (business_id, limit),
    ).fetchall()
    return {"activity": [
        {"id": r["id"], "action": r["action"], "actor": r["actor"],
         "detail": r["detail"] or {}, "model": r["model"],
         "created_at": r["created_at"].isoformat(),
         "conversation_id": str(r["conversation_id"]) if r["conversation_id"] else None,
         "phone_e164": r["phone_e164"]}
        for r in rows]}


def follow_ups(conn: psycopg.Connection, business_id: str) -> dict:
    """Scheduled and past follow-ups, soonest first.

    Includes the ones that were skipped and why. "Why did nobody chase this
    customer?" is the question this screen exists to answer, and a list that
    showed only pending rows could not.
    """
    rows = conn.execute(
        """
        select f.id, f.reason, f.due_at, f.status, f.attempt, f.skipped_reason,
               f.created_at, cu.phone_e164, cu.name, f.conversation_id
          from follow_ups f join customers cu on cu.id = f.customer_id
         where f.business_id = %s
         order by case f.status when 'scheduled' then 0 else 1 end, f.due_at desc
         limit 100
        """,
        (business_id,),
    ).fetchall()
    return {"follow_ups": [
        {**{k: r[k] for k in ("id", "reason", "status", "attempt", "skipped_reason",
                              "phone_e164", "name")},
         "id": str(r["id"]),
         "conversation_id": str(r["conversation_id"]),
         "due_at": r["due_at"].isoformat(),
         "created_at": r["created_at"].isoformat()}
        for r in rows]}


def daily(conn: psycopg.Connection, business_id: str, *,
          day: date | None = None) -> dict:
    """One day's trading, and the day to compare it with."""
    if day is None:
        day = datetime.now(LAGOS).date()
    start, end = _day_bounds(day)
    prev_start, _prev_end = _day_bounds(day - timedelta(days=1))

    # `::bigint` on every sum, and it is not decoration: `sum(bigint)` returns
    # `numeric` in Postgres, so without the cast a Decimal leaks out of the
    # API and the integer-kobo invariant stops at the database boundary.
    collected = conn.execute(
        """
        select coalesce(sum(amount_kobo), 0)::bigint as kobo, count(*) as n
          from payments
         where business_id = %s and status = 'success'
           and verified_at >= %s and verified_at < %s
        """,
        (business_id, start, end),
    ).fetchone()
    collected_prev = conn.execute(
        """
        select coalesce(sum(amount_kobo), 0)::bigint as kobo
          from payments
         where business_id = %s and status = 'success'
           and verified_at >= %s and verified_at < %s
        """,
        (business_id, prev_start, start),
    ).fetchone()["kobo"]

    conversations = conn.execute(
        """
        select count(*) as n from conversations
         where business_id = %s and created_at >= %s and created_at < %s
        """,
        (business_id, start, end),
    ).fetchone()["n"]

    orders = conn.execute(
        """
        select count(*) as n from orders
         where business_id = %s and created_at >= %s and created_at < %s
        """,
        (business_id, start, end),
    ).fetchone()["n"]

    leads = conn.execute(
        """
        select count(*) as n from leads
         where business_id = %s and created_at >= %s and created_at < %s
        """,
        (business_id, start, end),
    ).fetchone()["n"]

    most_asked = conn.execute(
        """
        select meta->>'intent' as intent, count(*) as n
          from messages
         where business_id = %s and role = 'customer'
           and created_at >= %s and created_at < %s
           and meta->>'intent' is not null and meta->>'intent' <> 'unknown'
         group by 1 order by n desc, intent limit 6
        """,
        (business_id, start, end),
    ).fetchall()

    did_not_buy = conn.execute(
        """
        select reason, count(*) as n from leads
         where business_id = %s and status = 'lost'
           and updated_at >= %s and updated_at < %s
         group by 1 order by n desc limit 6
        """,
        (business_id, start, end),
    ).fetchall()

    # The guard's own error rate, reported as a first-class figure. A guard
    # that blocks often is a guard that is wrong often, and the only way to
    # know is to show the number next to everything else.
    blocked = conn.execute(
        """
        select count(*) as n from audit_log
         where business_id = %s and action = 'guard_blocked'
           and created_at >= %s and created_at < %s
        """,
        (business_id, start, end),
    ).fetchone()["n"]

    attention = conn.execute(
        """
        select count(*) as n from conversations
         where business_id = %s and needs_attention and status <> 'closed'
        """,
        (business_id,),
    ).fetchone()["n"]

    return {
        "day": day.isoformat(),
        "collected_kobo": collected["kobo"],
        "payments": collected["n"],
        "collected_previous_kobo": collected_prev,
        # Named in words rather than as a percentage. A bare "+12%" with no
        # baseline is not information.
        "compared_with": (day - timedelta(days=1)).strftime("%A %d %B"),
        "conversations": conversations,
        "orders": orders,
        "new_leads": leads,
        "needing_attention": attention,
        "most_asked": [{"intent": r["intent"], "count": r["n"]} for r in most_asked],
        "why_they_did_not_buy": [{"reason": r["reason"], "count": r["n"]}
                                 for r in did_not_buy],
        "guard_blocked": blocked,
    }
