"""User-supplied strategy parsing plus data-backed feedback.

Parses a plain-English strategy into a StrategySpec, then backtests it
over stored snapshots and bars by injecting a custom signal_fn into
replay_week. Feedback cites fills, pnl, benchmark, and concrete
tighten/loosen suggestions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from bot.ideas import candidate_symbols, parse_timeframe, spread_pct
from bot.journal import Journal
from bot.replay import (
    _spy_benchmark,
    default_replay_run_id,
    paper_evidence_from_journal,
    replay_week,
)
from bot.shapes import Bar, ChainSnapshot, GexProfile, Signal
from bot.store import SnapshotStore

STRATEGY_RE = re.compile(
    r"\b(my strategy|my play|my setup|strategy:|backtest my|evaluate my|test my)\b",
    re.IGNORECASE,
)

LONG_RE = re.compile(r"\b(long|bullish|bull|buy\s*calls?|calls?)\b", re.IGNORECASE)
SHORT_RE = re.compile(r"\b(short|bearish|bear|buy\s*puts?|puts?)\b", re.IGNORECASE)
DTE_RANGE_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*(dte|dtes|days?|d)\b", re.IGNORECASE)
DTE_SINGLE_RE = re.compile(r"(\d+)\s*(dte|dtes)\b", re.IGNORECASE)
OI_RE = re.compile(r"\boi\s*(>=?|over|above|at least|>|min)?\s*(\d[\d,]*)\b", re.IGNORECASE)
SPREAD_RE = re.compile(r"spread\s*(<=?|<|under|below|max)?\s*(\d+(?:\.\d+)?)\s*%?", re.IGNORECASE)


@dataclass(frozen=True)
class StrategySpec:
    underlying: str
    direction: str  # long, short, or both
    dte_min: int
    dte_max: int
    min_oi: float
    max_spread_pct: float
    use_flip: bool
    label: str


def is_strategy_request(text: str) -> bool:
    return bool(STRATEGY_RE.search(text or ""))


def parse_strategy(text: str, default_underlying: str, store: SnapshotStore) -> StrategySpec:
    lowered = text or ""
    cands = candidate_symbols(lowered)
    stored = set(store.list_underlyings())
    underlying = default_underlying
    for cand in cands:
        if cand in stored:
            underlying = cand
            break
    else:
        if cands:
            underlying = cands[0]
    has_long = bool(LONG_RE.search(lowered))
    has_short = bool(SHORT_RE.search(lowered))
    if has_long and not has_short:
        direction = "long"
    elif has_short and not has_long:
        direction = "short"
    else:
        direction = "both"
    dte_min, dte_max = 1, 30
    maybe_max = parse_timeframe(lowered)
    if maybe_max is not None:
        dte_max = maybe_max
    ranged = DTE_RANGE_RE.search(lowered)
    if ranged:
        dte_min = max(1, int(ranged.group(1)))
        dte_max = max(dte_min, int(ranged.group(2)))
    else:
        single = DTE_SINGLE_RE.search(lowered)
        if single:
            dte_max = max(1, int(single.group(1)))
    min_oi = 100.0
    oi_hit = OI_RE.search(lowered)
    if oi_hit:
        try:
            min_oi = max(1.0, float(oi_hit.group(2).replace(",", "")))
        except ValueError:
            min_oi = 100.0
    max_spread = 10.0
    spread_hit = SPREAD_RE.search(lowered)
    if spread_hit:
        try:
            max_spread = max(1.0, min(50.0, float(spread_hit.group(2))))
        except ValueError:
            max_spread = 10.0
    use_flip = "flip" in lowered.lower()
    label = " ".join(lowered.strip().split())[:160] or "custom strategy"
    return StrategySpec(
        underlying=underlying,
        direction=direction,
        dte_min=dte_min,
        dte_max=dte_max,
        min_oi=min_oi,
        max_spread_pct=max_spread,
        use_flip=use_flip,
        label=label,
    )


def _days_to_expiry(expiry: str, ref: datetime) -> Optional[int]:
    try:
        from datetime import datetime as _dt

        parsed = _dt.fromisoformat(expiry)
    except ValueError:
        return None
    return (parsed.date() - ref.date()).days


def _passes_filters(snapshot: ChainSnapshot, spec: StrategySpec, ref: datetime) -> bool:
    for contract in snapshot.contracts_list:
        if contract.gamma is None or contract.open_interest < spec.min_oi:
            continue
        pct = spread_pct(contract)
        if pct is not None and pct > spec.max_spread_pct:
            continue
        days = _days_to_expiry(contract.expiry, ref)
        if days is None or days < spec.dte_min or days > spec.dte_max:
            continue
        return True
    return False


def strategy_signal_fn(spec: StrategySpec):
    from bot.ideas import resolve_direction

    def _fn(snapshot: ChainSnapshot, profile: GexProfile, bar: Bar | None) -> Signal | None:
        from bot.store import snapshot_id_for

        snapshot_id = snapshot_id_for(snapshot)
        if not _passes_filters(snapshot, spec, snapshot.timestamp_utc):
            return Signal(
                direction="flat", thesis="no contract passes strategy filters",
                confidence=0.1, snapshot_id=snapshot_id, symbol=snapshot.underlying,
            )
        if spec.use_flip or spec.direction == "both":
            regime_dir = resolve_direction(bar, profile.flip_point, profile.total_exposure)
            if spec.direction != "both" and regime_dir != spec.direction:
                return Signal(
                    direction="flat",
                    thesis=f"flip/regime says {regime_dir}, strategy wants {spec.direction}",
                    confidence=0.2, snapshot_id=snapshot_id, symbol=snapshot.underlying,
                )
            direction = regime_dir
        else:
            direction = spec.direction
        if direction == "flat":
            return Signal(
                direction="flat", thesis="flat regime", confidence=0.1,
                snapshot_id=snapshot_id, symbol=snapshot.underlying,
            )
        return Signal(
            direction=direction,
            thesis=(
                f"{spec.label}: {direction} {snapshot.underlying} "
                f"DTE {spec.dte_min}-{spec.dte_max} OI>={spec.min_oi:g} "
                f"spread<={spec.max_spread_pct:g}%"
            ),
            confidence=0.6, snapshot_id=snapshot_id, symbol=snapshot.underlying,
        )

    return _fn


def evaluate_strategy(
    spec: StrategySpec,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
) -> str:
    snaps = [
        (snapshot_id, snapshot)
        for snapshot_id, snapshot in store.snapshots_in_order()
        if snapshot.underlying == spec.underlying
    ]
    if len(snaps) < 2:
        return (
            f"strategy {spec.label}: needs at least 2 stored snapshots for "
            f"{spec.underlying} (has {len(snaps)}) -- say load {spec.underlying} to fetch more"
        )
    bars = store.read_bars(spec.underlying)
    if not bars:
        return (
            f"strategy {spec.label}: needs stored bars for {spec.underlying} "
            f"-- say load {spec.underlying} to fetch bars"
        )
    first_ts = min(snapshot.timestamp_utc for _, snapshot in snaps)
    last_ts = max(snapshot.timestamp_utc for _, snapshot in snaps)
    run_id = default_replay_run_id(
        spec.underlying, first_ts, last_ts, len(snaps),
    ) + "-strategy"
    scratch = Journal()
    replay_week(
        [snapshot for _, snapshot in snaps], scratch, bars=bars,
        run_id=run_id, signal_fn=strategy_signal_fn(spec),
    )
    fills = [
        (entry.timestamp_utc, dict(entry.payload))
        for entry in scratch.entries()
        if entry.kind == "fill"
    ]
    steps = [entry for entry in scratch.entries() if entry.kind == "backtest_step"]
    total_pnl = float(steps[-1].payload.get("total_pnl", 0.0)) if steps else 0.0
    evidence = paper_evidence_from_journal(scratch)
    benchmark_line, _ = _spy_benchmark(store, first_ts, last_ts, fills)
    header = (
        f"strategy feedback {spec.underlying} {spec.direction} "
        f"DTE {spec.dte_min}-{spec.dte_max} OI>={spec.min_oi:g} "
        f"spread<={spec.max_spread_pct:g}% flip={'on' if spec.use_flip else 'off'}: "
        f"{len(snaps)} snapshots, {len(fills)} fills, pnl {total_pnl:+.0f}"
    )
    lines = [header, benchmark_line]
    if not fills:
        lines.append(
            "no fills: no snapshot passed your filters on a tradable bar. "
            "next: widen DTE (try 1-60), lower OI (try 50), or raise spread cap (try 15%)"
        )
    else:
        wins = sum(1 for entry in steps if float(entry.payload.get("day_pnl", 0.0)) > 0)
        lines.append(
            f"clean days {evidence.clean_days}/{evidence.clean_days_target}, "
            f"max drawdown {evidence.max_drawdown:.0f}"
        )
        if total_pnl <= 0:
            lines.append(
                "negative or flat: consider flip filter on (add 'flip' to the strategy), "
                "avoid earnings weeks, or tighten spread to 5% for liquidity"
            )
        else:
            lines.append(
                f"positive over {len(steps)} steps with {wins} up days: "
                "consider sizing via trade cards and keeping the same DTE/OI guardrails"
            )
        lines.append("trade log:")
        for stamp, fill in fills[:20]:
            lines.append(
                f"  fill {stamp:%m-%d %H:%M}: {fill.get('side')} {fill.get('size', 0):g} "
                f"{fill.get('symbol')} @ {float(fill.get('price', 0)):,.2f}"
            )
        if len(fills) > 20:
            lines.append(f"  ... plus {len(fills) - 20} more fills")
    journal.append(
        now, "strategy_evaluated",
        {
            "underlying": spec.underlying, "direction": spec.direction,
            "dte_min": spec.dte_min, "dte_max": spec.dte_max,
            "min_oi": spec.min_oi, "max_spread_pct": spec.max_spread_pct,
            "use_flip": spec.use_flip, "fills": len(fills),
            "total_pnl": total_pnl, "run_id": run_id,
        },
    )
    return "\n".join(lines)
