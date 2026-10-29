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
import os
from datetime import datetime, timezone
from typing import Optional

from bot.config import operator_config
from bot.feeds import FeedUnavailableError
from bot.muse import default_dashboard_base_url, resolve_port, serve
from bot.persist import load_bundle, save_bundle
from bot.research import live_brief
from bot.schedule import normalize_underlyings

STORE_DIR = "data/trading-bot"
HOST = "0.0.0.0"
PORT = 8000
SECRETS_DIR = "/secrets"


def materialize_file_secrets(secrets_dir: str = SECRETS_DIR) -> None:
    if not os.path.isdir(secrets_dir):
        return
    for name in os.listdir(secrets_dir):
        path = os.path.join(secrets_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as handle:
            os.environ.setdefault(name, handle.read().strip())


def run(
    underlying: str = "SPY",
    store_dir: str = STORE_DIR,
    host: str = HOST,
    port: Optional[int] = None,
) -> str:
    materialize_file_secrets()
    port = resolve_port(port)
    store, journal = load_bundle(store_dir)
    config = operator_config(default_dashboard_base_url(), store.read_profile())
    briefs = []
    failures = []
    for symbol in normalize_underlyings(underlying):
        try:
            briefs.append(live_brief(symbol, store, journal, datetime.now(timezone.utc)))
        except FeedUnavailableError as exc:
            failures.append(f"live brief failed for {symbol}: {exc}")
    for line in briefs + failures:
        print(line)
    save_bundle(store_dir, store, journal)
    print(
        f"limits max_day_loss={config.max_day_loss} "
        f"day_profit_target={config.day_profit_target} max_trade_size={config.max_trade_size}"
    )
    print(f"serving on {host}:{port}")
    serve(store, journal, config, host, port, store_dir)
    if failures and not briefs:
        return "\n".join(failures)
    return "\n---\n".join(briefs + failures)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the live brief once, then serve the bot over HTTP.")
    parser.add_argument(
        "--underlying", default="SPY", help="Comma-separated symbols to brief (default: SPY)."
    )
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
        default=None,
        help=f"HTTP bind port (default: $PORT, else {PORT}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> str:
    args = parse_args(argv)
    return run(args.underlying, args.store_dir, args.host, args.port)


if __name__ == "__main__":
    main()
