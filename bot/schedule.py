from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Sequence

from bot.feeds import FeedContext, FeedUnavailableError, live_context
from bot.journal import Journal
from bot.persist import load_bundle, save_bundle
from bot.research import live_brief
from bot.store import SnapshotStore


def normalize_underlyings(underlying: str | Sequence[str]) -> list[str]:
    if isinstance(underlying, str):
        parts = [part.strip().upper() for part in underlying.split(",")]
    else:
        parts = [str(part).strip().upper() for part in underlying]
    seen = [part for part in parts if part]
    ordered = []
    for part in seen:
        if part not in ordered:
            ordered.append(part)
    return ordered or ["SPY"]


def run_scheduled_session(
    underlying: str | Sequence[str],
    store_dir: str,
    now: datetime | None = None,
    chain_feeds: Sequence[str] | None = None,
    bar_feeds: Sequence[str] | None = None,
    context_factory: Callable[[str], FeedContext] = live_context,
    live_models: bool = False,
    grounding: bool = False,
    cards: bool = False,
) -> str:
    moment = now or datetime.now(timezone.utc)
    symbols = normalize_underlyings(underlying)
    store, journal = load_bundle(store_dir)
    replies = []
    failures = []
    for symbol in symbols:
        try:
            replies.append(
                live_brief(
                    symbol,
                    store,
                    journal,
                    moment,
                    chain_feeds,
                    bar_feeds,
                    context_factory,
                    live_models,
                    grounding,
                    cards,
                )
            )
        except FeedUnavailableError as exc:
            journal.append(moment, "session_failed", {"underlying": symbol, "reason": str(exc)})
            failures.append(f"scheduled session failed for {symbol}: {exc}")
    if replies:
        journal.append(moment, "session_saved", {"underlyings": symbols, "store_dir": store_dir})
    save_bundle(store_dir, store, journal)
    if failures and not replies:
        return "\n".join(failures)
    return "\n---\n".join(replies + failures)


def session_store(underlying: str, store_dir: str) -> tuple[SnapshotStore, Journal]:
    del underlying
    return load_bundle(store_dir)
