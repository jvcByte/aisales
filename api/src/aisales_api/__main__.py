"""Entry point: python -m aisales_api [--port N] [--reload]"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aisales_api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--dsn", default=None,
                        help="defaults to $AISALES_DSN or postgresql:///aisales")
    args = parser.parse_args(argv)

    import uvicorn

    from .app import create_app, default_dsn

    target = args.dsn or default_dsn()
    # Two lines, and no business name in either. The simulator used to be
    # advertised as `/sim/adafabrics` -- a hardcoded slug, which was wrong for
    # every business but the seeded one, and became a dead URL the moment the
    # simulator moved under the session: `GET /sim/{slug}` no longer exists,
    # and there is no slug to put in it. A banner that names a route the
    # server does not serve is worse than no banner.
    print(f"api       http://{args.host}:{args.port}")
    print(f"simulator http://{args.host}:{args.port}/sim  (sign in first; it "
          f"drives the real turn function against real conversations)")
    uvicorn.run(create_app(target), host=args.host, port=args.port,
                reload=args.reload, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
