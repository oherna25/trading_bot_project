"""Local entry point: scheduled brief plus persistent store on local disk.

Run by hand, from cron, or from launchd (this machine is the scheduler)::

    .venv/bin/python run_session.py
    .venv/bin/python run_session.py --underlying SPY --store-dir data/trading-bot

Cron (machine-local time, weekdays 07:30, which is 08:30 ET)::

    30 7 * * 1-5 cd /path/to/trading_bot_project && .venv/bin/python run_session.py >> data/session.log 2>&1

See deploy/ for ready-to-install scheduler files. Credentials come from the
same ``.env`` plus environment lookup the feeds already use, so no secret
wiring is needed here.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

STORE_DIR = "data/trading-bot"
SCHEDULE = "30 7 * * 1-5"


def run(underlying: str = "SPY", store_dir: str = STORE_DIR) -> str:
    from bot.schedule import run_scheduled_session

    return run_scheduled_session(underlying, store_dir, datetime.now(timezone.utc))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one scheduled trading session.")
    parser.add_argument(
        "--underlying", default="SPY", help="Comma-separated symbols to brief (default: SPY)."
    )
    parser.add_argument(
        "--store-dir",
        default=STORE_DIR,
        help=f"JSON bundle directory (default: {STORE_DIR}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> str:
    args = parse_args(argv)
    brief = run(args.underlying, args.store_dir)
    print(brief)
    return brief


if __name__ == "__main__":
    main()
