from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional, Sequence

from bot.cards import render_brief_card
from bot.diff import compact_dollars, signed_dollars, snapshot_diff_text
from bot.exposure import compute_dex, compute_gex, compute_vex
from bot.feeds import (
    BAR_FEEDS,
    CHAIN_FEEDS,
    FeedContext,
    FeedUnavailableError,
    fetch_alpaca_iv,
    fetch_yahoo_iv,
    ingest_bars,
    ingest_chain,
    live_context,
)
from bot.ideas import (
    cached_earnings,
    format_ideas,
    parse_timeframe,
    rank_ideas,
    resolve_direction,
    symbols_for,
    unknown_symbols,
)
from bot.ivrank import backfill_iv_history, histories_for
from bot.journal import Journal
from bot.localmodels import LocalRunner, local_available, run_local
from bot.profile import build_profile, format_questionnaire, format_summary, parse_answers, suggest_params
from bot.risk import build_intent, realized_pnl, review_intent, submit_paper, update_position
from bot.tiers import select_model_tier
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
    Fill,
    GexProfile,
    OperatorConfig,
    OrderIntent,
    PaperEvidence,
    Position,
    RiskLimits,
    RiskState,
    Signal,
    VexProfile,
)
from bot.store import SnapshotStore, need_snapshot_text
from bot.tradecards import PROFILE_REQUIRED, cards_response
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
        {
            "get_gex_profile": lambda args: gex_tool_result(
                snapshot, snapshot_id, args, spot=bar.close if bar is not None else None
            )
        },
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


def local_signal(
    text: str,
    snapshot_id: str,
    snapshot: ChainSnapshot,
    profile: GexProfile,
    bar: Optional[Bar],
    journal: Journal,
    now: datetime,
    grounding: bool = False,
    env: Mapping[str, str] | None = None,
    runner: LocalRunner | None = None,
) -> Signal:
    prompt = _analyst_prompt(text, snapshot, profile, bar)
    reply, receipt = run_local("analyst", prompt, env, runner=runner)
    journal.append(now, "model_receipt", receipt_payload(receipt, snapshot_id))
    thesis = _strip_markers(reply)
    if not thesis:
        raise ValueError("empty local thesis")
    if grounding:
        journal.append(now, "grounding_skipped", {"reason": "local tier"})
    fallback_direction = analyst_tool(text, snapshot, profile, bar).direction
    return Signal(
        direction=_parse_direction(reply, fallback_direction),
        thesis=thesis,
        confidence=_parse_confidence(reply),
        snapshot_id=snapshot_id,
    )


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
    local_runner: LocalRunner | None = None,
) -> Signal:
    if not live_models:
        journal.append(now, "model_fallback", {"reason": "live models off"})
        return analyst_tool(text, snapshot, profile, bar)
    decision = select_model_tier("analyst", journal, now, env)
    if decision.tier == "local":
        try:
            return local_signal(
                text, snapshot_id, snapshot, profile, bar, journal, now, grounding, env, local_runner
            )
        except Exception as exc:
            journal.append(now, "model_fallback", {"reason": type(exc).__name__})
            return analyst_tool(text, snapshot, profile, bar)
    if decision.tier == "fallback":
        journal.append(now, "model_fallback", {"reason": decision.reason})
        return analyst_tool(text, snapshot, profile, bar)
    try:
        return spark_signal(text, snapshot_id, snapshot, profile, bar, journal, now, env, http_post, grounding)
    except Exception as exc:
        if local_available(env):
            try:
                return local_signal(
                    text, snapshot_id, snapshot, profile, bar, journal, now, grounding, env, local_runner
                )
            except Exception:
                pass
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
    return compact_dollars(value)


