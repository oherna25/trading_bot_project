from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from bot.journal import Journal
from bot.shapes import ChainSnapshot, OptionContract
from bot.store import SnapshotStore


def _tradier_sandbox() -> dict[str, Any]:
    return {
        "underlying": "SPY",
        "contracts": [
            {"strike": 100.0, "expiry": "2026-11-20", "call_or_put": "call", "open_interest": 1000.0},
        ],
    }


def _alpaca() -> dict[str, Any]:
    return {"underlying": "SPY", "contracts": []}


def _finnhub() -> dict[str, Any]:
    return {"underlying": "SPY", "contracts": []}


def _marketdata_eod() -> dict[str, Any]:
    return {"underlying": "SPY", "contracts": []}


def _yfinance() -> dict[str, Any]:
    return {"underlying": "SPY", "contracts": []}


FEED_REGISTRY: dict[str, Callable[[], dict[str, Any]]] = {
    "tradier_sandbox": _tradier_sandbox,
    "alpaca": _alpaca,
    "finnhub": _finnhub,
    "marketdata_eod": _marketdata_eod,
    "yfinance": _yfinance,
}


def ingest_chain(feed_name: str, store: SnapshotStore, journal: Journal) -> ChainSnapshot:
    adapter = FEED_REGISTRY[feed_name]
    started = time.perf_counter()
    raw = adapter()
    latency_ms = (time.perf_counter() - started) * 1000.0
    snapshot = ChainSnapshot(
        underlying=raw["underlying"],
        timestamp_utc=_now_utc(),
        source_name=feed_name,
        latency_ms=latency_ms,
        contracts_list=tuple(
            OptionContract(
                strike=c["strike"],
                expiry=c["expiry"],
                call_or_put=c["call_or_put"],
                open_interest=c["open_interest"],
                gamma=c.get("gamma"),
                vega=c.get("vega"),
            )
            for c in raw["contracts"]
        ),
    )
    snapshot_id = store.save(snapshot)
    journal.append(snapshot.timestamp_utc, "snapshot_ingested", {"snapshot_id": snapshot_id, "source_name": feed_name})
    return snapshot


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
