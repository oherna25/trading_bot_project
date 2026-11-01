from __future__ import annotations

import csv
import io
import json
from dataclasses import replace
from datetime import datetime
from typing import Any, Optional, Sequence

from bot.diff import compact_dollars, signed_dollars
from bot.exposure import compute_gex
from bot.journal import Journal
from bot.research import analyst_tool, derive_display
from bot.risk import IntentRejectedError, build_intent, fire_halt, submit_paper
from bot.shapes import Bar, ChainSnapshot, OrderIntent, PaperEvidence, Position, RiskLimits, RiskState
from bot.store import SnapshotStore, snapshot_id_for

REPLAY_COMMISSION_DEFAULT = 0.0
REPLAY_SLIPPAGE_BPS_DEFAULT = 0.0


def _bar_at(bars: Sequence[Bar], moment: datetime) -> Optional[Bar]:
    known = [bar for bar in bars if bar.timestamp_utc <= moment]
    if not known:
        return None
    return max(known, key=lambda b: b.timestamp_utc)


def default_replay_run_id(
    underlying: str,
    first_ts: datetime,
    last_ts: datetime,
    snapshots: int,
    commission_per_fill: float = REPLAY_COMMISSION_DEFAULT,
    slippage_bps: float = REPLAY_SLIPPAGE_BPS_DEFAULT,
) -> str:
    first = first_ts.strftime("%Y%m%dT%H%M%S")
    last = last_ts.strftime("%Y%m%dT%H%M%S")
    return (
        f"replay-{underlying}-{first}-{last}-{snapshots}s-"
        f"{commission_per_fill:g}c-{slippage_bps:g}s"
    )


def _tag_fill(journal: Journal, run_id: str) -> None:
    for entry in reversed(journal.entries()):
        if entry.kind == "fill" and "run_id" not in entry.payload:
            entry.payload["run_id"] = run_id
            return


