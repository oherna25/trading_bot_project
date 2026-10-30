"""Local entry point: live brief plus persistent store plus HTTP server.

Runs the live brief once against the local JSON bundle, prints it, then
serves the same store and journal over HTTP (this call blocks), re-ingesting
on a timer (``--refresh-seconds``, default 900, 0 disables) so snapshots stay
fresh without restarts::

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
from bot.muse import resolve_port, serve
from bot.persist import load_bundle, save_bundle
from bot.research import live_brief
from bot.schedule import normalize_underlyings, tick_symbols

STORE_DIR = "data/trading-bot"
HOST = "0.0.0.0"
PORT = 8000
SECRETS_DIR = "/secrets"
REFRESH_SECONDS = 900.0


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
    refresh_seconds: float = REFRESH_SECONDS,
    session_schedule: str = "",
    session_underlying: Optional[str] = None,
) -> str:
    materialize_file_secrets()
    port = resolve_port(port)
    store, journal = load_bundle(store_dir)
    config = operator_config(store.read_profile())
    symbols = normalize_underlyings(underlying)
    briefs = []
    failures = []
    for symbol in tick_symbols(symbols, store):
        try:
            briefs.append(live_brief(symbol, store, journal, datetime.now(timezone.utc), paper_trade=False))
        except FeedUnavailableError as exc:
            failures.append(f"live brief failed for {symbol}: {exc}")
    for line in briefs + failures:
        print(line)
    save_bundle(store_dir, store, journal)
    print(
        f"limits max_day_loss={config.max_day_loss} "
        f"day_profit_target={config.day_profit_target} max_trade_size={config.max_trade_size}"
    )
    if refresh_seconds > 0:
        print(f"refreshing {','.join(symbols)} every {refresh_seconds:g}s")
    session_symbols = normalize_underlyings(session_underlying or underlying)
    if session_schedule:
        print(f"scheduled sessions ({session_schedule}) for {','.join(session_symbols)}")
    print(f"serving on {host}:{port}")
    serve(store, journal, config, host, port, store_dir,
          refresh_seconds=refresh_seconds, refresh_symbols=symbols,
          session_schedule=session_schedule, session_underlyings=session_symbols)
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
    parser.add_argument(
        "--refresh-seconds",
        type=float,
        default=REFRESH_SECONDS,
        help=f"Re-ingest interval while serving; 0 disables (default: {REFRESH_SECONDS:g}).",
    )
    parser.add_argument(
        "--session-schedule",
        default="",
        help="Cron subset 'M H * * DOW' for in-app sessions; empty disables (default: disabled).",
    )
    parser.add_argument(
        "--session-underlying",
        default=None,
        help="Comma-separated symbols for scheduled sessions (default: value of --underlying).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> str:
    args = parse_args(argv)
    return run(args.underlying, args.store_dir, args.host, args.port, args.refresh_seconds,
               args.session_schedule, args.session_underlying)


if __name__ == "__main__":
    main()
