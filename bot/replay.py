from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Optional, Sequence

from bot.exposure import compute_gex
from bot.journal import Journal
from bot.research import analyst_tool, derive_display
from bot.risk import build_intent, review_intent, submit_paper
from bot.shapes import Bar, ChainSnapshot, OrderIntent, PaperEvidence, Position, RiskLimits, RiskState
from bot.store import SnapshotStore, snapshot_id_for


def _bar_at(bars: Sequence[Bar], moment: datetime) -> Optional[Bar]:
    known = [bar for bar in bars if bar.timestamp_utc <= moment]
    if not known:
        return None
    return max(known, key=lambda b: b.timestamp_utc)


def replay_week(
    snapshot_list: list[ChainSnapshot],
    journal: Journal,
    bars: Optional[Sequence[Bar]] = None,
    state: Optional[RiskState] = None,
    limits: Optional[RiskLimits] = None,
) -> Journal:
    ordered = sorted(snapshot_list, key=lambda s: s.timestamp_utc)
    if bars is None:
        for snapshot in ordered:
            snapshot_id = snapshot_id_for(snapshot)
            profile = compute_gex(snapshot, snapshot_id)
            journal.append(
                snapshot.timestamp_utc,
                "replay_step",
                {"snapshot_id": snapshot_id, "total_exposure": profile.total_exposure},
            )
        return journal
    active_state = state or RiskState(
        account_size=100000.0, risk_per_trade=0.01, day_pnl=0.0, halt_active=False, kill_active=False
    )
    active_limits = limits or RiskLimits(max_trade_size=5000.0, day_profit_target=500.0, max_day_loss=1000.0)
    position: Optional[Position] = None
    for snapshot in ordered:
        snapshot_id = snapshot_id_for(snapshot)
        spot_bar = _bar_at(bars, snapshot.timestamp_utc)
        spot = spot_bar.close if spot_bar is not None else None
        profile = compute_gex(snapshot, snapshot_id, spot=spot)
        journal.append(
            snapshot.timestamp_utc,
            "replay_step",
            {"snapshot_id": snapshot_id, "total_exposure": profile.total_exposure},
        )
        if spot_bar is None:
            continue
        signal = derive_display(
            replace(analyst_tool("replay", snapshot, profile, spot_bar), snapshot_id=snapshot_id),
            snapshot,
            profile,
            spot_bar,
        )
        journal.append(
            snapshot.timestamp_utc,
            "signal",
            {"snapshot_id": snapshot_id, "direction": signal.direction, "thesis": signal.thesis},
        )
        action = "flat"
        intent = build_intent(signal, f"replay-{snapshot_id}", active_state, journal, snapshot.timestamp_utc)
        if intent is not None:
            stamp = snapshot.timestamp_utc
            want = 1.0 if signal.direction == "long" else -1.0
            held = 0.0 if position is None else position.net_size
            if held == 0:
                if review_intent(intent, active_state, active_limits, journal, stamp) == "approved":
                    _, position, active_state = submit_paper(
                        intent, spot_bar.close, active_state, position, active_limits, journal, stamp
                    )
                    action = "open"
                else:
                    action = "vetoed"
            elif (held > 0) == (want > 0):
                journal.append(
                    stamp, "replay_hold", {"snapshot_id": snapshot_id, "direction": signal.direction}
                )
                action = "hold"
            else:
                close = OrderIntent(
                    symbol=snapshot.underlying,
                    side="sell" if held > 0 else "buy",
                    size=abs(held),
                    risk_numbers={"size": abs(held), "close": True},
                    signal_id=f"replay-close-{snapshot_id}",
                    legs=(),
                )
                journal.append(
                    stamp, "intent_proposed", {"signal_id": close.signal_id, "size": close.size, "legs": []}
                )
                if review_intent(close, active_state, active_limits, journal, stamp) == "approved":
                    _, position, active_state = submit_paper(
                        close, spot_bar.close, active_state, position, active_limits, journal, stamp
                    )
                    action = "close"
                else:
                    action = "vetoed"
        journal.append(
            snapshot.timestamp_utc,
            "backtest_step",
            {
                "snapshot_id": snapshot_id,
                "direction": signal.direction,
                "day_pnl": active_state.day_pnl,
                "action": action,
            },
        )
    return journal


def replay_report(
    underlying: str,
    store: SnapshotStore,
    clean_days_target: int = 10,
    drawdown_bound: float = 500.0,
) -> tuple[str, list[tuple[datetime, dict]], dict]:
    snaps = [
        (snapshot_id, snapshot)
        for snapshot_id, snapshot in store.snapshots_in_order()
        if snapshot.underlying == underlying
    ]
    if len(snaps) < 2:
        return f"replay {underlying}: needs at least 2 stored snapshots (has {len(snaps)})", [], {}
    bars = store.read_bars(underlying)
    if not bars:
        return f"replay {underlying}: needs stored bars", [], {}
    scratch = Journal()
    replay_week([snapshot for _, snapshot in snaps], scratch, bars=bars)
    evidence = paper_evidence_from_journal(scratch, clean_days_target, drawdown_bound)
    fills = [
        (entry.timestamp_utc, {**entry.payload, "venue": "replay"})
        for entry in scratch.entries()
        if entry.kind == "fill"
    ]
    steps = [entry for entry in scratch.entries() if entry.kind == "backtest_step"]
    last_pnl = float(steps[-1].payload.get("day_pnl", 0.0)) if steps else 0.0
    summary = {
        "underlying": underlying,
        "snapshots": len(snaps),
        "fills": len(fills),
        "day_pnl": last_pnl,
        "clean_days": evidence.clean_days,
        "max_drawdown": evidence.max_drawdown,
    }
    text = (
        f"replay {underlying}: {len(snaps)} snapshots, {len(fills)} fill{'s' if len(fills) != 1 else ''}, "
        f"day pnl {last_pnl:+.2f}\n"
        f"clean days {evidence.clean_days}/{evidence.clean_days_target}, "
        f"max drawdown {evidence.max_drawdown:.2f} (bound {evidence.drawdown_bound:.2f})"
    )
    return text, fills, summary


def replay_response(
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    clean_days_target: int = 10,
    drawdown_bound: float = 500.0,
) -> str:
    text, fills, summary = replay_report(underlying, store, clean_days_target, drawdown_bound)
    if not summary:
        return text
    for stamp, payload in fills:
        journal.append(stamp, "fill", payload)
    journal.append(now, "replay_summary", summary)
    return text


def paper_evidence_from_journal(
    journal: Journal,
    clean_days_target: int = 10,
    drawdown_bound: float = 500.0,
    halt_drills_passed: bool = False,
) -> PaperEvidence:
    by_day: dict[str, float] = {}
    halted_days: set[str] = set()
    for entry in journal.entries():
        day = entry.timestamp_utc.date().isoformat()
        if entry.kind == "backtest_step":
            by_day[day] = float(entry.payload.get("day_pnl", 0.0))
        elif entry.kind == "halt":
            halted_days.add(day)
    clean_days = sum(1 for day, pnl in by_day.items() if pnl >= 0 and day not in halted_days)
    peak = 0.0
    max_drawdown = 0.0
    for day in sorted(by_day):
        peak = max(peak, by_day[day])
        max_drawdown = max(max_drawdown, peak - by_day[day])
    return PaperEvidence(
        clean_days=clean_days,
        clean_days_target=clean_days_target,
        max_drawdown=max_drawdown,
        drawdown_bound=drawdown_bound,
        halt_drills_passed=halt_drills_passed,
    )
