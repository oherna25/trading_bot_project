from __future__ import annotations

import bisect
from datetime import date, datetime
from typing import NewType, Optional
from urllib.parse import unquote

from bot.shapes import Bar, ChainSnapshot, DashboardSettings, Holding, OperatorProfile

SnapshotId = NewType("SnapshotId", str)


def snapshot_id_for(snapshot: ChainSnapshot) -> str:
    return f"{snapshot.underlying}:{snapshot.timestamp_utc.isoformat()}"


def need_snapshot_text(underlying: str, store: SnapshotStore | None = None) -> str:
    if store is None:
        available: list[str] = []
    else:
        available = sorted(store.list_underlyings())
    have = ", ".join(available) if available else "none"
    return (
        f"no snapshot stored for {underlying} (store has: {have}) -- "
        f"say load {underlying} and I'll fetch it"
    )


def empty_store_text() -> str:
    return "no snapshots stored -- say load SPY and I'll fetch one"


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
        self._by_underlying: dict[str, list[str]] = {}
        self._latest: dict[str, str] = {}
        self._bars: dict[str, list[Bar]] = {}
        self._bar_keys: dict[str, set[tuple[str, datetime]]] = {}
        self._profile: Optional[OperatorProfile] = None
        self._iv: dict[str, dict[str, float]] = {}
        self._holdings: list[Holding] = []
        self._settings: DashboardSettings = DashboardSettings()
        self._earnings: dict[str, str] = {}
        self._alert_rules: dict[str, dict] = {}

    def _stamp(self, snapshot_id: str) -> datetime:
        return self._snapshots[snapshot_id].timestamp_utc

    def save(self, snapshot: ChainSnapshot) -> str:
        snapshot_id = snapshot_id_for(snapshot)
        is_new = snapshot_id not in self._snapshots
        self._snapshots[snapshot_id] = snapshot
        if is_new:
            bisect.insort(self._order, snapshot_id, key=self._stamp)
            bisect.insort(
                self._by_underlying.setdefault(snapshot.underlying, []),
                snapshot_id,
                key=self._stamp,
            )
        current_id = self._latest.get(snapshot.underlying)
        current = self._snapshots.get(current_id) if current_id is not None else None
        if current is None or snapshot.timestamp_utc >= current.timestamp_utc:
            self._latest[snapshot.underlying] = snapshot_id
        return snapshot_id

    def _rebuild_indexes(self) -> None:
        self._order = sorted(self._snapshots, key=lambda sid: (self._stamp(sid), sid))
        self._by_underlying = {}
        self._latest = {}
        for snapshot_id in self._order:
            snapshot = self._snapshots[snapshot_id]
            self._by_underlying.setdefault(snapshot.underlying, []).append(snapshot_id)
            self._latest[snapshot.underlying] = snapshot_id
        for bars in self._bars.values():
            bars.sort(key=lambda bar: bar.timestamp_utc)

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
        target = self._snapshots.get(snapshot_id)
        if target is None or target.underlying != underlying:
            return None
        ids = self._by_underlying.get(underlying, [])
        index = bisect.bisect_left(ids, target.timestamp_utc, key=self._stamp)
        if index >= len(ids) or ids[index] != snapshot_id or index == 0:
            return None
        previous = ids[index - 1]
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
        bisect.insort(
            self._bars.setdefault(underlying, []), bar, key=lambda b: b.timestamp_utc
        )
        return True

    def read_latest_bar(self, underlying: str) -> Optional[Bar]:
        bars = self._bars.get(underlying, [])
        if not bars:
            return None
        return bars[-1]

    def read_bars(self, underlying: str) -> list[Bar]:
        return list(self._bars.get(underlying, []))

    def snapshots_in_order(self) -> list[tuple[str, ChainSnapshot]]:
        return [(snapshot_id, self._snapshots[snapshot_id]) for snapshot_id in self._order]

    def all_bars(self) -> dict[str, list[Bar]]:
        return {underlying: self.read_bars(underlying) for underlying in self._bars}

    def save_profile(self, profile: OperatorProfile) -> None:
        self._profile = profile

    def read_profile(self) -> Optional[OperatorProfile]:
        return self._profile

    def save_holdings(self, holdings: list[Holding]) -> None:
        self._holdings = list(holdings)

    def read_holdings(self) -> list[Holding]:
        return list(self._holdings)

    def save_settings(self, settings: DashboardSettings) -> None:
        self._settings = settings

    def read_settings(self) -> DashboardSettings:
        return self._settings

    def save_earnings(self, symbol: str, day_iso: str) -> None:
        self._earnings[str(symbol).strip().upper()] = date.fromisoformat(day_iso).isoformat()

    def read_earnings(self, symbol: str) -> Optional[date]:
        raw = self._earnings.get(str(symbol).strip().upper())
        return date.fromisoformat(raw) if raw is not None else None

    def all_earnings(self) -> dict[str, date]:
        return {symbol: date.fromisoformat(raw) for symbol, raw in self._earnings.items()}

    def save_alert_rule(self, rule: dict) -> dict:
        saved = dict(rule)
        taken = [int(rule_id) for rule_id in self._alert_rules if rule_id.isdigit()]
        saved["id"] = str(max(taken, default=0) + 1)
        saved["symbol"] = str(saved.get("symbol", "")).strip().upper()
        self._alert_rules[saved["id"]] = saved
        return dict(saved)

    def list_alert_rules(self, symbol: str | None = None) -> list[dict]:
        rules = [dict(rule) for _, rule in sorted(self._alert_rules.items())]
        if symbol is None:
            return rules
        want = str(symbol).strip().upper()
        return [rule for rule in rules if rule.get("symbol") == want]

    def cancel_alert_rule(self, rule_id: str) -> bool:
        return self._alert_rules.pop(str(rule_id).strip(), None) is not None

    def restore_alert_rules(self, rules: list[dict]) -> None:
        self._alert_rules = {}
        for item in rules:
            if not isinstance(item, dict) or not str(item.get("symbol", "")).strip():
                continue
            saved = dict(item)
            rule_id = str(saved.get("id", "")).strip()
            if not rule_id or rule_id in self._alert_rules:
                taken = [int(existing) for existing in self._alert_rules if existing.isdigit()]
                rule_id = str(max(taken, default=0) + 1)
            saved["id"] = rule_id
            saved["symbol"] = str(saved.get("symbol", "")).strip().upper()
            self._alert_rules[saved["id"]] = saved

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
        self._by_underlying = {
            underlying: [sid for sid in ids if sid in keep]
            for underlying, ids in self._by_underlying.items()
        }
        self._latest = {}
        for snapshot_id in self._order:
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is not None:
                self._latest[snapshot.underlying] = snapshot_id
        for underlying, bars in self._bars.items():
            if len(bars) > keep_per_underlying:
                del bars[: len(bars) - keep_per_underlying]
                self._bar_keys[underlying] = {(bar.interval, bar.timestamp_utc) for bar in bars}
        return len(dropped)

    def restore(self, other: SnapshotStore) -> None:
        self._snapshots = dict(other._snapshots)
        self._bars = {underlying: list(bars) for underlying, bars in other._bars.items()}
        self._bar_keys = {underlying: set(keys) for underlying, keys in other._bar_keys.items()}
        self._profile = other._profile
        self._iv = {symbol: dict(days) for symbol, days in other._iv.items()}
        self._holdings = list(other._holdings)
        self._settings = other._settings
        self._earnings = dict(other._earnings)
        self._alert_rules = {rule_id: dict(rule) for rule_id, rule in other._alert_rules.items()}
        self._rebuild_indexes()

    def merge_from(self, other: SnapshotStore) -> None:
        for snapshot_id in other._order:
            snapshot = other._snapshots.get(snapshot_id)
            if snapshot is None:
                continue
            if snapshot_id not in self._snapshots:
                self.save(snapshot)
        for underlying, bars in other._bars.items():
            for bar in bars:
                self.save_bar(underlying, bar)
        for symbol, days in other._iv.items():
            for day_iso, iv in days.items():
                self.save_iv(symbol, day_iso, iv)
        for rule_id, rule in other._alert_rules.items():
            if rule_id not in self._alert_rules:
                self._alert_rules[rule_id] = dict(rule)
        self._settings = other._settings