def _gex_so_what(profile: GexProfile, spot: Optional[float]) -> str:
    regime = "long-gamma" if profile.total_exposure >= 0.0 else "short-gamma"
    dealers = "dampen" if regime == "long-gamma" else "amplify"
    bits = [f"{regime}: dealers {dealers} moves"]
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))
    if walls:
        strike = walls[0][0]
        if spot:
            dist = abs(strike - spot) / spot * 100.0
            pin = "pinned near " if dist <= 0.5 else ""
            bits.append(f"nearest magnet: {pin}{strike:g} ({dist:.1f}% away)")
        else:
            bits.append(f"nearest magnet: {strike:g}")
    if profile.flip_point is not None and spot:
        side = "above" if spot >= profile.flip_point else "below"
        bits.append(f"spot sits {side} flip {profile.flip_point:.1f}")
    return "so what: " + "; ".join(bits) + "."


def _exposure_so_what(kind: str, profile: VexProfile | DexProfile) -> str:
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))
    heavy = f"; heaviest strike {walls[0][0]:g}" if walls else ""
    if kind == "VEX":
        return f"so what: a 1pt IV rise is worth {compact_dollars(profile.total_exposure)} across the chain{heavy}."
    return f"so what: net {compact_dollars(profile.total_exposure)} share-equivalents{heavy}."


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
    stale_quote = False
    if spot is not None and bar is not None and now is not None:
        quote_age = now - bar.timestamp_utc
        snap_age = now - snapshot.timestamp_utc
        spot_text += f" (quote {_format_age(quote_age)})"
        stale_quote = quote_age > timedelta(minutes=15) and quote_age > 3 * snap_age
    lines = [
        f"{snapshot.underlying} GEX brief -- snapshot {profile.snapshot_id} (age {age}, source {snapshot.source_name})",
        f"spot {spot_text} | coverage {signal.coverage * 100.0:.0f}% | regime {regime}",
    ]
    if stale_quote:
        lines.append("STALE -- refresh the quote before trading")
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
    lines.append(_gex_so_what(profile, spot))
    if previous is None:
        lines.append("change vs previous: n/a (first snapshot)")
    else:
        delta = profile.total_exposure - previous.total_exposure
        if previous.total_exposure != 0:
            if (previous.total_exposure >= 0.0) != (profile.total_exposure >= 0.0):
                lines.append(f"change vs previous: {_money(delta)} (sign flip)")
            else:
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


SPARK_LEVELS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    if high == low:
        return SPARK_LEVELS[0] * len(values)
    return "".join(
        SPARK_LEVELS[min(7, int((value - low) / (high - low) * 7))] for value in values
    )


def wall_migration_text(underlying: str, store: SnapshotStore, now: datetime) -> str:
    snaps = [
        (snapshot_id, snapshot)
        for snapshot_id, snapshot in store.snapshots_in_order()
        if snapshot.underlying == underlying and snapshot.timestamp_utc.date() == now.date()
    ]
    if len(snaps) < 2:
        return ""
    bar = store.read_latest_bar(underlying)
    spot = bar.close if bar is not None else None
    profiles = [compute_gex(snapshot, snapshot_id, spot=spot) for snapshot_id, snapshot in snaps]
    first, last = profiles[0], profiles[-1]
    deltas = sorted(
        (
            (
                strike,
                last.per_strike_exposure.get(strike, 0.0) - first.per_strike_exposure.get(strike, 0.0),
            )
            for strike in set(first.per_strike_exposure) | set(last.per_strike_exposure)
        ),
        key=lambda item: -abs(item[1]),
    )[:3]
    lines = []
    movers = [(strike, delta) for strike, delta in deltas if delta != 0.0]
    if movers:
        lines.append("walls since the open:")
        for strike, delta in movers:
            before = first.per_strike_exposure.get(strike, 0.0)
            after = last.per_strike_exposure.get(strike, 0.0)
            lines.append(f"  {strike:g}: {_money(before)} -> {_money(after)} ({signed_dollars(delta)})")
    flips = [profile.flip_point for profile in profiles if profile.flip_point is not None]
    if flips:
        lines.append("flip path: " + " -> ".join(f"{flip:.1f}" for flip in flips))
    totals = [profile.total_exposure for profile in profiles]
    lines.append(f"day exposure: {_sparkline(totals)} {_money(totals[0])} -> {_money(totals[-1])}")
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
        f"total exposure: {compact_dollars(profile.total_exposure)} ({units})",
        "walls: " + "; ".join(f"{strike:g} {compact_dollars(value)}" for strike, value in walls),
        _exposure_so_what(kind, profile),
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