def replay_week(
    snapshot_list: list[ChainSnapshot],
    journal: Journal,
    bars: Optional[Sequence[Bar]] = None,
    state: Optional[RiskState] = None,
    limits: Optional[RiskLimits] = None,
    commission_per_fill: float = REPLAY_COMMISSION_DEFAULT,
    slippage_bps: float = REPLAY_SLIPPAGE_BPS_DEFAULT,
    run_id: Optional[str] = None,
) -> Journal:
    ordered = sorted(snapshot_list, key=lambda s: s.timestamp_utc)
    if run_id is None and ordered:
        run_id = default_replay_run_id(
            ordered[0].underlying,
            ordered[0].timestamp_utc,
            ordered[-1].timestamp_utc,
            len(ordered),
            commission_per_fill,
            slippage_bps,
        )
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
    base_equity = active_state.account_size + active_state.day_pnl
    total_pnl = 0.0
    position: Optional[Position] = None
    pending: Optional[tuple[str, OrderIntent, datetime, str]] = None
    current_day: Optional[str] = None
    last_bar: Optional[Bar] = None
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
        if last_bar is None or spot_bar.timestamp_utc > last_bar.timestamp_utc:
            last_bar = spot_bar
        stamp = snapshot.timestamp_utc
        day = stamp.date().isoformat()
        if current_day is None:
            current_day = day
        elif day != current_day:
            current_day = day
            active_state = replace(active_state, day_pnl=0.0, halt_active=False)
            journal.append(stamp, "replay_roll", {"day": day, "run_id": run_id})
        action = "flat"
        if pending is not None and spot_bar.timestamp_utc > pending[2]:
            kind, working, _, _ = pending
            pending = None
            prev = active_state.day_pnl
            try:
                _, position, active_state = submit_paper(
                    working, spot_bar.open, active_state, position, active_limits, journal, stamp,
                    slippage_bps=slippage_bps, fees=commission_per_fill,
                )
                if run_id is not None:
                    _tag_fill(journal, run_id)
                action = kind
            except IntentRejectedError:
                action = "vetoed"
            total_pnl += active_state.day_pnl - prev
        if position is not None and position.net_size != 0:
            marked = (spot_bar.close - position.average_price) * position.net_size
            drift = marked - position.unrealized_pnl
            total_pnl += drift
            active_state = replace(active_state, day_pnl=active_state.day_pnl + drift)
            position = replace(position, unrealized_pnl=marked)
            if active_state.day_pnl <= -active_limits.max_day_loss and not active_state.halt_active:
                active_state = fire_halt(active_state)
                journal.append(stamp, "halt", {"day_pnl": active_state.day_pnl})
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
        if intent is not None and pending is None and action != "vetoed":
            want = 1.0 if signal.direction == "long" else -1.0
            held = 0.0 if position is None else position.net_size
            if held == 0:
                pending = ("open", intent, spot_bar.timestamp_utc, snapshot_id)
                if action == "flat":
                    action = "pending"
            elif (held > 0) == (want > 0):
                journal.append(
                    stamp, "replay_hold", {"snapshot_id": snapshot_id, "direction": signal.direction}
                )
                if action == "flat":
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
                pending = ("close", close, spot_bar.timestamp_utc, snapshot_id)
                if action == "flat":
                    action = "pending"
        journal.append(
            snapshot.timestamp_utc,
            "backtest_step",
            {
                "snapshot_id": snapshot_id,
                "direction": signal.direction,
                "day_pnl": active_state.day_pnl,
                "total_pnl": total_pnl,
                "equity": base_equity + total_pnl,
                "action": action,
                "run_id": run_id,
            },
        )
    if last_bar is not None:
        end = last_bar.timestamp_utc
        if pending is not None:
            journal.append(
                end, "replay_expire",
                {"snapshot_id": pending[3], "action": pending[0], "run_id": run_id},
            )
            pending = None
        if position is not None and position.net_size != 0:
            held = position.net_size
            force = OrderIntent(
                symbol=position.symbol,
                side="sell" if held > 0 else "buy",
                size=abs(held),
                risk_numbers={"size": abs(held), "close": True, "force": True},
                signal_id=f"replay-force-{run_id}",
                legs=(),
            )
            journal.append(
                end, "intent_proposed", {"signal_id": force.signal_id, "size": force.size, "legs": []}
            )
            prev = active_state.day_pnl
            try:
                _, position, active_state = submit_paper(
                    force, last_bar.close, active_state, position, active_limits, journal, end,
                    slippage_bps=slippage_bps, fees=commission_per_fill,
                )
                if run_id is not None:
                    _tag_fill(journal, run_id)
                total_pnl += active_state.day_pnl - prev
                journal.append(
                    end,
                    "backtest_step",
                    {
                        "snapshot_id": snapshot_id_for(ordered[-1]),
                        "direction": "flat",
                        "day_pnl": active_state.day_pnl,
                        "total_pnl": total_pnl,
                        "equity": base_equity + total_pnl,
                        "action": "force-close",
                        "run_id": run_id,
                    },
                )
            except IntentRejectedError:
                journal.append(end, "replay_unclosed", {"reason": "force close vetoed", "run_id": run_id})
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


def _benchmark_shares(first_close: float, fills: list[tuple[datetime, dict]]) -> float:
    if fills:
        notional = abs(float(fills[0][1]["size"])) * float(fills[0][1]["price"])
        return notional / first_close if first_close else 0.0
    return 100.0


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
    shares = _benchmark_shares(first.close, fills)
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


def _replay_scratch(
    underlying: str,
    store: SnapshotStore,
    commission_per_fill: float = REPLAY_COMMISSION_DEFAULT,
    slippage_bps: float = REPLAY_SLIPPAGE_BPS_DEFAULT,
    run_id: Optional[str] = None,
) -> tuple[list, list, Journal, str, datetime, datetime] | str:
    snaps = [
        (snapshot_id, snapshot)
        for snapshot_id, snapshot in store.snapshots_in_order()
        if snapshot.underlying == underlying
    ]
    if len(snaps) < 2:
        return (
            f"replay {underlying}: needs at least 2 stored snapshots (has {len(snaps)})"
            f" -- say load {underlying} to fetch more"
        )
    bars = store.read_bars(underlying)
    if not bars:
        return f"replay {underlying}: needs stored bars -- say load {underlying} to fetch bars"
    first_ts = min(snapshot.timestamp_utc for _, snapshot in snaps)
    last_ts = max(snapshot.timestamp_utc for _, snapshot in snaps)
    if run_id is None:
        run_id = default_replay_run_id(
            underlying, first_ts, last_ts, len(snaps), commission_per_fill, slippage_bps
        )
    scratch = Journal()
    replay_week(
        [snapshot for _, snapshot in snaps], scratch, bars=bars,
        commission_per_fill=commission_per_fill, slippage_bps=slippage_bps, run_id=run_id,
    )
    return snaps, bars, scratch, run_id, first_ts, last_ts


