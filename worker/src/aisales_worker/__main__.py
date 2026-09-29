"""Entry point.

    python -m aisales_worker --install --seed     set up a database
    python -m aisales_worker --once               handle at most one job
    python -m aisales_worker --poll 2             run until interrupted
    python -m aisales_worker --create-user EMAIL  give someone a sign-in

`--create-user` is here rather than in the API on purpose. Creating an account
is a provisioning action taken by whoever runs the deployment, not a route: a
self-service signup would need a control plane, email verification and a way to
decide which business a stranger joins, and none of that is in this build.

Adding `--once` is not just for tests: it is what makes the worker scriptable,
so an end-to-end check can advance the queue deterministically instead of
racing a background process.
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg

from aisales import channels, db, dotenv, paystack, providers


def dsn() -> str:
    dotenv.load()
    return os.environ.get("AISALES_DSN", "postgresql:///aisales")


def build_chat(args):
    """The model for this run.

    Refusing beats falling back to a fake: a worker that silently ran on a
    script because a key was missing looks completely healthy from outside.
    But the refusal has to name the thing that is actually wrong. An earlier
    version reported a missing Chat without mentioning `AISALES_FAKES`, which
    sent a reader looking at code for a one-line config problem.
    """
    if args.offline:
        raise SystemExit(
            "--offline cannot drive the worker.\n"
            "  The worker answers real customers and needs a real model. A\n"
            "  scripted one belongs to the caller, so use the simulator\n"
            "  (which runs turns inline) or scripts/e2e.py.\n"
            "  Drop --offline to run against the model keys in .env."
        )
    if os.environ.get("AISALES_FAKES") == "1":
        raise SystemExit(
            "AISALES_FAKES=1 is set, so no model may be called.\n"
            "  That switch is for tests and the simulator, which supply their\n"
            "  own script. The worker has nothing to fall back on.\n"
            "  Fix: set AISALES_FAKES=0 in .env, or remove the line."
        )
    try:
        return providers.default_chat()
    except providers.ProviderError as exc:
        raise SystemExit(
            f"no model is configured: {exc}\n"
            "  Set GEMINI_API_KEY (or GROQ_API_KEY, HF_TOKEN, "
            "OPENROUTER_API_KEY) in .env."
        ) from exc


def build_paystack():
    """The payment client, or None.

    Deliberately not fatal. Payments are needed by exactly one tool, and a
    worker that refuses to start without a Paystack key cannot answer a
    question about a price -- which is most of what it does. Without a client,
    `create_payment_link` tells the model that payments are not configured, so
    the conversation degrades honestly instead of the process dying.
    """
    try:
        return paystack.default_paystack()
    except paystack.PaystackError as exc:
        print(f"  ! payments disabled: {exc}")
    return None


def build_channel(args):
    if args.channel == "whatsapp":
        return channels.MetaCloudChannel(
            token=os.environ.get("WHATSAPP_TOKEN", ""),
            phone_number_id=os.environ.get("WHATSAPP_PHONE_ID", ""),
            app_secret=os.environ.get("WHATSAPP_APP_SECRET", ""),
        )
    return channels.SimulatorChannel()


def create_user(target: str, args) -> int:
    """Create an account and attach it to a business.

    The password is read from `AISALES_PASSWORD` or prompted, never taken as
    an argument: an argument is in the shell history and in `ps` output for
    every other user on the machine.
    """
    import getpass

    from aisales import auth

    password = os.environ.get("AISALES_PASSWORD") or getpass.getpass("password: ")
    if not password:
        print("no password given", file=sys.stderr)
        return 2

    with db.connect(target) as conn:
        if args.operator and not args.business:
            # An operator need not belong to a business -- that is the whole
            # point of the flag -- so the business lookup is skipped rather
            # than defaulted to the only one, which would silently make them a
            # member of it.
            try:
                auth.create_user(conn, args.create_user, password,
                                 name=args.name, is_operator=True)
            except auth.AuthError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"{args.create_user} is an operator and can sign in")
            return 0

        if args.business:
            business = conn.execute("select id from businesses where slug = %s",
                                    (args.business,)).fetchone()
        else:
            rows = conn.execute(
                "select id, slug from businesses order by created_at limit 2").fetchall()
            business = rows[0] if len(rows) == 1 else None
            if business is None and len(rows) > 1:
                print("more than one business exists; pass --business SLUG",
                      file=sys.stderr)
                return 2
        if business is None:
            print(f"no business {args.business!r}", file=sys.stderr)
            return 2

        try:
            user_id = auth.create_user(conn, args.create_user, password,
                                       name=args.name, is_operator=args.operator)
        except auth.AuthError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        auth.add_member(conn, user_id, str(business["id"]), role=args.role)

    print(f"{args.create_user} can now sign in to {business['slug']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aisales_worker")
    parser.add_argument("--install", action="store_true",
                        help="create the schema and indexes, then exit")
    parser.add_argument("--seed", action="store_true",
                        help="create or update the pilot business, then exit")
    parser.add_argument("--once", action="store_true",
                        help="handle at most one job, then exit")
    parser.add_argument("--poll", type=float, default=None,
                        help="seconds between empty claims; run until interrupted")
    parser.add_argument("--channel", default=os.environ.get("AISALES_CHANNEL",
                                                            "simulator"))
    parser.add_argument("--offline", action="store_true",
                        help="refuse to start rather than run without a key")
    parser.add_argument("--create-user", metavar="EMAIL", default=None,
                        help="create an account for this address, then exit")
    parser.add_argument("--business", default=None,
                        help="slug to attach --create-user to (default: the only one)")
    parser.add_argument("--name", default=None, help="display name for --create-user")
    parser.add_argument("--role", default="owner", choices=("owner", "staff"))
    parser.add_argument("--operator", action="store_true",
                        help="this account runs the deployment: it may belong to no "
                             "business, and it can reach the operator console")
    args = parser.parse_args(argv)

    target = dsn()

    if args.install:
        db.install(target)
        try:
            db.install_operator(target)
        except psycopg.errors.InsufficientPrivilege as exc:  # pragma: no cover
            # Not fatal: a deployment that never uses the console does not need
            # the role, and refusing to install the schema over it would make
            # the console's absence break everything else.
            print(f"  ! operator role not created: {exc}", file=sys.stderr)
        print(f"schema installed at {target}")
    if args.seed:
        from .seed import seed

        counts = seed(target)
        print(f"seeded {counts['businesses']} business, {counts['products']} products")
    if args.create_user:
        return create_user(target, args)
    if args.install or args.seed:
        if not (args.once or args.poll is not None):
            return 0

    if not (args.once or args.poll is not None):
        parser.print_help()
        return 2

    from .runner import Worker

    # The interlock. A fake model that happens to produce a plausible sentence,
    # wired to a real WhatsApp channel, answers a real person with words no
    # model produced -- and the run looks completely healthy. Loudly refused at
    # the boundary rather than mentioned in a docstring.
    providers.assert_not_mixed(
        fakes=os.environ.get("AISALES_FAKES") == "1",
        live_channel=args.channel == "whatsapp",
    )

    worker = Worker(dsn=target, channel=build_channel(args), chat=build_chat(args),
                    paystack=build_paystack())

    if args.once:
        handled = worker.run_once()
        return 0 if handled else 1

    worker.loop(poll_seconds=args.poll or 2.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
