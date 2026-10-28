from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from bot.feeds import FeedContext, FeedUnavailableError, live_context
from bot.journal import Journal
from bot.persist import load_bundle, save_bundle
from bot.research import live_brief
from bot.store import SnapshotStore


def run_scheduled_session(
    underlying: str,
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
    store, journal = load_bundle(store_dir)
    try:
        reply = live_brief(
            underlying,
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
    except FeedUnavailableError as exc:
        journal.append(moment, "session_failed", {"underlying": underlying, "reason": str(exc)})
        save_bundle(store_dir, store, journal)
        return f"scheduled session failed: {exc}"
    journal.append(moment, "session_saved", {"underlying": underlying, "store_dir": store_dir})
    save_bundle(store_dir, store, journal)
    return reply


def session_store(underlying: str, store_dir: str) -> tuple[SnapshotStore, Journal]:
    del underlying
    return load_bundle(store_dir)