def replay_report(
    underlying: str,
    store: SnapshotStore,
    clean_days_target: int = 10,
    drawdown_bound: float = 500.0,
    commission_per_fill: float = REPLAY_COMMISSION_DEFAULT,
    slippage_bps: float = REPLAY_SLIPPAGE_BPS_DEFAULT,
    run_id: Optional[str] = None,
) -> tuple[str, list[tuple[datetime, dict]], dict]:
    inputs = _replay_scratch(underlying, store, commission_per_fill, slippage_bps, run_id)
    if isinstance(inputs, str):
        return inputs, [], {}
    snaps, bars, scratch, run_id, first_ts, last_ts = inputs
    evidence = paper_evidence_from_journal(scratch, clean_days_target, drawdown_bound)
    fills = [
        (entry.timestamp_utc, {**entry.payload, "venue": "replay"})
        for entry in scratch.entries()
        if entry.kind == "fill"
    ]
    steps = [entry for entry in scratch.entries() if entry.kind == "backtest_step"]
    if steps:
        last_pnl = float(steps[-1].payload.get("total_pnl", steps[-1].payload.get("day_pnl", 0.0)))
        last_equity: Optional[float] = steps[-1].payload.get("equity")
    else:
        last_pnl, last_equity = 0.0, None
    daily_deltas: dict[str, float] = {}
    for entry in steps:
        daily_deltas[entry.timestamp_utc.date().isoformat()] = float(entry.payload.get("day_pnl", 0.0))
    commission_total = len(fills) * commission_per_fill
    slip_total = _slippage_total(fills)
    total_costs = commission_total + slip_total
    benchmark_line, benchmark = _spy_benchmark(store, first_ts, last_ts, fills)
    summary = {
        "underlying": underlying,
        "snapshots": len(snaps),
        "fills": len(fills),
        "day_pnl": last_pnl,
        "equity": last_equity,
        "daily_deltas": daily_deltas,
        "run_id": run_id,
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
    day_bits = ", ".join(f"{day} {signed_dollars(daily_deltas[day])}" for day in sorted(daily_deltas))
    lines = [
        f"replay {underlying}: {len(snaps)} snapshots, {len(fills)} fill{'s' if len(fills) != 1 else ''}, "
        f"total pnl {signed_dollars(last_pnl)}",
        f"equity by day: {day_bits}",
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
    lines.append(
        "replay holds same-side signals and only direction flips trade, so trends yield few fills; "
        "fills print on the next bar and open positions force-close at the end"
    )
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
    run_id: Optional[str] = None,
) -> str:
    text, fills, summary = replay_report(
        underlying, store, clean_days_target, drawdown_bound,
        commission_per_fill, slippage_bps, run_id,
    )
    if not summary:
        return text
    if any(
        entry.kind == "replay_summary" and entry.payload.get("run_id") == summary["run_id"]
        for entry in journal.entries()
    ):
        return text + f"\nreplay run {summary['run_id']} already journaled; fills not duplicated"
    for stamp, payload in fills:
        journal.append(stamp, "fill", payload)
    journal.append(now, "replay_summary", summary)
    return text


REPLAY_EXPORT_SECTIONS = ("steps", "fills", "benchmark")

STEP_COLUMNS = (
    "timestamp_utc", "snapshot_id", "direction", "action", "day_pnl", "total_pnl", "equity", "run_id",
)
FILL_COLUMNS = (
    "timestamp_utc", "symbol", "side", "price", "size", "mark_price", "venue", "legs", "run_id",
)
BENCHMARK_COLUMNS = (
    "timestamp_utc", "date", "spy_close", "spy_equity", "strategy_equity", "strategy_day_pnl", "run_id",
)


def _frames_to_csv(columns: Sequence[str], rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in columns})
    return buffer.getvalue()


