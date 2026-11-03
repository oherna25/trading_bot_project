"""Research loop facade: chat queries, routing, live briefs."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime
from typing import Callable, Mapping, Optional, Sequence

from bot.cards import render_brief_card
from bot.diff import snapshot_diff_text
from bot.exposure import compute_charm, compute_dex, compute_gex, compute_vanna, compute_vex
from bot.feeds import (
    BAR_FEEDS,
    CHAIN_FEEDS,
    INTRADAY_TOP50,
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
from bot.research_formatters import (_format_age, derive_display, format_bars_brief, format_brief, format_exposure_brief, format_intraday_brief, previous_profile, wall_migration_text)
from bot.research_ingest import _record_option_iv, ensure_intraday, ingest_first_working
from bot.research_paper import PAPER_ACCOUNT_ID, open_trade_size, paper_pnl_text, paper_trade_step, take_card_trade

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
        bars_only = store.read_bars(underlying)
        if not bars_only:
            raise ValueError(need_snapshot_text(underlying, store))
        latest_bar = max(bars_only, key=lambda bar: bar.timestamp_utc)
        stamp = max(now, latest_bar.timestamp_utc)
        reply = format_bars_brief(underlying, bars_only, now)
        signal = Signal(
            direction="flat",
            thesis=f"bars-only price brief for {underlying}; no options snapshot",
            confidence=0.0,
            snapshot_id="",
            symbol=underlying,
        )
        journal.append(
            stamp, "bars_brief",
            {
                "symbol": underlying,
                "days": len(bars_only),
                "first": min(bar.timestamp_utc for bar in bars_only).date().isoformat(),
                "last": latest_bar.timestamp_utc.date().isoformat(),
            },
        )
        return reply, signal
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


def charm_brief_response(underlying: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(need_snapshot_text(underlying, store))
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    profile = compute_charm(snapshot, snapshot_id)
    journal.append(now, "exposure_brief", {"kind": "CHARM", "snapshot_id": snapshot_id})
    return format_exposure_brief("CHARM", profile, snapshot, bar, now)


def vanna_brief_response(underlying: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(need_snapshot_text(underlying, store))
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    profile = compute_vanna(snapshot, snapshot_id)
    journal.append(now, "exposure_brief", {"kind": "VANNA", "snapshot_id": snapshot_id})
    return format_exposure_brief("VANNA", profile, snapshot, bar, now)


def intraday_response(
    text: str,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    context_factory: Callable[[str], FeedContext] = live_context,
) -> str:
    candidates = symbols_for(text, store, underlying)
    symbol = (candidates[0] if candidates else underlying).upper()
    if symbol not in INTRADAY_TOP50:
        journal.append(now, "intraday_blocked", {"symbol": symbol})
        return f"intraday covers the top 50 only; {symbol} is outside it"
    try:
        bars, source = ensure_intraday(symbol, store, journal, now, context_factory)
    except Exception as exc:
        journal.append(now, "feed_failed", {"feed_name": "yfinance_intraday", "error": type(exc).__name__})
        return f"intraday fetch failed for {symbol}: {type(exc).__name__}"
    journal.append(now, "intraday_brief", {"symbol": symbol, "bars": len(bars), "source": source})
    return format_intraday_brief(symbol, bars, now, source)


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
    if re.search(r"\bagent cycle\b|\brun agent\b", lowered):
        from bot.agent import run_agent_cycle

        return run_agent_cycle(store, journal, now, underlying)
    if re.search(r"\bdeploy(ment|ments|ed)?\b|\bstrategies\b|\bpause deployment\b|\bresume deployment\b|\brun deployments\b|\bstrategy track record\b|\bdeployment track record\b", lowered):
        from bot.deployments import deployments_response

        return deployments_response(text, store, journal, now)
    if re.search(r"\bwheels?\b", lowered):
        return wheel_response(text, underlying, store, journal, now)
    if re.search(r"\bbuilder\b", lowered):
        from bot.builder import builder_preview_text
        from urllib.parse import parse_qs

        builder_params: dict[str, str] = {"underlying": underlying}
        if "?" in text:
            query = text.split("?", 1)[1]
            for key, values in parse_qs(query).items():
                if values:
                    builder_params[key] = values[0]
        return (
            builder_preview_text(store, journal, now, builder_params)
            + "\nopen /builder in the dashboard for the visual form"
        )
    if re.search(r"\bmy strategy\b|\bmy play\b|\bmy setup\b|\bbacktest my\b|\bevaluate my\b|\btest my\b|\bstrategy:", lowered):
        from bot.strategy import evaluate_strategy, parse_strategy

        return evaluate_strategy(parse_strategy(text, underlying, store), store, journal, now)
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
    if re.search(r"\bspark audit\b|\bspark check\b|\baudit.*spark\b", lowered):
        from bot.spark import spark_audit_response

        return spark_audit_response(
            text, underlying, store, journal, now, live_models, grounding
        )
    if re.search(r"\bdex\b", lowered) or "delta exposure" in lowered:
        return dex_brief_response(underlying, store, journal, now)
    if re.search(r"\bcharm\b", lowered) or "charm exposure" in lowered:
        return charm_brief_response(underlying, store, journal, now)
    if re.search(r"\bvanna\b", lowered) or "vanna exposure" in lowered:
        return vanna_brief_response(underlying, store, journal, now)
    if re.search(r"\bintraday\b", lowered):
        return intraday_response(text, underlying, store, journal, now)
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
    import threading
    from concurrent.futures import ThreadPoolExecutor

    lock = threading.RLock()
    chain_result: list[Optional[str]] = [None]
    bars_result: list[Optional[str]] = [None]

    def _chain_job() -> None:
        with lock:
            chain_result[0] = ingest_first_working(
                chains, ingest_chain, underlying, store, journal, now, context_factory
            )

    def _bars_job() -> None:
        with lock:
            used = ingest_first_working(
                bars, ingest_bars, underlying, store, journal, now, context_factory
            )
            if used != "finnhub":
                ingest_first_working(
                    ("finnhub",), ingest_bars, underlying, store, journal, now, context_factory
                )
            bars_result[0] = used

    with ThreadPoolExecutor(max_workers=2) as pool:
        chain_future = pool.submit(_chain_job)
        bars_future = pool.submit(_bars_job)
        chain_future.result()
        bars_future.result()
    if chain_result[0] is None:
        raise FeedUnavailableError(f"all chain feeds failed for {underlying}")
    backfill_iv_history(store, journal, now)
    reply, signal = handle_chat_query(
        "live session brief", underlying, store, journal, now, live_models, grounding, cards
    )
    _record_option_iv(underlying, store, journal, now, context_factory)
    if live_models:
        from bot.spark import spark_audit_response

        try:
            audit = spark_audit_response(
                "live session spark audit", underlying, store, journal, now,
                live_models=True, grounding=grounding,
            )
            reply += f"\n{audit}"
        except ValueError:
            pass
    if paper_trade:
        note = paper_trade_step(underlying, signal, store.read_latest_bar(underlying), journal, now, store)
        if note:
            reply += f"\n{note}"
    return reply

__all__ = [
    "advisory_review",
    "analyst_backend",
    "analyst_tool",
    "change_alerts",
    "derive_display",
    "charm_brief_response",
    "dex_brief_response",
    "format_brief",
    "grounded_note",
    "handle_chat_query",
    "ideas_response",
    "ingest_first_working",
    "intraday_response",
    "journal_tail_text",
    "live_brief",
    "load_response",
    "open_trade_size",
    "PAPER_ACCOUNT_ID",
    "paper_pnl_text",
    "paper_trade_step",
    "route_chat",
    "scheduled_brief",
    "take_card_trade",
    "vanna_brief_response",
    "vex_brief_response",
    "wall_migration_text",
]
