from __future__ import annotations

from datetime import datetime
from typing import NewType, Optional
from urllib.parse import unquote

from bot.shapes import Bar, ChainSnapshot, OperatorProfile

SnapshotId = NewType("SnapshotId", str)


def snapshot_id_for(snapshot: ChainSnapshot) -> str:
    return f"{snapshot.underlying}:{snapshot.timestamp_utc.isoformat()}"


def parse_snapshot_id(raw: str) -> SnapshotId:
    segment = unquote(raw)
    underlying, sep, stamp = segment.partition(":")
    if not sep or not underlying or not stamp:
        raise ValueError(f"malformed snapshot id {raw!r}")
    try:
        datetime.fromisoformat(stamp)
    except ValueError:
        raise ValueError(f"malformed snapshot id {raw!r}") from None
    return SnapshotId(f"{underlying}:{stamp}")


class SnapshotStore:
    def __init__(self) -> None:
        self._snapshots: dict[str, ChainSnapshot] = {}
        self._order: list[str] = []
        self._latest: dict[str, str] = {}
        self._bars: dict[str, list[Bar]] = {}
        self._bar_keys: dict[str, set[tuple[str, datetime]]] = {}
        self._profile: Optional[OperatorProfile] = None
        self._iv: dict[str, dict[str, float]] = {}

    def save(self, snapshot: ChainSnapshot) -> str:
        snapshot_id = snapshot_id_for(snapshot)
        if snapshot_id not in self._snapshots:
            self._order.append(snapshot_id)
        self._snapshots[snapshot_id] = snapshot
        self._latest[snapshot.underlying] = snapshot_id
        return snapshot_id

    def read_snapshot(self, snapshot_id: str) -> Optional[ChainSnapshot]:
        return self._snapshots.get(snapshot_id)

    def list_underlyings(self) -> list[str]:
        return sorted(self._latest)

    def read_latest(self, underlying: str) -> Optional[tuple[str, ChainSnapshot]]:
        snapshot_id = self._latest.get(underlying)
        if snapshot_id is None:
            return None
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None:
            return None
        return snapshot_id, snapshot

    def read_previous(self, underlying: str, snapshot_id: str) -> Optional[tuple[str, ChainSnapshot]]:
        previous: Optional[str] = None
        for candidate in self._order:
            snapshot = self._snapshots.get(candidate)
            if snapshot is None or snapshot.underlying != underlying:
                continue
            if candidate == snapshot_id:
                break
            previous = candidate
        if previous is None:
            return None
        found = self._snapshots.get(previous)
        if found is None:
            return None
        return previous, found

    def save_bar(self, underlying: str, bar: Bar) -> bool:
        key = (bar.interval, bar.timestamp_utc)
        known = self._bar_keys.setdefault(underlying, set())
        if key in known:
            return False
        known.add(key)
        self._bars.setdefault(underlying, []).append(bar)
        return True

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

    def save_iv(self, symbol: str, day_iso: str, iv: float, keep_per_symbol: int = 600) -> None:
        days = self._iv.setdefault(symbol, {})
        days[day_iso] = iv
        if len(days) > keep_per_symbol:
            for old in sorted(days)[: len(days) - keep_per_symbol]:
                del days[old]

    def read_iv_history(self, symbol: str) -> dict[str, float]:
        return dict(self._iv.get(symbol, {}))

    def all_iv(self) -> dict[str, dict[str, float]]:
        return {symbol: dict(days) for symbol, days in self._iv.items()}

    def prune(self, keep_per_underlying: int = 500) -> int:
        keep: set[str] = set()
        counts: dict[str, int] = {}
        for snapshot_id in reversed(self._order):
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None:
                continue
            seen = counts.get(snapshot.underlying, 0)
            if seen >= keep_per_underlying:
                continue
            counts[snapshot.underlying] = seen + 1
            keep.add(snapshot_id)
        dropped = [sid for sid in self._order if sid not in keep]
        for snapshot_id in dropped:
            self._snapshots.pop(snapshot_id, None)
        self._order = [sid for sid in self._order if sid in keep]
        self._latest = {}
        for snapshot_id in self._order:
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is not None:
                self._latest[snapshot.underlying] = snapshot_id
        return len(dropped)

    def restore(self, other: SnapshotStore) -> None:
        self._snapshots = dict(other._snapshots)
        self._order = list(other._order)
        self._latest = dict(other._latest)
        self._bars = {underlying: list(bars) for underlying, bars in other._bars.items()}
        self._bar_keys = {underlying: set(keys) for underlying, keys in other._bar_keys.items()}
        self._profile = other._profile
        self._iv = {symbol: dict(days) for symbol, days in other._iv.items()}

    def merge_from(self, other: SnapshotStore) -> None:
        for snapshot_id in other._order:
            snapshot = other._snapshots.get(snapshot_id)
            if snapshot is None:
                continue
            if snapshot_id not in self._snapshots:
                self._snapshots[snapshot_id] = snapshot
                self._order.append(snapshot_id)
        for underlying, other_latest_id in other._latest.items():
            other_latest = other._snapshots.get(other_latest_id)
            if other_latest is None:
                continue
            current_id = self._latest.get(underlying)
            current = self._snapshots.get(current_id) if current_id is not None else None
            if current is None or other_latest.timestamp_utc > current.timestamp_utc:
                self._latest[underlying] = other_latest_id
        for underlying, bars in other._bars.items():
            for bar in bars:
                self.save_bar(underlying, bar)
        for symbol, days in other._iv.items():
            for day_iso, iv in days.items():
                self.save_iv(symbol, day_iso, iv)