def replay_export(
    underlying: str,
    store: SnapshotStore,
    section: str = "steps",
    commission_per_fill: float = REPLAY_COMMISSION_DEFAULT,
    slippage_bps: float = REPLAY_SLIPPAGE_BPS_DEFAULT,
    run_id: Optional[str] = None,
) -> str:
    if section not in REPLAY_EXPORT_SECTIONS:
        raise ValueError(
            f"unknown replay section {section} -- want one of {', '.join(REPLAY_EXPORT_SECTIONS)}"
        )
    inputs = _replay_scratch(underlying, store, commission_per_fill, slippage_bps, run_id)
    if isinstance(inputs, str):
        raise ValueError(inputs)
    _, _, scratch, _, first_ts, last_ts = inputs
    if section == "steps":
        rows = [
            {
                "timestamp_utc": entry.timestamp_utc.isoformat(),
                "snapshot_id": entry.payload.get("snapshot_id", ""),
                "direction": entry.payload.get("direction", ""),
                "action": entry.payload.get("action", ""),
                "day_pnl": entry.payload.get("day_pnl", ""),
                "total_pnl": entry.payload.get("total_pnl", ""),
                "equity": entry.payload.get("equity", ""),
                "run_id": entry.payload.get("run_id", ""),
            }
            for entry in scratch.entries()
            if entry.kind == "backtest_step"
        ]
        return _frames_to_csv(STEP_COLUMNS, rows)
    fills = [
        (entry.timestamp_utc, entry.payload)
        for entry in scratch.entries()
        if entry.kind == "fill"
    ]
    if section == "fills":
        rows = [
            {
                "timestamp_utc": stamp.isoformat(),
                "symbol": payload.get("symbol", ""),
                "side": payload.get("side", ""),
                "price": payload.get("price", ""),
                "size": payload.get("size", ""),
                "mark_price": payload.get("mark_price", ""),
                "venue": "replay",
                "legs": json.dumps(payload.get("legs", [])),
                "run_id": payload.get("run_id", ""),
            }
            for stamp, payload in fills
        ]
        return _frames_to_csv(FILL_COLUMNS, rows)
    in_span = sorted(
        (bar for bar in store.read_bars("SPY") if first_ts <= bar.timestamp_utc <= last_ts),
        key=lambda bar: bar.timestamp_utc,
    )
    spy_by_day = {bar.timestamp_utc.date().isoformat(): bar.close for bar in in_span}
    shares = _benchmark_shares(in_span[0].close, fills) if in_span else 0.0
    rows = []
    for entry in scratch.entries():
        if entry.kind != "backtest_step":
            continue
        day = entry.timestamp_utc.date().isoformat()
        spy_close = spy_by_day.get(day)
        rows.append(
            {
                "timestamp_utc": entry.timestamp_utc.isoformat(),
                "date": day,
                "spy_close": spy_close if spy_close is not None else "",
                "spy_equity": shares * spy_close if spy_close is not None else "",
                "strategy_equity": entry.payload.get("equity", ""),
                "strategy_day_pnl": entry.payload.get("day_pnl", ""),
                "run_id": entry.payload.get("run_id", ""),
            }
        )
    return _frames_to_csv(BENCHMARK_COLUMNS, rows)


def paper_evidence_from_journal(
    journal: Journal,
    clean_days_target: int = 10,
    drawdown_bound: float = 500.0,
    halt_drills_passed: bool = False,
) -> PaperEvidence:
    by_day: dict[str, float] = {}
    equity_by_day: dict[str, float] = {}
    halted_days: set[str] = set()
    for entry in journal.entries():
        day = entry.timestamp_utc.date().isoformat()
        if entry.kind == "backtest_step":
            by_day[day] = float(entry.payload.get("day_pnl", 0.0))
            equity_by_day[day] = float(entry.payload.get("equity", entry.payload.get("day_pnl", 0.0)))
        elif entry.kind == "halt":
            halted_days.add(day)
    clean_days = sum(1 for day, pnl in by_day.items() if pnl >= 0 and day not in halted_days)
    peak = 0.0
    max_drawdown = 0.0
    for day in sorted(equity_by_day):
        peak = max(peak, equity_by_day[day])
        max_drawdown = max(max_drawdown, peak - equity_by_day[day])
    return PaperEvidence(
        clean_days=clean_days,
        clean_days_target=clean_days_target,
        max_drawdown=max_drawdown,
        drawdown_bound=drawdown_bound,
        halt_drills_passed=halt_drills_passed,
    )