PAPER_TRADE_STATE = RiskState(
    account_size=100000.0, risk_per_trade=0.01, day_pnl=0.0, halt_active=False, kill_active=False
)
PAPER_TRADE_LIMITS = RiskLimits(max_trade_size=5000.0, day_profit_target=500.0, max_day_loss=1000.0)


def open_trade_size(journal: Journal, symbol: str) -> float:
    net = 0.0
    for entry in journal.entries():
        if entry.kind != "fill" or entry.payload.get("symbol") != symbol:
            continue
        if entry.payload.get("venue", "paper") == "replay":
            continue
        size = entry.payload.get("size")
        if size is None:
            continue
        net += float(size) if entry.payload.get("side", "buy") == "buy" else -float(size)
    return net


def paper_state_from_journal(journal: Journal, symbol: str, mark: float, now: datetime) -> RiskState:
    positions: dict[str, Optional[Position]] = {}
    day_pnl = 0.0
    for entry in journal.entries():
        if entry.kind != "fill" or entry.payload.get("venue", "paper") == "replay":
            continue
        fill_symbol = entry.payload.get("symbol")
        price = entry.payload.get("price")
        size = entry.payload.get("size")
        if not fill_symbol or price is None or size is None:
            continue
        intent = OrderIntent(
            symbol=str(fill_symbol),
            side=str(entry.payload.get("side", "buy")),
            size=float(size),
            risk_numbers={},
            signal_id="journal-replay",
            legs=(),
        )
        fill = Fill(price=float(price), size=float(size), timestamp_utc=entry.timestamp_utc, venue="paper")
        old = positions.get(str(fill_symbol))
        if entry.timestamp_utc.date() == now.date():
            day_pnl += realized_pnl(old, intent, fill)
            day_pnl -= float(entry.payload.get("fees") or 0.0)
        positions[str(fill_symbol)] = update_position(old, intent, fill, float(price))
    current = positions.get(symbol)
    if current is not None and current.net_size != 0:
        day_pnl += (mark - current.average_price) * current.net_size
    return replace(PAPER_TRADE_STATE, day_pnl=day_pnl)


def paper_trade_step(
    symbol: str,
    signal: Signal,
    bar: Optional[Bar],
    journal: Journal,
    now: datetime,
) -> str:
    if bar is None:
        journal.append(now, "trade_skipped", {"underlying": symbol, "reason": "no bar"})
        return ""
    if open_trade_size(journal, symbol) != 0:
        journal.append(now, "trade_skipped", {"underlying": symbol, "reason": "position open"})
        return ""
    state = paper_state_from_journal(journal, symbol, bar.close, now)
    intent = build_intent(signal, f"live-{signal.snapshot_id}", state, journal, now)
    if intent is None:
        return ""
    if review_intent(intent, state, PAPER_TRADE_LIMITS, journal, now) != "approved":
        return ""
    fill, _, _ = submit_paper(intent, bar.close, state, None, PAPER_TRADE_LIMITS, journal, now)
    return f"paper trade: {intent.side} {intent.size:.0f} {symbol} @ {fill.price:.2f}"


