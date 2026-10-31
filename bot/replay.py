from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Optional, Sequence

from bot.diff import compact_dollars, signed_dollars
from bot.exposure import compute_gex
from bot.journal import Journal
from bot.research import analyst_tool, derive_display
from bot.risk import build_intent, review_intent, submit_paper
from bot.shapes import Bar, ChainSnapshot, OrderIntent, PaperEvidence, Position, RiskLimits, RiskState
from bot.store import SnapshotStore, snapshot_id_for

REPLAY_COMMISSION_DEFAULT = 0.0
REPLAY_SLIPPAGE_BPS_DEFAULT = 0.0


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
    commission_per_fill: float = REPLAY_COMMISSION_DEFAULT,
    slippage_bps: float = REPLAY_SLIPPAGE_BPS_DEFAULT,
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
                        intent, spot_bar.close, active_state, position, active_limits, journal, stamp,
                        slippage_bps=slippage_bps, fees=commission_per_fill,
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
                        close, spot_bar.close, active_state, position, active_limits, journal, stamp,
                        slippage_bps=slippage_bps, fees=commission_per_fill,
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


def _slippage_total(fills: list[tuple[datetime, dict]]) -> float:
    total = 0.0
    for _, fill in fills:
        mark = fill.get("mark_price")
        price = fill.get("price")
        size = fill.get("size")
        if mark is None or price is None or size is None:
            continue
        total += abs(float(price) - float(mark)) * abs(float(size))
    return total


def _spy_benchmark(
    store: SnapshotStore,
    first_ts: datetime,
    last_ts: datetime,
    fills: list[tuple[datetime, dict]],
) -> tuple[str, dict]:
    spy_bars = store.read_bars("SPY")
    if len(spy_bars) < 2:
        return (
            "buy-and-hold SPY benchmark n/a (no stored SPY bars)"
            " -- next: say load SPY to fetch bars",
            {"spy_benchmark_pnl": None, "spy_benchmark_pct": None, "spy_benchmark_shares": 0.0},
        )
    in_span = [bar for bar in spy_bars if first_ts <= bar.timestamp_utc <= last_ts]
    if len(in_span) < 2:
        return (
            "buy-and-hold SPY benchmark n/a (fewer than 2 SPY bars inside the replay window)"
            " -- next: say load SPY to fetch bars",
            {"spy_benchmark_pnl": None, "spy_benchmark_pct": None, "spy_benchmark_shares": 0.0},
        )
    first, last = in_span[0], in_span[-1]
    if fills:
        notional = abs(float(fills[0][1]["size"])) * float(fills[0][1]["price"])
        shares = notional / first.close if first.close else 0.0
    else:
        shares = 100.0
    pnl = (last.close - first.close) * shares
    pct = (last.close - first.close) / first.close * 100.0 if first.close else 0.0
    line = (
        f"buy-and-hold SPY benchmark {signed_dollars(pnl)} ({pct:+.1f}%) over "
        f"{first.timestamp_utc.date().isoformat()} to {last.timestamp_utc.date().isoformat()} "
        f"({shares:g} shares, {first.close:.2f} to {last.close:.2f})"
    )
    return line, {
        "spy_benchmark_pnl": pnl,
        "spy_benchmark_pct": pct,
        "spy_benchmark_shares": shares,
    }


