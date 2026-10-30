from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional, Sequence

from bot.cards import render_brief_card
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
from bot.ideas import format_ideas, parse_timeframe, rank_ideas, resolve_direction, symbols_for, unknown_symbols
from bot.journal import Journal
from bot.profile import build_profile, format_questionnaire, format_summary, parse_answers, suggest_params
from bot.models import (
    MODEL_REGISTRY,
    HttpPost,
    gex_tool_result,
    model_api_key,
    prompt_hash,
    receipt_payload,
    run_analyst,
    run_responses,
    select_backend,
)
from bot.shapes import (
    Bar,
    ChainSnapshot,
    DexProfile,
    GexProfile,
    OperatorConfig,
    PaperEvidence,
    Signal,
    VexProfile,
)
from bot.store import SnapshotStore
from bot.tradecards import cards_response
from bot.wheel import wheel_response


def analyst_tool(text: str, snapshot: ChainSnapshot, profile: GexProfile, bar: Optional[Bar]) -> Signal:
    direction = resolve_direction(bar, profile.flip_point, profile.total_exposure)
    return Signal(
        direction=direction,
        thesis=f"analyst read on {snapshot.underlying} for query: {text}",
        confidence=0.5,
        snapshot_id="",
    )


ANALYST_SYSTEM = (
    "You must call get_gex_profile exactly once for the underlying before answering. "
    "Reply with a one-paragraph thesis, then two lines, exactly: "
    "DIRECTION: long|short|flat and CONFIDENCE: 0 to 1."
)

GEX_TOOL = {
    "type": "function",
    "function": {
        "name": "get_gex_profile",
        "description": "Read the computed GEX profile for an underlying",
        "parameters": {
            "type": "object",
            "properties": {"underlying": {"type": "string"}},
            "required": ["underlying"],
        },
    },
}


def _analyst_prompt(text: str, snapshot: ChainSnapshot, profile: GexProfile, bar: Optional[Bar]) -> str:
    summary = {
        "query": text,
        "underlying": snapshot.underlying,
        "total_exposure": profile.total_exposure,
        "flip_point": profile.flip_point,
        "per_strike_exposure": profile.per_strike_exposure,
        "bar_close": bar.close if bar is not None else None,
    }
    return f"Snapshot: {json.dumps(summary)}"


def _parse_direction(text: str, fallback: str) -> str:
    match = re.search(r"DIRECTION:\s*(long|short|flat)", text, re.IGNORECASE)
    return match.group(1).lower() if match else fallback


def _parse_confidence(text: str) -> float:
    match = re.search(r"CONFIDENCE:\s*([0-9]*\.?[0-9]+)", text)
    if not match:
        return 0.5
    try:
        return min(1.0, max(0.0, float(match.group(1))))
    except ValueError:
        return 0.5


def _strip_markers(text: str) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not re.match(r"\s*(DIRECTION|CONFIDENCE)\s*:", line, re.IGNORECASE)
    ]
    return "\n".join(lines).strip()


def spark_signal(
    text: str,
    snapshot_id: str,
    snapshot: ChainSnapshot,
    profile: GexProfile,
    bar: Optional[Bar],
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None,
    http_post: HttpPost | None,
    grounding: bool = False,
) -> Signal:
    spec = MODEL_REGISTRY["analyst"]
    prompt = _analyst_prompt(text, snapshot, profile, bar)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": ANALYST_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    reply, receipts = run_analyst(
        spec,
        model_api_key(env),
        messages,
        [GEX_TOOL],
        {"get_gex_profile": lambda args: gex_tool_result(snapshot, snapshot_id, args)},
        prompt,
        http_post,
    )
    for receipt in receipts:
        journal.append(now, "model_receipt", receipt_payload(receipt, snapshot_id))
    thesis = _strip_markers(reply)
    if not thesis:
        raise ValueError("empty model thesis")
    if grounding:
        note = grounded_note(
            f"Is there current market news that contradicts this thesis on {snapshot.underlying}? One sentence.",
            journal,
            now,
            snapshot_id,
            env,
            http_post,
        )
        if note:
            thesis = f"{thesis}\nGrounded note: {note}"
    fallback_direction = analyst_tool(text, snapshot, profile, bar).direction
    return Signal(
        direction=_parse_direction(reply, fallback_direction),
        thesis=thesis,
        confidence=_parse_confidence(reply),
        snapshot_id=snapshot_id,
    )


