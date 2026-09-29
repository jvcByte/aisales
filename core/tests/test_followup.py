"""Follow-ups: the service window, quiet hours, and the dispatcher.

Two of these are the reason the feature exists at all rather than being a
naive `sleep(86400)`:

  * Meta rejects free-form messages outside the 24-hour service window, so a
    follow-up due after it must be *skipped and recorded*, not queued
    hopefully. A next-day default would look correct and silently do nothing.
  * Quiet hours are computed in Africa/Lagos. Hardcoding +1 works today and is
    exactly the kind of thing that breaks without anyone touching it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
import pytest

from aisales import agent, db, followup, providers
from aisales.channels import Inbound

LAGOS = ZoneInfo("Africa/Lagos")
PHONE = "+2348031234567"


class Chan:
    name = "test"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def verify(self, b, h): return True

    def parse(self, b, h): return []

    def send(self, m):
        self.sent.append(m.text)
        return "x"


POLICY = followup.FollowUpPolicy(first_after_minutes=120, max_attempts=2,
                                 quiet_start="21:00", quiet_end="08:00")


# --------------------------------------------------------- quiet hours


def test_quiet_hours_cross_midnight() -> None:
    """21:00-08:00 is the normal case and it wraps, which is what a naive
    `start <= now < end` gets wrong."""
    def at(hour, minute=0):
        return datetime(2026, 9, 28, hour, minute, tzinfo=LAGOS)

    assert followup.in_quiet_hours(POLICY, at(22)) is True
    assert followup.in_quiet_hours(POLICY, at(2)) is True
    assert followup.in_quiet_hours(POLICY, at(7, 59)) is True
    assert followup.in_quiet_hours(POLICY, at(8)) is False
    assert followup.in_quiet_hours(POLICY, at(14)) is False
    assert followup.in_quiet_hours(POLICY, at(20, 59)) is False


def test_quiet_hours_are_read_in_lagos_not_utc() -> None:
    """21:30 in Lagos is 20:30 UTC. Read as UTC the window would be an hour
    out, and a follow-up the owner asked never to send at night would go out
    at 21:30 local."""
    lagos_night = datetime(2026, 9, 28, 21, 30, tzinfo=LAGOS)
    assert followup.in_quiet_hours(POLICY, lagos_night) is True
    assert followup.in_quiet_hours(POLICY, lagos_night.astimezone(timezone.utc)) is True

    # 08:30 Lagos is inside the day; 08:30 UTC is 09:30 Lagos, also inside.
    assert followup.in_quiet_hours(POLICY, datetime(2026, 9, 28, 8, 30, tzinfo=LAGOS)) is False


def test_next_open_moment_leaves_a_waking_hour_alone() -> None:
    awake = datetime(2026, 9, 28, 14, 0, tzinfo=LAGOS)
    assert followup.next_open_moment(POLICY, awake) == awake


def test_next_open_moment_moves_to_the_end_of_quiet_hours() -> None:
    night = datetime(2026, 9, 28, 22, 0, tzinfo=LAGOS)
    opened = followup.next_open_moment(POLICY, night)
    assert opened.astimezone(LAGOS).hour == 8
    assert opened.astimezone(LAGOS).day == 29


def test_policy_reads_every_control_from_settings() -> None:
    """Timing, frequency and tone -- all three of the vision's controls, in
    one read in one place."""
    policy = followup.FollowUpPolicy.from_settings({
        "followup": {"first_after_minutes": 45, "max_attempts": 5,
                     "min_gap_minutes": 60, "tone": "playful",
                     "quiet_start": "22:00", "quiet_end": "07:00"}})
    assert policy.first_after_minutes == 45
    assert policy.max_attempts == 5
    assert policy.min_gap_minutes == 60
    assert policy.tone == "playful"
    assert policy.quiet_start == "22:00"


def test_a_missing_settings_block_gives_sane_defaults() -> None:
    policy = followup.FollowUpPolicy.from_settings({})
    assert policy.first_after_minutes == 120       # inside the service window
    assert policy.max_attempts == 2


# ------------------------------------------------------------ scheduling


@pytest.fixture
def setup(dsn: str, business):
    with db.connect(dsn) as conn:
        # Quiet hours OFF, deliberately. The business default is 21:00-08:00,
        # so without this the dispatch tests pass during the working day and
        # fail at night -- a suite that depends on when you run it is worse
        # than one that is missing a case. The deferral itself is covered
        # explicitly by test_quiet_hours_defer_the_send below, which forces
        # the window to cover the present moment instead of hoping for it.
        conn.execute(
            "update businesses set settings = settings || %s::jsonb where id = %s",
            (psycopg.types.json.Json({
                "followup": {"quiet_start": "00:00", "quiet_end": "00:00",
                             "first_after_minutes": 120, "max_attempts": 2,
                             "min_gap_minutes": 0}}), business.id),
        )
        result = agent.record_inbound(conn, db.Queue(dsn), business, Inbound(
            channel="simulator", external_id="f-1", from_phone=PHONE,
            text="I go get back to you"))
        # That message queued an agent turn. These tests are about follow-ups,
        # and `claim` returns the oldest due job -- so leaving it would mean
        # every test claiming the wrong job and asserting on it.
        conn.execute("update jobs set status = 'succeeded', finished_at = now() "
                     "where kind = %s", (agent.TURN,))
    return {"business": business, "conversation": result.conversation_id,
            "customer": result.customer_id, "queue": db.Queue(dsn)}


def _schedule(dsn, setup, **kwargs):
    with db.connect(dsn) as conn:
        conversation = conn.execute(
            "select last_inbound_at from conversations where id = %s",
            (setup["conversation"],)).fetchone()
        return followup.schedule(
            conn, setup["queue"], business_id=setup["business"].id,
            customer_id=setup["customer"], conversation_id=setup["conversation"],
            reason="said they would return",
            policy=kwargs.pop("policy", POLICY),
            last_inbound_at=conversation["last_inbound_at"], **kwargs)


def test_a_follow_up_is_queued_but_not_claimable_yet(dsn, setup) -> None:
    """The timer is `jobs.not_before`. No cron, no sleeping worker."""
    follow_up_id, skip = _schedule(dsn, setup)
    assert follow_up_id and skip is None
    assert setup["queue"].claim("w") is None, "a future follow-up was claimable now"


def test_outside_the_service_window_it_is_skipped_not_queued(dsn, setup) -> None:
    """Meta rejects free-form text more than 24 hours after the customer's
    last message. Queuing it anyway looks correct and does nothing."""
    with db.connect(dsn) as conn:
        conn.execute("update conversations set last_inbound_at = now() - interval '30 hours' "
                     "where id = %s", (setup["conversation"],))
    follow_up_id, skip = _schedule(dsn, setup, channel="whatsapp")
    assert follow_up_id is None
    assert skip == followup.SkipReason.WINDOW_CLOSED


def test_the_window_does_not_apply_to_the_simulator(dsn, setup) -> None:
    """There is no service window on a channel that has no platform behind it,
    and applying one would make the feature untestable."""
    with db.connect(dsn) as conn:
        conn.execute("update conversations set last_inbound_at = now() - interval '30 hours' "
                     "where id = %s", (setup["conversation"],))
    follow_up_id, skip = _schedule(dsn, setup, channel="simulator")
    assert follow_up_id is not None and skip is None


def test_a_second_open_follow_up_is_refused(dsn, setup) -> None:
    """One open follow-up per conversation, so a chatty model cannot schedule
    four and have the customer chased all afternoon."""
    first, _ = _schedule(dsn, setup)
    second, skip = _schedule(dsn, setup)
    assert first is not None
    assert second is None and skip == followup.SkipReason.ALREADY_SCHEDULED


def test_attempts_are_capped(dsn, setup) -> None:
    for _ in range(POLICY.max_attempts):
        scheduled, skip = _schedule(dsn, setup)
        assert skip is None
        with db.connect(dsn) as conn:
            conn.execute("update follow_ups set status = 'sent' where status = 'scheduled'")
    blocked, skip = _schedule(dsn, setup)
    assert blocked is None and skip == followup.SkipReason.CAPPED


def test_a_scheduled_follow_up_becomes_claimable_when_due(dsn, setup) -> None:
    follow_up_id, _ = _schedule(dsn, setup)
    with db.connect(dsn) as conn:
        conn.execute("update follow_ups set due_at = now() - interval '1 minute' where id = %s",
                     (follow_up_id,))
        conn.execute("update jobs set not_before = now() - interval '1 minute'")
    job = setup["queue"].claim("w")
    assert job is not None and job.kind == agent.FOLLOW_UP


# ------------------------------------------------------------ dispatching


def _dispatch(dsn, setup, script, *, channel=None):
    with db.connect(dsn) as conn:
        job = db.Queue(dsn).claim("w")
        assert job is not None, "nothing was claimable"
        chat = providers.ScriptedChat(turns=script)
        channel = channel or Chan()
        outcome = agent.dispatch_follow_up(conn, db.Queue(dsn), job, chat=chat,
                                           channel=channel)
    return outcome, channel


def _due(dsn, setup):
    follow_up_id, _ = _schedule(dsn, setup)
    with db.connect(dsn) as conn:
        conn.execute("update follow_ups set due_at = now() - interval '1 minute' where id = %s",
                     (follow_up_id,))
        conn.execute("update jobs set not_before = now() - interval '1 minute'")
    return follow_up_id


def test_a_due_follow_up_is_sent(dsn, setup) -> None:
    _due(dsn, setup)
    outcome, channel = _dispatch(dsn, setup, [
        providers.scripted_turn(text="Just checking in about the lace — still interested?")])
    assert outcome == "sent"
    assert channel.sent == ["Just checking in about the lace — still interested?"]
    with db.connect(dsn) as conn:
        row = conn.execute("select status from follow_ups").fetchone()
        assert row["status"] == "sent"
        actions = [r["action"] for r in conn.execute("select action from audit_log")]
    assert "follow_up_sent" in actions


def test_the_dispatcher_notices_a_reply_that_bypassed_intake(dsn, setup) -> None:
    """Nothing damages this product more than a bot chasing someone who has
    already answered.

    The message is inserted directly rather than through `record_inbound`,
    which cancels follow-ups itself -- so this exercises the dispatcher's own
    backstop rather than intake's, and covers a message that arrived by any
    other route.
    """
    _due(dsn, setup)
    with db.connect(dsn) as conn:
        conn.execute(
            """
            insert into messages (business_id, conversation_id, role, body,
                                  provider_message_id)
            values (%s, %s, 'customer', 'Actually I''m ready', 'bypass-1')
            """,
            (setup["business"].id, setup["conversation"]))
    outcome, channel = _dispatch(dsn, setup, [
        providers.scripted_turn(text="Just checking in!")])
    assert outcome == "customer_replied"
    assert channel.sent == []


def test_nothing_is_sent_while_a_person_owns_the_thread(dsn, setup) -> None:
    _due(dsn, setup)
    with db.connect(dsn) as conn:
        conn.execute("update conversations set status = 'human' where id = %s",
                     (setup["conversation"],))
    outcome, channel = _dispatch(dsn, setup, [
        providers.scripted_turn(text="Just checking in!")])
    assert outcome == "human_owns_it"
    assert channel.sent == []


def test_a_follow_up_waits_for_a_turn_already_in_flight(dsn, setup) -> None:
    """Two messages answering different things moments apart is worse than a
    late nudge."""
    _due(dsn, setup)
    setup["queue"].submit(agent.TURN, business_id=setup["business"].id,
                          conversation_id=setup["conversation"])
    outcome, channel = _dispatch(dsn, setup, [
        providers.scripted_turn(text="Just checking in!")])
    assert outcome == "turn_in_flight"
    assert channel.sent == []


def test_the_guard_applies_to_follow_ups_too(dsn, setup) -> None:
    """A follow-up is not exempt from the rules because a person asked for it
    on a timer."""
    _due(dsn, setup)
    outcome, channel = _dispatch(dsn, setup, [
        providers.scripted_turn(text="Special offer — just ₦9,999 today only!")])
    assert outcome == "guard_blocked"
    assert channel.sent == [], "an invented price went out on a timer"
    with db.connect(dsn) as conn:
        detail = conn.execute(
            "select detail from audit_log where action = 'guard_blocked'").fetchone()["detail"]
    assert detail["during"] == "follow_up"


def test_a_spent_allowance_defers_rather_than_dropping(dsn, setup) -> None:
    _due(dsn, setup)

    class Dead:
        name = "dead"

        def chat(self, messages, tools):
            raise providers.ProviderError("out of quota", reason="quota")

    with db.connect(dsn) as conn:
        job = db.Queue(dsn).claim("w")
        outcome = agent.dispatch_follow_up(conn, db.Queue(dsn), job, chat=Dead(),
                                           channel=Chan())
    assert outcome.startswith("deferred_no_model")
    with db.connect(dsn) as conn:
        row = conn.execute("select status, due_at from follow_ups").fetchone()
    assert row["status"] == "scheduled", "a follow-up was dropped because a free tier ran out"
    assert row["due_at"] > datetime.now(timezone.utc)


def test_quiet_hours_defer_the_send(dsn, setup) -> None:
    """The behaviour the fixtures above deliberately switch off.

    Forcing the window to cover the present moment rather than waiting for
    21:00 to come round -- the point is to test the deferral, not to have it
    happen by luck once a day.
    """
    _due(dsn, setup)
    now = datetime.now(LAGOS)
    one_hour_ago = (now - timedelta(hours=1)).strftime("%H:%M")
    one_hour_ahead = (now + timedelta(hours=1)).strftime("%H:%M")
    with db.connect(dsn) as conn:
        conn.execute(
            "update businesses set settings = settings || %s::jsonb where id = %s",
            (psycopg.types.json.Json({"followup": {
                "quiet_start": one_hour_ago, "quiet_end": one_hour_ahead,
                "first_after_minutes": 120, "max_attempts": 2, "min_gap_minutes": 0}}),
             setup["business"].id),
        )

    outcome, channel = _dispatch(dsn, setup, [
        providers.scripted_turn(text="Just checking in!")])

    assert outcome == "quiet_hours"
    assert channel.sent == [], "a follow-up went out during quiet hours"
    with db.connect(dsn) as conn:
        row = conn.execute("select status, due_at from follow_ups").fetchone()
    assert row["status"] == "scheduled", "it was dropped rather than deferred"
    assert row["due_at"] > datetime.now(timezone.utc), "it was not moved forward"
