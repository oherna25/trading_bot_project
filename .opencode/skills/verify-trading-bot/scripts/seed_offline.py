#!/usr/bin/env python3
"""Seed an isolated scratch bundle with canned (offline) fixtures.

Usage:
    .venv/bin/python scripts/seed_offline.py <scratch-store-dir> [UNDERLYING]

Uses bot.feeds.default_context (http_get=_canned_get: no network, no keys)
plus ingest_chain("tradier_sandbox") and ingest_bars("alpaca"), then
save_bundle into the scratch dir. Never touches data/trading-bot.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.abspath(__file__)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE)))))
sys.path.insert(0, ROOT)
from bot.feeds import default_context, ingest_bars, ingest_chain
from bot.journal import Journal
from bot.persist import save_bundle
from bot.store import SnapshotStore


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        print("usage: seed_offline.py <scratch-store-dir> [UNDERLYING]", file=sys.stderr)
        raise SystemExit(2)
    store_dir = argv[1]
    symbol = (argv[2] if len(argv) > 2 else "SPY").strip().upper() or "SPY"
    store = SnapshotStore()
    journal = Journal()
    ctx = default_context(symbol)
    snapshot = ingest_chain("tradier_sandbox", store, journal, ctx)
    bars = ingest_bars("alpaca", store, journal, ctx)
    save_bundle(store_dir, store, journal)
    print(f"seeded {symbol}: {len(snapshot.contracts_list)} contracts, {len(bars)} bars -> {store_dir}")


if __name__ == "__main__":
    main(sys.argv)
