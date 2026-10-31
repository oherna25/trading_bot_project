"""Feed ingest helpers and option IV recording."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional, Sequence

from bot.feeds import (
    FeedContext,
    fetch_alpaca_iv,
    fetch_yahoo_iv,
)
from bot.journal import Journal
from bot.store import SnapshotStore

def ingest_first_working(
    feed_names: Sequence[str],
    ingest: Callable[..., object],
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    context_factory: Callable[[str], FeedContext],
) -> Optional[str]:
    for feed_name in feed_names:
        try:
            ingest(feed_name, store, journal, context_factory(underlying))
        except Exception as exc:
            journal.append(now, "feed_failed", {"feed_name": feed_name, "error": type(exc).__name__})
            continue
        return feed_name
    return None


def _record_option_iv(
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    context_factory: Callable[[str], FeedContext],
) -> None:
    found = store.read_latest(underlying)
    if found is None:
        return
    _, snapshot = found
    if any(contract.iv is not None for contract in snapshot.contracts_list):
        return
    for source, fetch in (("alpaca_options", fetch_alpaca_iv), ("yahoo_options", fetch_yahoo_iv)):
        try:
            result = fetch(context_factory(underlying))
        except Exception:
            continue
        if result["iv_average"] is None:
            continue
        store.save_iv(underlying, snapshot.timestamp_utc.isoformat(), result["iv_average"])
        journal.append(
            now,
            "iv_recorded",
            {
                "symbol": underlying,
                "date": snapshot.timestamp_utc.date().isoformat(),
                "iv": result["iv_average"],
                "points": result["count"],
                "source": source,
            },
        )
        return
