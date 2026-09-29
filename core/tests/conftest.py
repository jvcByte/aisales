"""Shared fixtures.

Database-backed tests skip rather than fail when no database is reachable, so
the rest of the suite -- the guard, the phone normaliser, the intent golden
set -- still runs on a machine with nothing installed.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from aisales import db

DSN = os.environ.get("AISALES_TEST_DSN", "postgresql:///aisales_test")


@pytest.fixture(scope="module")
def dsn() -> str:
    try:
        with psycopg.connect(DSN):
            pass
    except psycopg.Error as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no test database at {DSN}: {exc}")
    db.install(DSN)
    return DSN


@pytest.fixture
def business(dsn: str):
    """A fresh business per test, so tests cannot see each other's rows."""
    with db.connect(dsn) as conn:
        row = conn.execute(
            "insert into businesses (slug, name, settings) values (%s, %s, %s) "
            "returning id, slug, name, settings",
            (f"t-{uuid.uuid4().hex[:8]}", "Test Business", "{}"),
        ).fetchone()
    yield db.Business.from_row(row)
    with db.connect(dsn) as conn:
        conn.execute("delete from businesses where id = %s", (row["id"],))


@pytest.fixture
def business_id(business: db.Business) -> str:
    return business.id