def take_card_trade(
    index: int,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    snapshot_id: str | None = None,
) -> str:
    from bot.demo import demo_cards

    if store.read_profile() is None:
        return PROFILE_REQUIRED + " -- next: POST /profile with risk_tolerance, portfolio_size, knowledge, options_ok"
    if snapshot_id is None:
        latest = store.read_latest(underlying)
        if latest is None:
            raise ValueError(need_snapshot_text(underlying, store))
        snapshot_id = latest[0]
    _, cards = demo_cards(underlying, store, journal, now, snapshot_id)
    if not cards:
        raise ValueError(
            f"no cards for {underlying} snapshot {snapshot_id} "
            f"-- next: GET /cards?underlying={underlying} to see what is stored"
        )
    if index < 0 or index >= len(cards):
        raise ValueError(
            f"index {index} out of range (0-{len(cards) - 1}) "
            f"-- next: pick an index from GET /cards?underlying={underlying}"
        )
    card = cards[index]
    if card.direction not in ("long", "short"):
        return (
            f"card {index} for {card.symbol} is flat "
            f"-- next: pick another index from GET /cards?underlying={underlying}"
        )
    bar = store.read_latest_bar(card.symbol)
    if bar is None:
        return f"no stored bar for {card.symbol} -- next: say load {card.symbol} and I'll fetch it"
    signal = Signal(
        direction=card.direction, thesis=card.reason,
        confidence=card.confidence, snapshot_id=card.snapshot_id,
    )
    note = paper_trade_step(card.symbol, signal, bar, journal, now)
    if note:
        for entry in reversed(journal.entries()):
            if entry.kind == "fill" and entry.payload.get("symbol") == card.symbol:
                entry.payload["card_snapshot"] = card.snapshot_id
                entry.payload["card_index"] = index
                break
        return note
    reason = journal.entries()[-1].payload.get("reason", "vetoed") if journal.entries() else "vetoed"
    if reason == "position open":
        return (
            f"already holding {card.symbol} "
            f"-- next: close it via portfolio_command 'close {card.symbol}' or take another card"
        )
    return f"paper trade for {card.symbol} skipped ({reason}) -- next: check paper_pnl and retry"


def _fill_books(journal: Journal) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float]]:
    live_net: dict[str, float] = {}
    live_cost: dict[str, float] = {}
    replay_net: dict[str, float] = {}
    replay_cost: dict[str, float] = {}
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
        if entry.payload.get("venue", "paper") == "replay":
            book_net, book_cost = replay_net, replay_cost
        else:
            book_net, book_cost = live_net, live_cost
        old_net = book_net.get(symbol, 0.0)
        old_cost = book_cost.get(symbol, 0.0)
        new_net = old_net + signed
        if old_net == 0.0 or (old_net > 0) == (signed > 0):
            new_cost = old_cost + signed * float(price)
        elif new_net == 0.0:
            new_cost = 0.0
        elif abs(signed) < abs(old_net):
            new_cost = old_cost * (new_net / old_net)
        else:
            new_cost = new_net * float(price)
        book_net[symbol] = new_net
        book_cost[symbol] = new_cost
    return live_net, live_cost, replay_net, replay_cost


def _pnl_lines(
    net: dict[str, float], cost: dict[str, float], store: SnapshotStore, prefix: str
) -> tuple[list[str], float]:
    lines = []
    total = 0.0
    for symbol in sorted(net):
        bar = store.read_latest_bar(symbol)
        if bar is None or net[symbol] == 0:
            continue
        average = cost[symbol] / net[symbol]
        pnl = (bar.close - average) * net[symbol] + 0.0
        total += pnl
        lines.append(f"{prefix}{symbol}: size {net[symbol]:.0f} avg {average:.2f} mark {bar.close:.2f} pnl {pnl:+.2f}")
    return lines, total


def paper_pnl_text(journal: Journal, store: SnapshotStore) -> str:
    live_net, live_cost, replay_net, replay_cost = _fill_books(journal)
    if not live_net and not replay_net:
        return "paper pnl: no fills journaled"
    lines: list[str] = []
    if live_net:
        live_lines, live_total = _pnl_lines(live_net, live_cost, store, "")
        lines.extend(live_lines)
        lines.append(f"total paper pnl: {live_total:+.2f}")
    if replay_net:
        replay_lines, replay_total = _pnl_lines(replay_net, replay_cost, store, "replay ")
        lines.extend(replay_lines)
        lines.append(f"total replay pnl: {replay_total:+.2f}")
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
