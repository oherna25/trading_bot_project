"""Download 1 month of daily bars via Yahoo-first into the local bundle.

Universe: major indices + top 1000 NYSE by market cap (Nasdaq screener,
sorted locally). Rerunnable and idempotent (save_bar dedupes).

Usage:
    PYTHONPATH=. .venv/bin/python scripts/download_month_bars.py [--store-dir data/trading-bot]
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from bot.feeds import YAHOO_BASE_URL
from bot.persist import load_bundle, save_bundle
from bot.shapes import Bar

MAJOR_INDICES = ["SPY", "QQQ", "DIA", "IWM"]

NASDAQ_SCREENER_URL = (
    "https://api.nasdaq.com/api/screener/stocks"
    "?tableonly=true&exchange=NYSE&download=true"
)

# Verified NYSE large caps absent from the Nasdaq screener snapshot
# (2026-11-01: WMT/HON/LIN returned 0 occurrences). Unioned in so a
# source gap never drops known names.
SUPPLEMENT = [
    "BRK-B", "JPM", "V", "WMT", "XOM", "UNH", "MA", "PG", "JNJ", "ORCL",
    "BAC", "HD", "CVX", "KO", "MRK", "LLY", "ABBV", "CRM", "DIS", "MCD",
    "ACN", "LIN", "ABT", "DHR", "TMO", "CAT", "GS", "MS", "NEE", "PM",
    "HON", "RTX", "LOW", "UNP", "SPGI", "BLK", "SCHW", "AMT", "AXP", "C",
    "VZ", "T", "PFE", "MDT", "DUK", "SO", "F", "GM", "NKE", "DE",
]

SYMBOL_OK = re.compile(r"^[A-Z0-9.\-]+$")


def fetch_nyse_top1000(limit: int = 1000) -> list[str]:
    req = urllib.request.Request(
        NASDAQ_SCREENER_URL,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    rows = data["data"]["rows"]

    def cap(row: dict) -> float:
        try:
            return float(str(row.get("marketCap") or "0").replace(",", "") or 0)
        except ValueError:
            return 0.0

    rows_sorted = sorted(rows, key=cap, reverse=True)
    out: list[str] = []
    seen: set[str] = set()
    for row in rows_sorted:
        raw = str(row.get("symbol") or "").strip().upper()
        if not raw or "^" in raw:
            continue
        yahoo = raw.replace("/", "-").replace(".", "-")
        if not SYMBOL_OK.match(yahoo):
            continue
        if cap(row) <= 0:
            continue
        if yahoo in seen:
            continue
        seen.add(yahoo)
        out.append(yahoo)
        if len(out) >= limit:
            break
    return out


def fetch_yahoo_month(yahoo_symbol: str, timeout: float = 15.0) -> list[dict]:
    query = urllib.parse.urlencode({"interval": "1d", "range": "1mo"})
    url = f"{YAHOO_BASE_URL}/v8/finance/chart/{yahoo_symbol}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "trading-bot/0.1"})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:  # noqa: BLE001 - boundary retry, journaled by caller
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (2**attempt))
    else:
        raise RuntimeError(f"yahoo fetch failed for {yahoo_symbol}: {last_error}")
    results = (payload.get("chart") or {}).get("result") or []
    node = results[0] if results else {}
    stamps = node.get("timestamp") or []
    quotes = (node.get("indicators") or {}).get("quote") or []
    quote = quotes[0] if quotes else {}
    bars: list[dict] = []
    for stamp, o, h, low, c, v in zip(
        stamps,
        quote.get("open") or [],
        quote.get("high") or [],
        quote.get("low") or [],
        quote.get("close") or [],
        quote.get("volume") or [],
    ):
        if stamp is None or c is None:
            continue
        bars.append(
            {
                "timestamp": datetime.fromtimestamp(int(stamp), tz=timezone.utc),
                "open": float(o or c),
                "high": float(h or c),
                "low": float(low or c),
                "close": float(c),
                "volume": float(v or 0.0),
            }
        )
    bars.sort(key=lambda b: b["timestamp"])
    if not bars:
        raise RuntimeError(f"yahoo returned no bars for {yahoo_symbol}")
    return bars


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-dir", default="data/trading-bot")
    parser.add_argument("--top-n", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    store, journal = load_bundle(args.store_dir)
    top = fetch_nyse_top1000(args.top_n)
    print(f"nyse top{len(top)} cutoff ok, e.g. {top[:3]} ... {top[-3:]}")
    symbols = [*MAJOR_INDICES, *top]
    for extra in SUPPLEMENT:
        if extra not in symbols:
            symbols.append(extra)
            print(f"supplement: {extra} (absent from screener snapshot)")
    print(f"universe: {len(symbols)} symbols (indices + top{len(top)} NYSE + supplement), yahoo 1mo")

    now = datetime.now(timezone.utc)
    fetched: dict[str, list[dict] | None] = {}

    def _one(symbol: str) -> tuple[str, list[dict] | None]:
        try:
            return symbol, fetch_yahoo_month(symbol)
        except Exception as exc:  # noqa: BLE001 - per-symbol failure, journaled below
            print(f"{symbol}: FAILED {type(exc).__name__}")
            return symbol, None

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for symbol, bars in pool.map(_one, symbols):
            fetched[symbol] = bars

    ok = 0
    failed: list[str] = []
    for symbol in symbols:
        bars = fetched.get(symbol)
        if not bars:
            failed.append(symbol)
            journal.append(now, "feed_failed", {"feed_name": "yfinance", "error": "no-bars"})
            continue
        new = 0
        for b in bars:
            bar = Bar(
                interval="1d",
                timestamp_utc=b["timestamp"],
                open=b["open"],
                high=b["high"],
                low=b["low"],
                close=b["close"],
                volume=b["volume"],
            )
            if store.save_bar(symbol, bar):
                new += 1
        journal.append(
            now,
            "bars_ingested",
            {"source_name": "yfinance", "underlying": symbol, "count": len(bars)},
        )
        ok += 1
        if ok % 100 == 0:
            save_bundle(args.store_dir, store, journal)
            print(f"progress: {ok}/{len(symbols)} saved")
    save_bundle(args.store_dir, store, journal)
    print(f"ok: {ok}/{len(symbols)} failed ({len(failed)}): {failed[:20]}")
    sample = [s for s in symbols if store.read_bars(s)][:3]
    for s in sample:
        bars = store.read_bars(s)
        print(f"{s}: n={len(bars)} {bars[0].timestamp_utc.date()}->{bars[-1].timestamp_utc.date()}")


if __name__ == "__main__":
    main()
