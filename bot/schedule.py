from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable, Sequence

from bot.alerts import push_alerts
from bot.feeds import BAR_FEEDS, CHAIN_FEEDS, FeedContext, FeedUnavailableError, ingest_bars, ingest_chain, live_context
from bot.ivrank import backfill_iv_history
from bot.journal import Journal
from bot.persist import load_bundle, save_bundle
from bot.research import ingest_first_working, live_brief
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


def tick_symbols(fixed: Sequence[str], store: SnapshotStore | None) -> list[str]:
    """Requested symbols first, then every other underlying in the store."""
    tick = list(fixed)
    if store is not None:
        for symbol in store.list_underlyings():
            if symbol not in tick:
                tick.append(symbol)
    return tick or ["SPY"]


def run_session_core(
    underlying: str | Sequence[str],
    store: SnapshotStore,
    journal: Journal,
    store_dir: str,
    now: datetime | None = None,
    chain_feeds: Sequence[str] | None = None,
    bar_feeds: Sequence[str] | None = None,
    context_factory: Callable[[str], FeedContext] = live_context,
    live_models: bool = False,
    grounding: bool = False,
    cards: bool = False,
    paper_trade: bool = False,
) -> str:
    """Ingest plus brief plus session journals against loaded state.

    Shared by the standalone job and the in-app session drivers. Loads
    nothing and saves nothing; the caller owns the bundle.
    """
    moment = now or datetime.now(timezone.utc)
    symbols = tick_symbols(normalize_underlyings(underlying), store)
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
                    paper_trade,
                )
            )
            push_alerts(symbol, store, journal, moment)
        except FeedUnavailableError as exc:
            journal.append(moment, "session_failed", {"underlying": symbol, "reason": str(exc)})
            failures.append(f"scheduled session failed for {symbol}: {exc}")
    if replies:
        journal.append(moment, "session_saved", {"underlyings": symbols, "store_dir": store_dir})
    if failures and not replies:
        return "\n".join(failures)
    return "\n---\n".join(replies + failures)


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
    paper_trade: bool = False,
) -> str:
    store, journal = load_bundle(store_dir)
    text = run_session_core(
        underlying,
        store,
        journal,
        store_dir,
        now,
        chain_feeds,
        bar_feeds,
        context_factory,
        live_models,
        grounding,
        cards,
        paper_trade,
    )
    moment = now or datetime.now(timezone.utc)
    try:
        from bot.video import video_response

        requested = underlying if isinstance(underlying, str) else ",".join(underlying)
        video_response(
            "", requested, store, journal, moment,
            project_dir=os.path.join(store_dir, "video-brief"),
        )
    except Exception as exc:
        journal.append(moment, "video_brief_failed", {"error": type(exc).__name__})
    save_bundle(store_dir, store, journal)
    return text


def session_store(underlying: str, store_dir: str) -> tuple[SnapshotStore, Journal]:
    del underlying
    return load_bundle(store_dir)


def refresh_core(
    underlying: str | Sequence[str],
    store: SnapshotStore,
    journal: Journal,
    now: datetime | None = None,
    chain_feeds: Sequence[str] | None = None,
    bar_feeds: Sequence[str] | None = None,
    context_factory: Callable[[str], FeedContext] = live_context,
    store_dir: str = "",
) -> dict[str, str | None]:
    """Ingest-only refresh against passed-in state.

    Appends ingest plus refresh_failed plus refreshed journals. Prunes
    nothing and saves nothing; the caller owns the bundle.
    """
    moment = now or datetime.now(timezone.utc)
    chains = tuple(chain_feeds) if chain_feeds is not None else CHAIN_FEEDS
    bars = tuple(bar_feeds) if bar_feeds is not None else BAR_FEEDS
    used: dict[str, str | None] = {}
    for symbol in normalize_underlyings(underlying):
        chain = ingest_first_working(chains, ingest_chain, symbol, store, journal, moment, context_factory)
        bars_used = ingest_first_working(bars, ingest_bars, symbol, store, journal, moment, context_factory)
        if bars_used != "finnhub":
            ingest_first_working(("finnhub",), ingest_bars, symbol, store, journal, moment, context_factory)
        if chain is None:
            journal.append(moment, "refresh_failed", {"underlying": symbol})
        used[symbol] = chain
    backfill_iv_history(store, journal, moment)
    journal.append(moment, "refreshed", {"underlyings": sorted(used), "store_dir": store_dir})
    return used


def refresh_store(
    underlying: str | Sequence[str],
    store: SnapshotStore,
    journal: Journal,
    store_dir: str,
    now: datetime | None = None,
    chain_feeds: Sequence[str] | None = None,
    bar_feeds: Sequence[str] | None = None,
    context_factory: Callable[[str], FeedContext] = live_context,
) -> dict[str, str | None]:
    """Ingest-only refresh: new snapshots and bars without minting signals.

    The serving loop calls this on a timer so snapshots stay fresh between
    restarts. Prunes in-memory growth and persists the bundle every pass.
    """
    used = refresh_core(
        underlying, store, journal, now, chain_feeds, bar_feeds, context_factory, store_dir
    )
    store.prune()
    journal.prune()
    save_bundle(store_dir, store, journal)
    return used
