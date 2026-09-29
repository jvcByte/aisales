"""The worker loop: claim a job, do it, reach a terminal state.

Kept deliberately boring. The queue already guarantees one worker per job and
that a dead worker's job is picked up again, so this only has to do three
things in the right order: claim, work, finish -- and on failure, fail with a
delay rather than a tight retry loop.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass

import psycopg

from aisales import agent, channels, db, followup, paystack as paystack_mod
from aisales.channels import Channel
from aisales.providers import Chat, ProviderError

#: Long enough that a spent per-day allowance is not re-tried in a tight loop,
#: short enough that a genuinely transient outage recovers the same day.
QUOTA_BACKOFF_S = 900
BUSY_BACKOFF_S = 30


@dataclass
class Worker:
    dsn: str
    channel: Channel
    chat: Chat
    paystack: object | None = None
    owner: str = ""

    def __post_init__(self) -> None:
        self.queue = db.Queue(self.dsn)
        self.owner = self.owner or db.new_owner()

    def run_once(self) -> bool:
        """Claim and handle one job. False when there was nothing to do."""
        job = self.queue.claim(self.owner)
        if job is None:
            return False

        try:
            # The whole turn runs inside the job's tenant. Every query the
            # agent, the tools and the guard issue inherits this context, so
            # RLS scopes them without any of that code knowing about tenancy.
            with db.tenant(self.dsn, job.business_id) as conn:
                outcome = self._handle(conn, job)
            self.queue.finish(job, self.owner)
            # Strictly after finish: while this job is queued or leased the
            # one-turn index correctly refuses a replacement for the same
            # conversation, so asking any earlier always gets "no".
            self.finish_and_requeue(job)
            print(f"  {job.kind} {job.id[:8]} -> {outcome}")
        except ProviderError as exc:
            delay = QUOTA_BACKOFF_S if exc.reason == "quota" else BUSY_BACKOFF_S
            self.queue.fail(job, self.owner, code=exc.reason,
                            message=str(exc)[:300], delay_seconds=delay)
            print(f"  {job.kind} {job.id[:8]} -> provider {exc.reason}, retry in {delay}s")
        except Exception as exc:  # noqa: BLE001 - a worker must not die on one job
            self._note_channel_failure(job, exc)
            self.queue.fail(job, self.owner, code="error",
                            message=f"{type(exc).__name__}: {exc}"[:300],
                            delay_seconds=BUSY_BACKOFF_S)
            print(f"  {job.kind} {job.id[:8]} -> error: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        return True

    def _note_channel_failure(self, job: db.Job, exc: Exception) -> None:
        """Mark the business's integration as failing when a send fails.

        Requirement 6.6. A revoked WhatsApp token otherwise produces a silent
        retry loop: every send fails, the job backs off and retries, and
        nothing anywhere says the connection is dead.

        Its own failure is swallowed on purpose. This is bookkeeping about an
        error; it must never replace the error.
        """
        if not isinstance(exc, channels.ChannelError):
            return
        try:
            with db.tenant(self.dsn, job.business_id) as conn:
                channels.record_send_failure(conn, business_id=job.business_id,
                                             provider="whatsapp", reason=str(exc))
        except Exception:  # noqa: BLE001 - see the docstring
            pass

    def _handle(self, conn: psycopg.Connection, job: db.Job) -> str:
        # The business's own channel, not the worker's. One process serves
        # every business, and a reply must leave through the number the
        # customer messaged -- otherwise it arrives from a stranger.
        channel = channels.for_business(conn, business_id=job.business_id,
                                        fallback=self.channel)
        # The business's own payment account, resolved the same way. A business
        # with none gets `None` rather than the platform's client, so its
        # customers cannot pay into somebody else's account.
        paying = paystack_mod.for_business(conn, business_id=job.business_id,
                                           fallback=self.paystack)
        if job.kind == agent.TURN:
            return self._turn(conn, job, channel, paying)
        if job.kind == agent.FOLLOW_UP:
            return agent.dispatch_follow_up(conn, self.queue, job, chat=self.chat,
                                            channel=channel, paystack=paying)
        return f"unknown job kind {job.kind!r}"

    def _turn(self, conn: psycopg.Connection, job: db.Job,
              channel: channels.Channel, paying) -> str:
        business = agent.business_by_id(conn, job.business_id)
        if business is None:
            return "no_business"
        # A job queued before the flag flipped is already claimed and would
        # otherwise go all the way to a model call. Stopping here costs one
        # read and keeps the queue from being a way around suspension.
        if business.suspended:
            return "suspended"
        conversation = agent._load_conversation(  # noqa: SLF001
            conn, job.conversation_id, job.business_id)
        if conversation is None:
            return "no_conversation"

        ctx = db.TurnContext(business=business, customer_id=str(conversation["customer_id"]),
                             conversation_id=job.conversation_id,
                             channel=conversation["channel"])
        result = agent.run_turn(conn, channel, self.chat, ctx,
                                paystack=paying, queue=self.queue)

        # The race coalescing opens: a message that arrived while this turn was
        # reading the thread was correctly refused a second turn, and this turn
        # has already replied. Without the re-check it is never answered.
        # Queued *after* the finish below, because the one-turn index would
        # refuse it while this job still holds the conversation.
        if result.silent and result.reason.startswith("a person"):
            return "suppressed"
        return result.reason or ("silent" if result.silent else "replied")

    def finish_and_requeue(self, job: db.Job) -> None:
        """Queue another turn if the customer spoke while we were replying.

        Called after `finish`, because the job must stop holding the
        conversation before a replacement can be queued for it.
        """
        if job.kind != agent.TURN or not job.conversation_id:
            return
        with db.tenant(self.dsn, job.business_id) as conn:
            queued = agent.requeue_if_unanswered(conn, self.queue, job.business_id,
                                                 job.conversation_id)
        if queued:
            print(f"  -> the customer spoke mid-turn; queued another ({queued[:8]})")

    def loop(self, *, poll_seconds: float = 2.0, max_jobs: int | None = None) -> int:
        """Run until interrupted, reaping dead leases as it goes."""
        import time

        done = 0
        print(f"worker {self.owner} watching {self.dsn}")
        try:
            while True:
                if self.run_once():
                    done += 1
                    if max_jobs is not None and done >= max_jobs:
                        return done
                    continue
                self.queue.reap()
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            print("\nstopping")
        return done
