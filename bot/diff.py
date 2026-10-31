from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from bot.exposure import compute_gex
from bot.store import SnapshotStore


def compact_dollars(value: float) -> str:
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1_000_000_000:
        return f"{sign}${magnitude / 1_000_000_000:.1f}B"
    if magnitude >= 1_000_000:
        return f"{sign}${magnitude / 1_000_000:.0f}M"
    if magnitude >= 1_000:
        return f"{sign}${magnitude / 1_000:.0f}K"
    return f"{sign}${magnitude:.0f}"


def signed_dollars(value: float) -> str:
    if value < 0:
        return compact_dollars(value)
    return f"+{compact_dollars(value)}"


@dataclass
class SnapshotChanges:
    underlying: str
    previous_id: str
    latest_id: str
    previous_ts: datetime
    latest_ts: datetime
    regime: str
    previous_regime: str
    flip: float | None
    previous_flip: float | None
    total: float
    previous_total: float
    wall_strike: float | None
    wall_value: float | None
    previous_wall_strike: float | None
    previous_wall_value: float | None
    movers: list[tuple[float, float, float, float]]


def _top_wall(exposures: dict[float, float]) -> tuple[float | None, float | None]:
    if not exposures:
        return None, None
    strike, value = max(exposures.items(), key=lambda item: abs(item[1]))
    return strike, value


def snapshot_changes(underlying: str, store: SnapshotStore) -> SnapshotChanges | None:
    snaps = [
        (snapshot_id, snapshot)
        for snapshot_id, snapshot in store.snapshots_in_order()
        if snapshot.underlying == underlying
    ]
    if len(snaps) < 2:
        return None
    (prev_id, prev), (latest_id, latest) = snaps[-2], snaps[-1]
    bar = store.read_latest_bar(underlying)
    spot = bar.close if bar is not None else None
    before = compute_gex(prev, prev_id, spot=spot)
    after = compute_gex(latest, latest_id, spot=spot)
    wall_strike, wall_value = _top_wall(after.per_strike_exposure)
    prev_wall_strike, prev_wall_value = _top_wall(before.per_strike_exposure)
    movers = []
    for strike in set(before.per_strike_exposure) | set(after.per_strike_exposure):
        old = before.per_strike_exposure.get(strike, 0.0)
        new = after.per_strike_exposure.get(strike, 0.0)
        if new != old:
            movers.append((strike, new - old, old, new))
    movers.sort(key=lambda item: -abs(item[1]))
    return SnapshotChanges(
        underlying=underlying,
        previous_id=prev_id,
        latest_id=latest_id,
        previous_ts=prev.timestamp_utc,
        latest_ts=latest.timestamp_utc,
        regime="long-gamma" if after.total_exposure >= 0.0 else "short-gamma",
        previous_regime="long-gamma" if before.total_exposure >= 0.0 else "short-gamma",
        flip=after.flip_point,
        previous_flip=before.flip_point,
        total=after.total_exposure,
        previous_total=before.total_exposure,
        wall_strike=wall_strike,
        wall_value=wall_value,
        previous_wall_strike=prev_wall_strike,
        previous_wall_value=prev_wall_value,
        movers=movers[:3],
    )


def _flip_line(changes: SnapshotChanges) -> str:
    if changes.flip is None and changes.previous_flip is None:
        return "flip: none"
    if changes.previous_flip is None:
        return f"flip appeared at {changes.flip}"
    if changes.flip is None:
        return f"flip vanished (was {changes.previous_flip})"
    if changes.flip == changes.previous_flip:
        return f"flip {changes.flip} (unchanged)"
    return f"flip {changes.previous_flip} -> {changes.flip}"


def _wall_line(changes: SnapshotChanges) -> str | None:
    if changes.wall_strike is None or changes.wall_value is None:
        return None
    if changes.wall_strike == changes.previous_wall_strike and changes.previous_wall_value:
        growth = abs(changes.wall_value) - abs(changes.previous_wall_value)
        pct = growth / abs(changes.previous_wall_value) * 100.0
        return (
            f"top wall {changes.wall_strike} {compact_dollars(changes.wall_value)} "
            f"({signed_dollars(growth)}, {pct:+.1f}%)"
        )
    return f"top wall {changes.wall_strike} {compact_dollars(changes.wall_value)}"


def snapshot_diff_text(underlying: str, store: SnapshotStore) -> str:
    changes = snapshot_changes(underlying, store)
    if changes is None:
        have = sum(1 for _, s in store.snapshots_in_order() if s.underlying == underlying)
        return f"diff {underlying}: needs at least 2 stored snapshots (has {have}) -- say load {underlying} to fetch more"
    head = (
        f"{underlying} {changes.previous_ts:%H:%M} -> {changes.latest_ts:%H:%M}:"
    )
    lines = [head]
    if changes.regime != changes.previous_regime:
        lines.append(f"regime flipped {changes.previous_regime} -> {changes.regime}")
    else:
        lines.append(f"regime {changes.regime}")
    lines.append(_flip_line(changes))
    wall = _wall_line(changes)
    if wall is not None:
        lines.append(wall)
    total_delta = changes.total - changes.previous_total
    lines.append(
        f"total {compact_dollars(changes.previous_total)} -> {compact_dollars(changes.total)} "
        f"({signed_dollars(total_delta)})"
    )
    if changes.movers:
        moves = "; ".join(f"{strike} {signed_dollars(delta)}" for strike, delta, _, _ in changes.movers)
        lines.append(f"movers: {moves}")
    return "\n".join(lines)