def grounded_note(
    question: str,
    journal: Journal,
    now: datetime,
    snapshot_id: str,
    env: Mapping[str, str] | None,
    http_post: HttpPost | None,
) -> str:
    backend, reason = select_backend("grounding", journal, now, env)
    if backend != "model":
        journal.append(now, "grounding_skipped", {"reason": reason})
        return ""
    try:
        text, receipt = run_responses(
            MODEL_REGISTRY["grounding"],
            model_api_key(env),
            question,
            [{"type": "web_search"}],
            question,
            "grounding",
            http_post,
        )
    except Exception as exc:
        journal.append(now, "grounding_skipped", {"reason": type(exc).__name__})
        return ""
    journal.append(now, "model_receipt", receipt_payload(receipt, snapshot_id))
    return " ".join(text.split())


REVIEW_SYSTEM = (
    "You review paper-trading evidence for a go-live decision. Reply with a short risk review: "
    "top 3 concerns, then one line VERDICT: ready|not-ready. You advise only and approve nothing."
)


def _review_prompt(evidence: PaperEvidence, journal: Journal) -> str:
    kinds: dict[str, int] = {}
    signals = []
    for entry in journal.entries():
        kinds[entry.kind] = kinds.get(entry.kind, 0) + 1
        if entry.kind == "signal":
            signals.append(entry.payload)
    return json.dumps(
        {
            "evidence": {
                "clean_days": evidence.clean_days,
                "clean_days_target": evidence.clean_days_target,
                "max_drawdown": evidence.max_drawdown,
                "drawdown_bound": evidence.drawdown_bound,
                "halt_drills_passed": evidence.halt_drills_passed,
            },
            "journal_kinds": kinds,
            "recent_signals": signals[-3:],
        }
    )


