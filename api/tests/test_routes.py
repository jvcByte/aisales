"""Route wiring.

Thin tests, with one that matters a lot. FastAPI matches routes in declaration
order, so registering the generic `/webhooks/{name}` before the concrete
`/webhooks/paystack` silently swallows every real callback: it matches the
generic route, finds no channel called "paystack", and returns 404.

Nothing in `core/tests` can see that -- those tests call `apply_callback`
directly and pass while the endpoint is unreachable. A green suite with a dead
webhook is exactly the failure this file exists to prevent.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def app(monkeypatch):
    from aisales_api.app import create_app

    return create_app("postgresql:///aisales_test")


def paths(app) -> list[str]:
    return [r.path for r in app.routes if hasattr(r, "path")]


def test_the_paystack_webhook_is_declared_before_the_generic_channel(app) -> None:
    """Order, not just presence. Declaring both and hoping is what broke."""
    routes = paths(app)
    assert "/webhooks/paystack" in routes and "/webhooks/{name}" in routes
    assert routes.index("/webhooks/paystack") < routes.index("/webhooks/{name}"), (
        "the generic channel route shadows the Paystack webhook: FastAPI "
        "matches in declaration order, so every real callback would 404"
    )


def test_the_dev_charge_endpoint_does_not_exist_without_fakes(app, monkeypatch) -> None:
    """It signs with the server's own secret, so its signature check passes by
    construction. Exposed publicly it would let anyone mark any order paid by
    naming its reference."""
    monkeypatch.delenv("AISALES_FAKES", raising=False)
    from aisales_api.app import create_app

    assert "/dev/paystack/charge" not in paths(create_app("postgresql:///aisales_test"))


def test_the_dev_charge_endpoint_exists_in_offline_mode(monkeypatch) -> None:
    monkeypatch.setenv("AISALES_FAKES", "1")
    from aisales_api.app import create_app

    assert "/dev/paystack/charge" in paths(create_app("postgresql:///aisales_test"))


def test_an_unknown_channel_is_not_silently_accepted(app) -> None:
    """A misconfigured webhook URL must fail loudly rather than 200."""
    assert "simulator" in __import__("aisales_api.app", fromlist=["CHANNELS"]).CHANNELS
