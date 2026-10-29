from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Optional, Sequence

from bot.exposure import compute_gex
from bot.journal import Journal
from bot.research import analyst_tool, derive_display
from bot.risk import build_intent, review_intent, submit_paper
from bot.shapes import Bar, ChainSnapshot, PaperEvidence, Position, RiskLimits, RiskState
from bot.store import snapshot_id_for


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
        intent = build_intent(signal, f"replay-{snapshot_id}", active_state, journal, snapshot.timestamp_utc)
        if intent is None:
            continue
        if review_intent(intent, active_state, active_limits, journal, snapshot.timestamp_utc) != "approved":
            continue
        _, position, active_state = submit_paper(
            intent, spot_bar.close, active_state, position, active_limits, journal, snapshot.timestamp_utc
        )
        journal.append(
            snapshot.timestamp_utc,
            "backtest_step",
            {"snapshot_id": snapshot_id, "direction": signal.direction, "day_pnl": active_state.day_pnl},
        )
    return journal


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