def advisory_review(
    evidence: PaperEvidence,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> str:
    if not live_models:
        journal.append(now, "review_skipped", {"reason": "live models off"})
        return ""
    backend, reason = select_backend("reviewer", journal, now, env)
    if backend != "model":
        journal.append(now, "review_skipped", {"reason": reason})
        return ""
    prompt = _review_prompt(evidence, journal)
    try:
        text, receipts = run_analyst(
            MODEL_REGISTRY["reviewer"],
            model_api_key(env),
            [
                {"role": "system", "content": REVIEW_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            [],
            {},
            prompt,
            http_post,
            reasoning_effort="high",
        )
    except Exception as exc:
        journal.append(now, "review_skipped", {"reason": type(exc).__name__})
        return ""
    for receipt in receipts:
        journal.append(now, "model_receipt", receipt_payload(receipt, ""))
    note = " ".join(text.split())
    if not note:
        journal.append(now, "review_skipped", {"reason": "empty review"})
        return ""
    journal.append(now, "review_note", {"note": note, "prompt_hash": prompt_hash(prompt)})
    return note


def analyst_backend(
    text: str,
    snapshot_id: str,
    snapshot: ChainSnapshot,
    profile: GexProfile,
    bar: Optional[Bar],
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    grounding: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> Signal:
    if not live_models:
        journal.append(now, "model_fallback", {"reason": "live models off"})
        return analyst_tool(text, snapshot, profile, bar)
    backend, reason = select_backend("analyst", journal, now, env)
    if backend != "model":
        journal.append(now, "model_fallback", {"reason": reason})
        return analyst_tool(text, snapshot, profile, bar)
    try:
        return spark_signal(text, snapshot_id, snapshot, profile, bar, journal, now, env, http_post, grounding)
    except Exception as exc:
        journal.append(now, "model_fallback", {"reason": type(exc).__name__})
        return analyst_tool(text, snapshot, profile, bar)


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


def _money(value: float) -> str:
    return f"${value:,.0f}"


def _format_age(age: timedelta) -> str:
    seconds = max(0, int(age.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def format_brief(
    profile: GexProfile,
    signal: Signal,
    snapshot: ChainSnapshot,
    bar: Optional[Bar] = None,
    now: Optional[datetime] = None,
    previous: Optional[GexProfile] = None,
) -> str:
    spot = bar.close if bar is not None else None
    age = _format_age(now - snapshot.timestamp_utc) if now is not None else "unknown"
    regime = "long-gamma" if profile.total_exposure >= 0.0 else "short-gamma"
    units = "$GEX per 1% move" if profile.spot else "raw gamma x OI (no spot)"
    spot_text = f"{spot:.2f}" if spot is not None else "unknown"
    lines = [
        f"{snapshot.underlying} GEX brief -- snapshot {profile.snapshot_id} (age {age}, source {snapshot.source_name})",
        f"spot {spot_text} | coverage {signal.coverage * 100.0:.0f}% | regime {regime}",
    ]
    front = f"; front {profile.front_expiry}: {_money(profile.front_exposure)}" if profile.front_expiry else ""
    lines.append(f"total exposure: {_money(profile.total_exposure)} ({units}{front})")
    if profile.flip_point is not None and spot:
        pct = (profile.flip_point - spot) / spot * 100.0
        side = "above" if profile.flip_point >= spot else "below"
        lines.append(f"flip {profile.flip_point:.1f} ({abs(pct):.1f}% {side} spot)")
    elif profile.flip_point is not None:
        lines.append(f"flip {profile.flip_point:.1f} (no spot)")
    else:
        lines.append("flip none found")
    lines.append(f"calls {_money(profile.call_exposure)} / puts {_money(profile.put_exposure)}")
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))[:10]
    lines.append("walls: " + "; ".join(f"{strike:g} {_money(value)}" for strike, value in walls))
    if previous is None:
        lines.append("change vs previous: n/a (first snapshot)")
    else:
        delta = profile.total_exposure - previous.total_exposure
        if previous.total_exposure != 0:
            pct = delta / abs(previous.total_exposure) * 100.0
            note = ""
            if previous.total_exposure < 0:
                if delta > 0:
                    note = ", less negative"
                elif delta < 0:
                    note = ", more negative"
            lines.append(f"change vs previous: {_money(delta)} ({pct:+.1f}%{note})")
        else:
            lines.append(f"change vs previous: {_money(delta)}")
    basis = ""
    if (profile.flip_point is None or bar is None) and signal.direction != "flat":
        basis = " (regime fallback)"
    lines.append(f"direction: {signal.direction} confidence: {signal.confidence:.2f}{basis}")
    lines.append(f"status: {signal.status} trigger: {signal.trigger}")
    return "\n".join(lines)


def previous_profile(
    store: SnapshotStore, underlying: str, snapshot_id: str, spot: Optional[float] = None
) -> Optional[GexProfile]:
    found = store.read_previous(underlying, snapshot_id)
    if found is None:
        return None
    prev_id, prev_snapshot = found
    return compute_gex(prev_snapshot, prev_id, spot=spot)


def format_exposure_brief(
    kind: str,
    profile: VexProfile | DexProfile,
    snapshot: ChainSnapshot,
    bar: Optional[Bar],
    now: datetime,
) -> str:
    spot = bar.close if bar is not None else None
    age = _format_age(now - snapshot.timestamp_utc)
    units = "$ per vol point" if kind == "VEX" else "delta x OI x 100"
    spot_text = f"{spot:.2f}" if spot is not None else "unknown"
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))[:10]
    lines = [
        f"{snapshot.underlying} {kind} brief -- snapshot {profile.snapshot_id} (age {age}, source {snapshot.source_name})",
        f"spot {spot_text}",
        f"total exposure: {profile.total_exposure:.1f} ({units})",
        "walls: " + "; ".join(f"{strike:g} {value:.1f}" for strike, value in walls),
    ]
    return "\n".join(lines)


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
        raise ValueError(f"no snapshot stored for {underlying}")
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
        available = ", ".join(store.list_underlyings()) or "none"
        raise ValueError(
            f"no snapshot stored for {missing} (store has: {available}); "
            f"ingest it first, e.g. run_session.py --underlying {unknown[0]}"
        )
    max_days = parse_timeframe(text)
    entries = []
    used = []
    snapshot_ids = []
    for symbol in symbols_for(text, store, underlying):
        found = store.read_latest(symbol)
        if found is None:
            continue
        snapshot_id, snapshot = found
        entries.append((snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), store.read_latest_bar(symbol)))
        used.append(symbol)
        snapshot_ids.append(snapshot_id)
    if not entries:
        raise ValueError(f"no snapshot stored for {underlying}")
    ideas = rank_ideas(entries, max_days, now=now)
    journal.append(
        now,
        "ideas",
        {"query": text, "symbols": used, "timeframe": max_days, "count": len(ideas), "snapshot_ids": snapshot_ids},
    )
    return format_ideas(ideas)


def vex_brief_response(underlying: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(f"no snapshot stored for {underlying}")
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    profile = compute_vex(snapshot, snapshot_id)
    journal.append(now, "exposure_brief", {"kind": "VEX", "snapshot_id": snapshot_id})
    return format_exposure_brief("VEX", profile, snapshot, bar, now)


def dex_brief_response(underlying: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(f"no snapshot stored for {underlying}")
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


def paper_pnl_text(journal: Journal, store: SnapshotStore) -> str:
    net: dict[str, float] = {}
    cost: dict[str, float] = {}
    for entry in journal.entries():
        if entry.kind != "fill":
            continue
        symbol = entry.payload.get("symbol")
        price = entry.payload.get("price")
        size = entry.payload.get("size")
        if not symbol or price is None or size is None:
            continue
        side = entry.payload.get("side", "buy")
        signed = float(size) if side == "buy" else -float(size)
        net[symbol] = net.get(symbol, 0.0) + signed
        cost[symbol] = cost.get(symbol, 0.0) + signed * float(price)
    if not net:
        return "paper pnl: no fills journaled"
    lines = []
    total = 0.0
    for symbol in sorted(net):
        bar = store.read_latest_bar(symbol)
        if bar is None or net[symbol] == 0:
            continue
        average = cost[symbol] / net[symbol]
        pnl = (bar.close - average) * net[symbol]
        total += pnl
        lines.append(f"{symbol}: size {net[symbol]:.0f} avg {average:.2f} mark {bar.close:.2f} pnl {pnl:+.2f}")
    lines.append(f"total paper pnl: {total:+.2f}")
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
    lowered = text.strip().lower()
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
    if "profile" in lowered:
        return format_questionnaire()
    if re.search(r"\bwheels?\b", lowered):
        return wheel_response(text, underlying, store, journal, now)
    if re.search(r"\bcards?\b", lowered):
        return cards_response(text, underlying, store, journal, now)
    if "idea" in lowered:
        return ideas_response(text, underlying, store, journal, now)
    if re.search(r"\bvex\b", lowered) or "vega exposure" in lowered:
        return vex_brief_response(underlying, store, journal, now)
    if re.search(r"\bdex\b", lowered) or "delta exposure" in lowered:
        return dex_brief_response(underlying, store, journal, now)
    reply, _ = handle_chat_query(text, underlying, store, journal, now, live_models, grounding, cards)
    return reply


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
) -> str:
    chains = tuple(chain_feeds) if chain_feeds is not None else CHAIN_FEEDS
    bars = tuple(bar_feeds) if bar_feeds is not None else BAR_FEEDS
    if ingest_first_working(chains, ingest_chain, underlying, store, journal, now, context_factory) is None:
        raise FeedUnavailableError(f"all chain feeds failed for {underlying}")
    ingest_first_working(bars, ingest_bars, underlying, store, journal, now, context_factory)
    reply, _ = handle_chat_query("live session brief", underlying, store, journal, now, live_models, grounding, cards)
    return reply
