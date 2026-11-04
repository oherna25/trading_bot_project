"""Zero-key demo: seed canned market state in memory, then serve.

    .venv/bin/python run_demo.py
    .venv/bin/python run_demo.py --port 8000 --store-dir data/demo

No keys, accounts, MongoDB, Telegram, or network are required. With
--store-dir the seeded bundle and any paper activity persist there;
without it the demo is rebuilt fresh on every start.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Optional

from bot.config import operator_config
from bot.demo_seed import seed_demo
from bot.journal import Journal
from bot.muse import resolve_port, serve
from bot.persist import load_bundle, save_bundle
from bot.store import SnapshotStore

HOST = "127.0.0.1"
PORT = 8000


def build_demo(store_dir: str = "") -> tuple[SnapshotStore, Journal, list[str]]:
    if store_dir:
        store, journal = load_bundle(store_dir)
    else:
        store, journal = SnapshotStore(), Journal()
    symbols = seed_demo(store, journal, datetime.now(timezone.utc))
    if store_dir:
        save_bundle(store_dir, store, journal)
    return store, journal, symbols


def run(
    host: str = HOST,
    port: Optional[int] = None,
    store_dir: str = "",
) -> list[str]:
    store, journal, symbols = build_demo(store_dir)
    config = operator_config(store.read_profile())
    bound = resolve_port(port)
    print("MarketWingMan zero-key demo (no keys, no network, seeded data)")
    print(f"seeded: {', '.join(symbols)}; open http://{host}:{bound}/demo")
    print("try chat on /demo: 'gex brief', 'trade cards', 'wheel ideas', 'agent cycle', 'replay'")
    print("add BASETEN_API_KEY to .env later to see live Muse Spark Open picks; nothing else changes")
    serve(store, journal, config, host, bound, store_dir or None, refresh_seconds=0)
    return symbols


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the zero-key seeded demo.")
    parser.add_argument("--host", default=HOST, help=f"HTTP bind host (default: {HOST}).")
    parser.add_argument("--port", type=int, default=None, help=f"HTTP port (default: $PORT, else {PORT}).")
    parser.add_argument(
        "--store-dir",
        default="",
        help="Persist the seeded bundle and paper activity here (default: in-memory only).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> list[str]:
    args = parse_args(argv)
    return run(args.host, args.port, args.store_dir)


def console_main() -> None:
    main()


if __name__ == "__main__":
    main()
