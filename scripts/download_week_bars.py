"""Download 1 week of daily bars via Yahoo-first ingest into the local bundle.

Lever for: major indices + top 50 NYSE. Rerunnable and idempotent
(save_bar dedupes; save_bundle rewrites the same JSON shape).

Usage:
    .venv/bin/python scripts/download_week_bars.py [--store-dir data/trading-bot]
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from bot.feeds import BAR_FEEDS, ingest_bars, live_context
from bot.persist import load_bundle, save_bundle
from bot.research_ingest import ingest_first_working

MAJOR_INDICES = ["SPY", "QQQ", "DIA", "IWM"]

NYSE_TOP_50 = [
    "BRK-B", "JPM", "V", "WMT", "XOM", "UNH", "MA", "PG", "JNJ", "ORCL",
    "BAC", "HD", "CVX", "KO", "MRK", "LLY", "ABBV", "CRM", "DIS", "MCD",
    "ACN", "LIN", "ABT", "DHR", "TMO", "CAT", "GS", "MS", "NEE", "PM",
    "HON", "RTX", "LOW", "UNP", "SPGI", "BLK", "SCHW", "AMT", "AXP", "C",
    "VZ", "T", "PFE", "MDT", "DUK", "SO", "F", "GM", "NKE", "DE",
]

YAHOO_FIRST_BAR_FEEDS = tuple(dict.fromkeys(["yfinance", *BAR_FEEDS]))


def download(symbols: list[str], store, journal, now: datetime) -> dict[str, str | None]:
    used: dict[str, str | None] = {}
    for symbol in symbols:
        feed = ingest_first_working(
            YAHOO_FIRST_BAR_FEEDS, ingest_bars, symbol, store, journal, now, live_context
        )
        used[symbol] = feed
        status = feed if feed is not None else "FAILED"
        print(f"{symbol}: {status}")
        time.sleep(0.3)
    return used


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-dir", default="data/trading-bot")
    args = parser.parse_args()

    store, journal = load_bundle(args.store_dir)
    now = datetime.now(timezone.utc)
    symbols = [*MAJOR_INDICES, *NYSE_TOP_50]

    print(f"universe: {len(symbols)} symbols (4 indices + 50 NYSE), yahoo-first")
    used = download(symbols, store, journal, now)
    save_bundle(args.store_dir, store, journal)

    ok = {s: f for s, f in used.items() if f is not None}
    failed = [s for s, f in used.items() if f is None]
    print(f"ok: {len(ok)}/{len(symbols)} failed: {failed}")
    for symbol in symbols:
        bars = store.read_bars(symbol)
        if bars:
            first, last = bars[0], bars[-1]
            print(
                f"{symbol}: n={len(bars)} "
                f"{first.timestamp_utc.date()}->{last.timestamp_utc.date()} "
                f"last_close={last.close}"
            )


if __name__ == "__main__":
    main()
