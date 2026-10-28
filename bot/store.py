from __future__ import annotations

from typing import Optional

from bot.shapes import Bar, ChainSnapshot, OperatorProfile


def snapshot_id_for(snapshot: ChainSnapshot) -> str:
    return f"{snapshot.underlying}:{snapshot.timestamp_utc.isoformat()}"


class SnapshotStore:
    def __init__(self) -> None:
        self._snapshots: dict[str, ChainSnapshot] = {}
        self._order: list[str] = []
        self._bars: dict[str, list[Bar]] = {}
        self._profile: Optional[OperatorProfile] = None

    def save(self, snapshot: ChainSnapshot) -> str:
        snapshot_id = snapshot_id_for(snapshot)
        self._snapshots[snapshot_id] = snapshot
        self._order.append(snapshot_id)
        return snapshot_id

    def read_snapshot(self, snapshot_id: str) -> Optional[ChainSnapshot]:
        return self._snapshots.get(snapshot_id)

    def list_underlyings(self) -> list[str]:
        seen: list[str] = []
        for snapshot_id in self._order:
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is not None and snapshot.underlying not in seen:
                seen.append(snapshot.underlying)
        return seen

    def read_latest(self, underlying: str) -> Optional[tuple[str, ChainSnapshot]]:
        for snapshot_id in reversed(self._order):
            if self._snapshots[snapshot_id].underlying == underlying:
                return snapshot_id, self._snapshots[snapshot_id]
        return None

    def save_bar(self, underlying: str, bar: Bar) -> None:
        self._bars.setdefault(underlying, []).append(bar)

    def read_latest_bar(self, underlying: str) -> Optional[Bar]:
        bars = self._bars.get(underlying, [])
        if not bars:
            return None
        return max(bars, key=lambda b: b.timestamp_utc)

    def read_bars(self, underlying: str) -> list[Bar]:
        return sorted(self._bars.get(underlying, []), key=lambda b: b.timestamp_utc)

    def snapshots_in_order(self) -> list[tuple[str, ChainSnapshot]]:
        return [(snapshot_id, self._snapshots[snapshot_id]) for snapshot_id in self._order]

    def all_bars(self) -> dict[str, list[Bar]]:
        return {underlying: self.read_bars(underlying) for underlying in self._bars}

    def save_profile(self, profile: OperatorProfile) -> None:
        self._profile = profile

    def read_profile(self) -> Optional[OperatorProfile]:
        return self._profile
