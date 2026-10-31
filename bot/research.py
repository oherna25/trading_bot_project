"""Research loop facade: chat queries, routing, live briefs."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime
from typing import Callable, Mapping, Optional, Sequence

from bot.cards import render_brief_card
from bot.diff import snapshot_diff_text
from bot.exposure import compute_dex, compute_gex, compute_vex
from bot.feeds import (
    BAR_FEEDS,
    CHAIN_FEEDS,
    FeedContext,
    FeedUnavailableError,
    ingest_bars,
    ingest_chain,
    live_context,
)
from bot.ideas import (
    cached_earnings,
    format_ideas,
    parse_timeframe,
    rank_ideas,
    symbols_for,
    unknown_symbols,
)
from bot.ivrank import backfill_iv_history, histories_for
from bot.journal import Journal
from bot.profile import build_profile, format_questionnaire, format_summary, parse_answers, suggest_params
from bot.models import (
    HttpPost,
)
from bot.shapes import (
    OperatorConfig,
    Signal,
)
from bot.store import SnapshotStore, need_snapshot_text
from bot.tradecards import cards_response
from bot.wheel import wheel_response

from bot.research_analyst import advisory_review, analyst_backend, analyst_tool, grounded_note
from bot.research_formatters import (_format_age, derive_display, format_brief, format_exposure_brief, previous_profile, wall_migration_text)
from bot.research_ingest import _record_option_iv, ingest_first_working
from bot.research_paper import open_trade_size, paper_pnl_text, paper_trade_step, take_card_trade

def handle_chat_query(
    text: str,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    grounding: bool = False,
    cards: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> tuple[str, Signal]:
    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(need_snapshot_text(underlying, store))
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    spot = bar.close if bar is not None else None
    profile = compute_gex(snapshot, snapshot_id, spot=spot)
    stamp = max(now, snapshot.timestamp_utc)
    signal = replace(
        analyst_backend(
            text, snapshot_id, snapshot, profile, bar, journal, stamp, live_models, grounding, env, http_post
        ),
        snapshot_id=snapshot_id,
    )
    signal = derive_display(signal, snapshot, profile, bar)
    journal.append(stamp, "signal", {"snapshot_id": snapshot_id, "direction": signal.direction, "thesis": signal.thesis})
    prev = previous_profile(store, underlying, snapshot_id, spot)
    reply = format_brief(profile, signal, snapshot, bar, now, prev)
    migration = wall_migration_text(underlying, store, now)
    if migration:
        reply += f"\n{migration}"
    if cards:
        card = render_brief_card(signal, snapshot, profile, journal, now, snapshot_id, env, http_post)
        if card is not None:
            reply += f"\ncard: {snapshot_id} {card['regime']} {card['art_hash'][:12]}"
    return reply, signal


STALE_SNAPSHOT_SECONDS = 6 * 3600


def change_alerts(
    underlying: str, store: SnapshotStore, journal: Journal, now: datetime
) -> list[str]:
    found = store.read_latest(underlying)
    if found is None:
        return []
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    spot = bar.close if bar is not None else None
    profile = compute_gex(snapshot, snapshot_id, spot=spot)
    alerts = []
    age_seconds = (now - snapshot.timestamp_utc).total_seconds()
    if age_seconds > STALE_SNAPSHOT_SECONDS:
        alerts.append(f"ALERT: snapshot is stale (age {_format_age(now - snapshot.timestamp_utc)})")
    prev = previous_profile(store, underlying, snapshot_id, spot)
    if prev is not None:
        regime = "long-gamma" if profile.total_exposure >= 0.0 else "short-gamma"
        prev_regime = "long-gamma" if prev.total_exposure >= 0.0 else "short-gamma"
        if regime != prev_regime:
            alerts.append(f"ALERT: regime flipped {prev_regime} -> {regime}")
        if bar is not None and profile.flip_point is not None and prev.flip_point is not None:
            before = bar.close - prev.flip_point
            after = bar.close - profile.flip_point
            if before == 0 or after == 0 or (before > 0) != (after > 0):
                alerts.append(f"ALERT: spot crossed the flip ({prev.flip_point:.1f} -> {profile.flip_point:.1f})")
    for alert in alerts:
        journal.append(now, "alert", {"underlying": underlying, "text": alert})
    return alerts


def scheduled_brief(
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    grounding: bool = False,
    cards: bool = False,
) -> str:
    reply, _ = handle_chat_query(
        "scheduled session brief", underlying, store, journal, now, live_models, grounding, cards
    )
    alerts = change_alerts(underlying, store, journal, now)
    if alerts:
        reply += "\n" + "\n".join(alerts)
    journal.append(now, "brief_sent", {"underlying": underlying})
    return reply


def ideas_response(text: str, underlying: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    profile = store.read_profile()
    if profile is not None and not profile.options_ok:
        journal.append(now, "ideas_blocked", {"reason": "options blocked by profile"})
        return "options ideas are disabled by your saved profile"
    unknown = unknown_symbols(text, store)
    if unknown:
        missing = ", ".join(unknown)
        raise ValueError(need_snapshot_text(missing, store))
    max_days = parse_timeframe(text)
    entries = []
    used = []
    snapshot_ids = []
    for symbol in symbols_for(text, store, underlying):
        found = store.read_latest(symbol)
        if found is None:
            continue
        snapshot_id, snapshot = found
        bar = store.read_latest_bar(symbol)
        entries.append(
            (
                snapshot_id,
                snapshot,
                compute_gex(snapshot, snapshot_id, spot=bar.close if bar is not None else None),
                bar,
            )
        )
        used.append(symbol)
        snapshot_ids.append(snapshot_id)
    if not entries:
        raise ValueError(need_snapshot_text(underlying, store))
    ideas = rank_ideas(
        entries,
        max_days,
        now=now,
        iv_history=histories_for(store, used),
        earnings=cached_earnings(used, now),
    )
    journal.append(
        now,
        "ideas",
        {"query": text, "symbols": used, "timeframe": max_days, "count": len(ideas), "snapshot_ids": snapshot_ids},
    )
    return format_ideas(ideas)


def vex_brief_response(underlying: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(need_snapshot_text(underlying, store))
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    profile = compute_vex(snapshot, snapshot_id)
    journal.append(now, "exposure_brief", {"kind": "VEX", "snapshot_id": snapshot_id})
    return format_exposure_brief("VEX", profile, snapshot, bar, now)


def dex_brief_response(underlying: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(need_snapshot_text(underlying, store))
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    profile = compute_dex(snapshot, snapshot_id)
    journal.append(now, "exposure_brief", {"kind": "DEX", "snapshot_id": snapshot_id})
    return format_exposure_brief("DEX", profile, snapshot, bar, now)


def journal_tail_text(journal: Journal, count: int = 20) -> str:
    entries = journal.entries()[-max(1, count):]
    if not entries:
        return "journal: empty"
    lines = [f"journal tail ({len(entries)}):"]
    for entry in entries:
        lines.append(f"{entry.timestamp_utc.isoformat()} {entry.kind} {json.dumps(entry.payload, default=str)}")
    return "\n".join(lines)

CHAIN_KEY_NAMES = ("TRADIER_ACCESS_TOKEN", "MARKETDATA_API_KEY")


def load_response(
    text: str, underlying: str, store: SnapshotStore, journal: Journal, now: datetime
) -> str:
    lines = []
    seen = []
    for symbol in symbols_for(text, store, underlying):
        seen.append(symbol)
        ctx = live_context(symbol)
        if not any((ctx.credentials or {}).get(name) for name in CHAIN_KEY_NAMES):
            lines.append(
                f"load {symbol} needs a chain key: set TRADIER_ACCESS_TOKEN or MARKETDATA_API_KEY"
            )
            continue
        used = ingest_first_working(CHAIN_FEEDS, ingest_chain, symbol, store, journal, now, live_context)
        if used is None:
            lines.append(f"load {symbol} failed on every chain feed -- check feed_failed journal entries")
            continue
        ingest_first_working(BAR_FEEDS, ingest_bars, symbol, store, journal, now, live_context)
        found = store.read_latest(symbol)
        snapshot_id = found[0] if found is not None else "unknown"
        lines.append(f"loaded {symbol} via {used}: snapshot {snapshot_id}")
    journal.append(now, "load", {"symbols": seen, "lines": len(lines)})
    return "\n".join(lines)

def route_chat(
    text: str,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    now: datetime,
    live_models: bool = False,
    grounding: bool = False,
    cards: bool = False,
) -> str:
    if text.strip().startswith("/"):
        from bot.slash import slash_response

        return slash_response(text, underlying, store, journal, config, now)
    lowered = text.strip().lower()
    from bot.onboarding import is_start, start_onboarding

    if is_start(text):
        return start_onboarding(journal, now)
    if re.search(r"\bexplain\b", lowered):
        from bot.explain import explain_position
        from bot.ideas import candidate_symbols

        match = re.search(r"explain\s+my\s+([A-Za-z]{1,6})\b", text, re.IGNORECASE)
        if match:
            symbol = match.group(1).upper()
        else:
            cands = candidate_symbols(text)
            symbol = cands[0] if cands else underlying
        return explain_position(symbol, store, journal, now)
    if lowered.startswith("profile:"):
        try:
            profile = build_profile(parse_answers(text))
        except ValueError as exc:
            return f"could not save profile: {exc}"
        store.save_profile(profile)
        params = suggest_params(profile)
        journal.append(
            now,
            "profile_accepted",
            {
                "risk_tolerance": profile.risk_tolerance,
                "portfolio_size": profile.portfolio_size,
                "knowledge": profile.knowledge,
                "options_ok": profile.options_ok,
                **params,
            },
        )
        return format_summary(profile, params)
    if "profile" in lowered or "questionnaire" in lowered:
        return format_questionnaire()
    if re.match(r"\s*(close|sell|flatten|roll)\b", lowered):
        from bot.commands import portfolio_response

        return portfolio_response(text, underlying, store, journal, now, live_models=live_models)
    if re.match(r"\s*load\b", lowered):
        return load_response(text, underlying, store, journal, now)
    if re.search(r"\bcancel\b.*\balert\b|\balert\b.*\bcancel\b", lowered):
        from bot.customalerts import cancel_rule_response

        match = re.search(r"rule\s*#?\s*(\d+)", lowered)
        return cancel_rule_response(match.group(1) if match else "", store, journal, now)
    if re.search(r"\blist\b.*\balerts?\b|\balert\s+rules?\b|\bmy\s+alerts?\b", lowered):
        from bot.customalerts import list_rules_text

        return list_rules_text(store)
    if re.search(r"\balert\s+me\s+if\b", lowered):
        from bot.customalerts import set_rule_response

        return set_rule_response(text, store, journal, now)
    if re.search(r"\bwheels?\b", lowered):
        return wheel_response(text, underlying, store, journal, now)
    if re.search(r"\bshare\b", lowered) and "card" in lowered:
        from bot.cards import share_card

        match = re.search(r"\bshare\b.*?(\d+)?", lowered)
        index = int(match.group(1)) if match and match.group(1) else 0
        try:
            return share_card(index, underlying, store, journal, now, live_models=live_models)
        except ValueError as exc:
            return str(exc)
    if re.search(r"\bcards?\b", lowered):
        return cards_response(text, underlying, store, journal, now)
    if "idea" in lowered:
        return ideas_response(text, underlying, store, journal, now)
    if re.search(r"\bvex\b", lowered) or "vega exposure" in lowered:
        return vex_brief_response(underlying, store, journal, now)
    if re.search(r"\bdex\b", lowered) or "delta exposure" in lowered:
        return dex_brief_response(underlying, store, journal, now)
    if re.search(r"\breplay\b", lowered):
        from bot.replay import replay_response

        return replay_response(underlying, store, journal, now)
    if re.search(r"\bbackfill\b", lowered):
        from bot.ivrank import backfill_iv_history

        return backfill_iv_history(store, journal, now)
    if re.search(r"\bdiffs?\b", lowered):
        return snapshot_diff_text(underlying, store)
    if re.search(r"\bshock\b", lowered):
        from bot.news import shock_news_response
        from bot.shock import SHOCK_USAGE, parse_shock_pct, shock_text

        shock_pct = parse_shock_pct(text)
        if shock_pct is None:
            return SHOCK_USAGE
        if "news" in lowered or "why" in lowered:
            return shock_news_response(
                underlying, store, journal, now, shock_pct, live_models=live_models
            )
        return shock_text(underlying, store, journal, now, shock_pct)
    if re.search(r"\bdebate\b", lowered):
        from bot.debate import card_debate

        match = re.search(r"\bdebate\b\s*(\d+)?", lowered)
        index = int(match.group(1)) if match and match.group(1) else 0
        try:
            return card_debate(underlying, store, journal, now, index=index, live_models=live_models)
        except ValueError as exc:
            return str(exc)
    if re.search(r"\bmorning\s+brief\b|\bopen\s+brief\b", lowered):
        from bot.morning import generate_open_brief

        return generate_open_brief(store, journal, now, underlying, live_models=live_models)
    if re.search(r"\bvideo\b", lowered):
        from bot.video import video_response

        return video_response(text, underlying, store, journal, now)
    if re.search(r"\bcoach\b", lowered):
        from bot.coach import coach_review

        return coach_review(store, journal, now, live_models=live_models)
    if "chart" in lowered and ("read" in lowered or "image" in lowered or "screenshot" in lowered):
        from bot.chartread import CHART_USAGE

        return CHART_USAGE
    if "document" in lowered and ("read" in lowered or "scan" in lowered):
        from bot.docread import DOC_USAGE

        return DOC_USAGE
    if "tier status" in lowered or "model status" in lowered or "model tiers" in lowered:
        from bot.tiers import format_tier_status, tier_status

        return format_tier_status(tier_status(journal, now))
    if re.search(r"\bholdings?\b|\bmy book\b", lowered):
        from bot.holdings import (
            HOLDINGS_FORMAT,
            HOLDINGS_WRITE_RE,
            holdings_text,
            save_holdings_shapes,
            save_spoken_holdings,
        )

        marks = [text.index(mark) for mark in ("[", "{") if mark in text]
        if marks:
            try:
                return save_holdings_shapes(text[min(marks):], store, journal, now)
            except ValueError as exc:
                return str(exc)
        if HOLDINGS_WRITE_RE.search(text):
            try:
                return save_spoken_holdings(text, store, journal, now)
            except ValueError as exc:
                return f"{exc}\n{HOLDINGS_FORMAT}"
        return holdings_text(store)
    from bot.onboarding import answer_onboarding, onboarding_in_progress

    if onboarding_in_progress(journal) and "brief" not in lowered and "gex" not in lowered:
        return answer_onboarding(text, underlying, store, journal, now)
    reply, _ = handle_chat_query(text, underlying, store, journal, now, live_models, grounding, cards)
    return reply

def live_brief(
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    chain_feeds: Optional[Sequence[str]] = None,
    bar_feeds: Optional[Sequence[str]] = None,
    context_factory: Callable[[str], FeedContext] = live_context,
    live_models: bool = False,
    grounding: bool = False,
    cards: bool = False,
    paper_trade: bool = False,
) -> str:
    chains = tuple(chain_feeds) if chain_feeds is not None else CHAIN_FEEDS
    bars = tuple(bar_feeds) if bar_feeds is not None else BAR_FEEDS
    if ingest_first_working(chains, ingest_chain, underlying, store, journal, now, context_factory) is None:
        raise FeedUnavailableError(f"all chain feeds failed for {underlying}")
    bars_used = ingest_first_working(bars, ingest_bars, underlying, store, journal, now, context_factory)
    if bars_used != "finnhub":
        ingest_first_working(("finnhub",), ingest_bars, underlying, store, journal, now, context_factory)
    backfill_iv_history(store, journal, now)
    reply, signal = handle_chat_query(
        "live session brief", underlying, store, journal, now, live_models, grounding, cards
    )
    _record_option_iv(underlying, store, journal, now, context_factory)
    if paper_trade:
        note = paper_trade_step(underlying, signal, store.read_latest_bar(underlying), journal, now)
        if note:
            reply += f"\n{note}"
    return reply

__all__ = [
    "advisory_review",
    "analyst_backend",
    "analyst_tool",
    "change_alerts",
    "derive_display",
    "dex_brief_response",
    "format_brief",
    "grounded_note",
    "handle_chat_query",
    "ideas_response",
    "ingest_first_working",
    "journal_tail_text",
    "live_brief",
    "load_response",
    "open_trade_size",
    "paper_pnl_text",
    "paper_trade_step",
    "route_chat",
    "scheduled_brief",
    "take_card_trade",
    "vex_brief_response",
    "wall_migration_text",
]
