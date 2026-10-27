from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Callable, Optional, Sequence

from bot.exposure import compute_gex
from bot.feeds import (
    BAR_FEEDS,
    CHAIN_FEEDS,
    FeedContext,
    FeedUnavailableError,
    ingest_bars,
    ingest_chain,
    live_context,
)
from bot.journal import Journal
from bot.shapes import (
    Bar,
    ChainSnapshot,
    DashboardLink,
    GexProfile,
    OperatorConfig,
    Signal,
)
from bot.store import SnapshotStore


def analyst_tool(text: str, snapshot: ChainSnapshot, profile: GexProfile, bar: Optional[Bar]) -> Signal:
    flip = profile.flip_point
    if flip is None or bar is None:
        direction = "flat"
    elif bar.close > flip:
        direction = "long"
    else:
        direction = "short"
    return Signal(
        direction=direction,
        thesis=f"analyst read on {snapshot.underlying} for query: {text}",
        confidence=0.5,
        snapshot_id="",
    )


def derive_display(signal: Signal, snapshot: ChainSnapshot, profile: GexProfile, bar: Optional[Bar]) -> Signal:
    contracts = snapshot.contracts_list
    checks = []
    if contracts:
        checks.append("has_contracts")
    if any(c.gamma is not None for c in contracts):
        checks.append("has_greeks")
    if profile.flip_point is not None:
        checks.append("flip_found")
    coverage = sum(1 for c in contracts if c.gamma is not None) / len(contracts) if contracts else 0.0
    aligned = (signal.direction == "long" and profile.total_exposure >= 0.0) or (
        signal.direction == "short" and profile.total_exposure < 0.0
    )
    if profile.flip_point is not None and bar is not None:
        entry_band = f"{abs(bar.close - profile.flip_point):.1f} points from flip"
    else:
        entry_band = "unknown"
    return replace(
        signal,
        status="aligned" if aligned else "watch",
        trigger=f"flip {profile.flip_point}" if profile.flip_point is not None else "none",
        entry_band=entry_band,
        checklist=tuple(checks),
        coverage=coverage,
    )


def format_brief(profile: GexProfile, signal: Signal) -> str:
    lines = [
        f"total exposure: {profile.total_exposure:.1f}",
        f"flip point: {profile.flip_point}",
        f"direction: {signal.direction} confidence: {signal.confidence:.2f}",
        f"status: {signal.status} trigger: {signal.trigger}",
    ]
    for strike in sorted(profile.per_strike_exposure):
        lines.append(f"strike {strike}: {profile.per_strike_exposure[strike]:.1f}")
    return "\n".join(lines)


def handle_chat_query(
    text: str, underlying: str, store: SnapshotStore, journal: Journal, now: datetime
) -> tuple[str, Signal]:
    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(f"no snapshot stored for {underlying}")
    snapshot_id, snapshot = found
    profile = compute_gex(snapshot, snapshot_id)
    bar = store.read_latest_bar(underlying)
    signal = replace(analyst_tool(text, snapshot, profile, bar), snapshot_id=snapshot_id)
    signal = derive_display(signal, snapshot, profile, bar)
    journal.append(now, "signal", {"snapshot_id": snapshot_id, "direction": signal.direction, "thesis": signal.thesis})
    return format_brief(profile, signal), signal


def scheduled_brief(underlying: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    reply, _ = handle_chat_query("scheduled session brief", underlying, store, journal, now)
    journal.append(now, "brief_sent", {"underlying": underlying})
    return reply


def build_dashboard_link(snapshot_id: str, config: OperatorConfig, now: datetime) -> DashboardLink:
    path = f"/dashboard/{snapshot_id}"
    return DashboardLink(
        snapshot_id=snapshot_id,
        url=config.dashboard_base_url + path,
        created_utc=now,
        expires_utc=now + timedelta(seconds=config.dashboard_link_expiry_seconds),
    )


def dashboard_link_response(
    underlying: str, store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime
) -> str:
    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(f"no snapshot stored for {underlying}")
    snapshot_id, _ = found
    link = build_dashboard_link(snapshot_id, config, now)
    journal.append(now, "dashboard_link_sent", {"snapshot_id": snapshot_id, "url": link.url})
    return f"dashboard for snapshot {snapshot_id}: {link.url}"


def route_chat(text: str, underlying: str, store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime) -> str:
    if "dashboard" in text.lower():
        return dashboard_link_response(underlying, store, journal, config, now)
    reply, _ = handle_chat_query(text, underlying, store, journal, now)
    return reply


def _ingest_first_working(
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


def live_brief(
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    chain_feeds: Optional[Sequence[str]] = None,
    bar_feeds: Optional[Sequence[str]] = None,
    context_factory: Callable[[str], FeedContext] = live_context,
) -> str:
    chains = tuple(chain_feeds) if chain_feeds is not None else CHAIN_FEEDS
    bars = tuple(bar_feeds) if bar_feeds is not None else BAR_FEEDS
    if _ingest_first_working(chains, ingest_chain, underlying, store, journal, now, context_factory) is None:
        raise FeedUnavailableError(f"all chain feeds failed for {underlying}")
    _ingest_first_working(bars, ingest_bars, underlying, store, journal, now, context_factory)
    reply, _ = handle_chat_query("live session brief", underlying, store, journal, now)
    return reply
