"""The queue, against a real PostgreSQL.

Not mocked. The properties that matter here -- SKIP LOCKED, lease expiry, the
uniqueness of a live turn -- are properties of the SQL, and a fake would only
test the fake. Skips cleanly when no database is reachable, so the rest of the
suite still runs offline.

The `not_before` test is the important one: it is the entire follow-up
scheduler. If a job due tomorrow can be claimed today, follow-ups fire
immediately and the feature is worse than absent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from aisales import db

# `dsn` and `business_id` come from conftest.


def _expire_lease(dsn: str, job_id: str, *, seconds_ago: int) -> None:
    """Push a lease into the past, standing in for a worker that died."""
    with db.connect(dsn) as conn:
        conn.execute(
            "update jobs set lease_expires_at = now() - make_interval(secs => %s) "
            "where id = %s",
            (float(seconds_ago), job_id),
        )


def test_job_due_later_is_not_claimable(dsn: str, business_id: str) -> None:
    """The follow-up timer. A job scheduled ahead is queued, not available."""
    q = db.Queue(dsn)
    job_id = q.submit("follow_up", business_id=business_id,
                      not_before=datetime.now(timezone.utc) + timedelta(hours=2))
    assert job_id is not None
    assert q.claim("worker-a") is None, "claimed a job that is not due yet"

    # And it becomes claimable once due, without any sweep running.
    with db.connect(dsn) as conn:
        conn.execute("update jobs set not_before = now() - interval '1 second' "
                     "where id = %s", (job_id,))
    claimed = q.claim("worker-a")
    assert claimed is not None and claimed.id == job_id


def test_two_workers_never_get_the_same_job(dsn: str, business_id: str) -> None:
    q = db.Queue(dsn)
    first = q.submit("agent_turn", business_id=business_id)
    second = q.submit("agent_turn", business_id=business_id)

    a = q.claim("worker-a")
    b = q.claim("worker-b")
    assert a is not None and b is not None
    assert a.id != b.id, "two workers claimed the same job"
    assert {a.id, b.id} == {first, second}


def test_leased_job_is_hidden_from_other_workers(dsn: str, business_id: str) -> None:
    q = db.Queue(dsn)
    q.submit("agent_turn", business_id=business_id)
    assert q.claim("worker-a") is not None
    assert q.claim("worker-b") is None, "a live lease did not hide the job"


def test_expired_lease_is_reclaimed_by_next_claim(dsn: str, business_id: str) -> None:
    """A dead worker's job is picked up without waiting for a periodic sweep."""
    q = db.Queue(dsn)
    job_id = q.submit("agent_turn", business_id=business_id)
    first = q.claim("worker-a")
    assert first is not None and first.id == job_id

    _expire_lease(dsn, job_id, seconds_ago=1)
    second = q.claim("worker-b")
    assert second is not None and second.id == job_id
    assert second.attempt == 2, "the reclaimer should count as a new attempt"


def test_heartbeat_reports_a_lost_lease(dsn: str, business_id: str) -> None:
    """The return value is the point: False means stop, do not finish."""
    q = db.Queue(dsn)
    job_id = q.submit("agent_turn", business_id=business_id)
    held = q.claim("worker-a")
    assert held is not None

    assert q.heartbeat(held, "worker-a") is True
    assert q.heartbeat(held, "worker-b") is False, "wrong owner extended the lease"

    _expire_lease(dsn, job_id, seconds_ago=1)
    q.claim("worker-b")
    assert q.heartbeat(held, "worker-a") is False, "a reaped worker kept its lease"


def test_reap_only_touches_jobs_past_the_grace(dsn: str, business_id: str) -> None:
    q = db.Queue(dsn)
    fresh = q.submit("agent_turn", business_id=business_id)
    stale = q.submit("agent_turn", business_id=business_id)
    q.claim("worker-a")
    q.claim("worker-b")

    _expire_lease(dsn, stale, seconds_ago=db.REAP_GRACE_SECONDS + 60)
    _expire_lease(dsn, fresh, seconds_ago=1)

    reaped = q.reap()
    assert stale in reaped
    assert fresh not in reaped, "reaped a job whose worker may still be heartbeating"


def test_idempotency_key_queues_once(dsn: str, business_id: str) -> None:
    """A redelivered webhook must not buy the same thing twice."""
    q = db.Queue(dsn)
    key = f"agent:{uuid.uuid4()}"
    first = q.submit("agent_turn", business_id=business_id, idempotency_key=key)
    second = q.submit("agent_turn", business_id=business_id, idempotency_key=key)
    assert first == second
    assert q.counts(business_id).get("queued", 0) == 1


def test_one_live_turn_per_conversation(dsn: str, business_id: str) -> None:
    """A customer sending three messages in four seconds gets one reply."""
    q = db.Queue(dsn)
    with db.connect(dsn) as conn:
        customer = conn.execute(
            "insert into customers (business_id, phone_e164) values (%s, %s) "
            "returning id", (business_id, "+2348031234567")).fetchone()
        conv = conn.execute(
            "insert into conversations (business_id, customer_id) values (%s, %s) "
            "returning id", (business_id, customer["id"])).fetchone()
    cid = str(conv["id"])

    assert q.submit("agent_turn", business_id=business_id, conversation_id=cid) is not None
    assert q.submit("agent_turn", business_id=business_id, conversation_id=cid) is None
    assert q.submit("agent_turn", business_id=business_id, conversation_id=cid) is None

    # A follow-up is a different kind, so the turn index does not block it.
    assert q.submit("follow_up", business_id=business_id, conversation_id=cid) is not None


def test_fail_with_delay_requeues_and_without_stops(dsn: str, business_id: str) -> None:
    q = db.Queue(dsn)
    retrying = q.submit("agent_turn", business_id=business_id)
    terminal = q.submit("agent_turn", business_id=business_id)
    retrying_job = q.claim("worker-a")
    terminal_job = q.claim("worker-a")
    assert retrying_job is not None and terminal_job is not None
    # Claim order is by not_before then created_at; take them explicitly.
    with db.connect(dsn) as conn:
        conn.execute("update jobs set lease_owner = %s where id = %s",
                     ("worker-a", retrying))
        conn.execute("update jobs set lease_owner = %s where id = %s",
                     ("worker-a", terminal))

    assert q.fail(retrying_job, "worker-a", code="quota", message="out of quota",
                  delay_seconds=60) is True
    assert q.fail(terminal_job, "worker-a", code="declined", message="will never work",
                  delay_seconds=None) is True

    with db.connect(dsn) as conn:
        rows = {str(r["id"]): r for r in conn.execute(
            "select id, status, error_code from jobs where id = any(%s)",
            ([retrying, terminal],)).fetchall()}
    assert rows[retrying]["status"] == "queued"
    assert rows[retrying]["error_code"] == "quota"
    assert rows[terminal]["status"] == "failed"

    # The requeued job is not claimable until its delay elapses.
    assert q.claim("worker-a") is None


def test_attempts_are_bounded(dsn: str, business_id: str) -> None:
    q = db.Queue(dsn)
    q.submit("agent_turn", business_id=business_id, max_attempts=1)
    held = q.claim("worker-a")
    assert held is not None
    # attempt is now 1 == max_attempts, so a retry must stop rather than loop.
    assert q.fail(held, "worker-a", code="boom", message="x",
                  delay_seconds=5) is True
    assert q.get(held)["status"] == "failed"
