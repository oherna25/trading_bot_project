"""Local entry point: live brief plus persistent store plus HTTP server.

Runs the live brief once against the local JSON bundle, prints it, then
serves the same store and journal over HTTP (this call blocks)::

    .venv/bin/python run_app.py
    .venv/bin/python run_app.py --underlying SPY --store-dir data/trading-bot --host 0.0.0.0 --port 8000

Credentials come from the same ``.env`` plus environment lookup the feeds
already use, so no secret wiring is needed here. When every chain feed
fails, the failure is printed, the bundle is still saved, and the server
still starts so the bot endpoint stays up.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from bot.feeds import FeedUnavailableError
from bot.muse import serve
from bot.persist import load_bundle, save_bundle
from bot.research import live_brief
from bot.shapes import OperatorConfig

STORE_DIR = "data/trading-bot"
HOST = "0.0.0.0"
PORT = 8000


def run(underlying: str = "SPY", store_dir: str = STORE_DIR, host: str = HOST, port: int = PORT) -> str:
    store, journal = load_bundle(store_dir)
    config = OperatorConfig(
        dashboard_base_url="https://ops.example.internal",
        dashboard_link_expiry_seconds=3600,
        max_day_loss=1000.0,
        day_profit_target=500.0,
        max_trade_size=5000.0,
    )
    try:
        brief = live_brief(underlying, store, journal, datetime.now(timezone.utc))
    except FeedUnavailableError as exc:
        failure = f"live brief failed: {exc}"
        print(failure)
        save_bundle(store_dir, store, journal)
        print(f"serving on {host}:{port}")
        serve(store, journal, config, host, port)
        return failure
    print(brief)
    save_bundle(store_dir, store, journal)
    print(f"serving on {host}:{port}")
    serve(store, journal, config, host, port)
    return brief


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the live brief once, then serve the bot over HTTP.")
    parser.add_argument("--underlying", default="SPY", help="Symbol to brief (default: SPY).")
    parser.add_argument(
        "--store-dir",
        default=STORE_DIR,
        help=f"JSON bundle directory (default: {STORE_DIR}).",
    )
    parser.add_argument(
        "--host",
        default=HOST,
        help=f"HTTP bind host (default: {HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=PORT,
        help=f"HTTP bind port (default: {PORT}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> str:
    args = parse_args(argv)
    return run(args.underlying, args.store_dir, args.host, args.port)


if __name__ == "__main__":
    main()