def replay_report(
    underlying: str,
    store: SnapshotStore,
    clean_days_target: int = 10,
    drawdown_bound: float = 500.0,
    commission_per_fill: float = REPLAY_COMMISSION_DEFAULT,
    slippage_bps: float = REPLAY_SLIPPAGE_BPS_DEFAULT,
) -> tuple[str, list[tuple[datetime, dict]], dict]:
    snaps = [
        (snapshot_id, snapshot)
        for snapshot_id, snapshot in store.snapshots_in_order()
        if snapshot.underlying == underlying
    ]
    if len(snaps) < 2:
        return (
            f"replay {underlying}: needs at least 2 stored snapshots (has {len(snaps)})"
            f" -- say load {underlying} to fetch more",
            [],
            {},
        )
    bars = store.read_bars(underlying)
    if not bars:
        return f"replay {underlying}: needs stored bars -- say load {underlying} to fetch bars", [], {}
    scratch = Journal()
    replay_week(
        [snapshot for _, snapshot in snaps], scratch, bars=bars,
        commission_per_fill=commission_per_fill, slippage_bps=slippage_bps,
    )
    evidence = paper_evidence_from_journal(scratch, clean_days_target, drawdown_bound)
    fills = [
        (entry.timestamp_utc, {**entry.payload, "venue": "replay"})
        for entry in scratch.entries()
        if entry.kind == "fill"
    ]
    steps = [entry for entry in scratch.entries() if entry.kind == "backtest_step"]
    last_pnl = float(steps[-1].payload.get("day_pnl", 0.0)) if steps else 0.0
    commission_total = len(fills) * commission_per_fill
    slip_total = _slippage_total(fills)
    total_costs = commission_total + slip_total
    first_ts = min(snapshot.timestamp_utc for _, snapshot in snaps)
    last_ts = max(snapshot.timestamp_utc for _, snapshot in snaps)
    benchmark_line, benchmark = _spy_benchmark(store, first_ts, last_ts, fills)
    summary = {
        "underlying": underlying,
        "snapshots": len(snaps),
        "fills": len(fills),
        "day_pnl": last_pnl,
        "clean_days": evidence.clean_days,
        "max_drawdown": evidence.max_drawdown,
        "commission_per_fill": commission_per_fill,
        "slippage_bps": slippage_bps,
        "commission_total": commission_total,
        "slippage_total": slip_total,
        "total_costs": total_costs,
        **benchmark,
    }
    days = sorted({entry.timestamp_utc.date().isoformat() for entry in scratch.entries()})
    lines = [
        f"replay {underlying}: {len(snaps)} snapshots, {len(fills)} fill{'s' if len(fills) != 1 else ''}, "
        f"day pnl {signed_dollars(last_pnl)}",
        f"{benchmark_line} vs strategy {signed_dollars(last_pnl)}",
        f"costs: {compact_dollars(total_costs)} total "
        f"({compact_dollars(commission_total)} commission, {len(fills)} fills x "
        f"{compact_dollars(commission_per_fill)}/fill, plus {compact_dollars(slip_total)} "
        f"slippage @ {slippage_bps:g} bps; defaults: commission "
        f"{compact_dollars(REPLAY_COMMISSION_DEFAULT)} per fill, slippage "
        f"{REPLAY_SLIPPAGE_BPS_DEFAULT:g} bps)",
        f"clean days {evidence.clean_days}/{evidence.clean_days_target}, "
        f"max drawdown {compact_dollars(evidence.max_drawdown)} "
        f"(bound {compact_dollars(evidence.drawdown_bound)})",
        f"span {days[0]} to {days[-1]} ({len(days)} distinct days; clean target counts distinct days)",
    ]
    if fills:
        lines.append("trade log:")
        for stamp, fill in fills:
            lines.append(
                f"  fill {stamp:%m-%d %H:%M}: {fill['side']} {fill['size']:g} "
                f"{fill['symbol']} @ {fill['price']:.2f}"
            )
    else:
        lines.append("trade log: no fills (no direction flip in span)")
    lines.append("replay holds same-side signals and only direction flips trade, so trends yield few fills")
    return "\n".join(lines), fills, summary


def replay_response(
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    clean_days_target: int = 10,
    drawdown_bound: float = 500.0,
    commission_per_fill: float = REPLAY_COMMISSION_DEFAULT,
    slippage_bps: float = REPLAY_SLIPPAGE_BPS_DEFAULT,
) -> str:
    text, fills, summary = replay_report(
        underlying, store, clean_days_target, drawdown_bound,
        commission_per_fill, slippage_bps,
    )
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
