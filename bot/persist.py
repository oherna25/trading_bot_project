from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, Holding, JournalEntry, OperatorProfile, OptionContract
from bot.store import SnapshotStore

SNAPSHOTS_FILE = "snapshots.json"
BARS_FILE = "bars.json"
JOURNAL_FILE = "journal.json"
PROFILE_FILE = "profile.json"
IV_FILE = "iv.json"
HOLDINGS_FILE = "holdings.json"


def _freeze(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, dict):
        return {str(key): _freeze(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_freeze(item) for item in value]
    return value


def _thaw_datetime(raw: Any) -> datetime:
    if isinstance(raw, dict) and "$datetime" in raw:
        return datetime.fromisoformat(raw["$datetime"])
    if isinstance(raw, str):
        return datetime.fromisoformat(raw)
    raise ValueError(f"bad datetime {raw!r}")


def _thaw_value(raw: Any) -> Any:
    if isinstance(raw, dict):
        if set(raw.keys()) == {"$datetime"}:
            return datetime.fromisoformat(raw["$datetime"])
        return {key: _thaw_value(item) for key, item in raw.items()}
    if isinstance(raw, list):
        return [_thaw_value(item) for item in raw]
    return raw


def _freeze_contract(contract: OptionContract) -> dict[str, Any]:
    return {
        "strike": contract.strike,
        "expiry": contract.expiry,
        "call_or_put": contract.call_or_put,
        "open_interest": contract.open_interest,
        "gamma": contract.gamma,
        "vega": contract.vega,
        "delta": contract.delta,
        "price": contract.price,
        "bid": contract.bid,
        "ask": contract.ask,
        "iv": contract.iv,
    }


def _thaw_contract(raw: dict[str, Any]) -> OptionContract:
    return OptionContract(
        strike=float(raw["strike"]),
        expiry=str(raw["expiry"]),
        call_or_put=str(raw["call_or_put"]),
        open_interest=float(raw["open_interest"]),
        gamma=raw.get("gamma"),
        vega=raw.get("vega"),
        delta=raw.get("delta"),
        price=raw.get("price"),
        bid=raw.get("bid"),
        ask=raw.get("ask"),
        iv=raw.get("iv"),
    )


def _freeze_snapshot(snapshot_id: str, snapshot: ChainSnapshot) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "underlying": snapshot.underlying,
        "timestamp_utc": _freeze(snapshot.timestamp_utc),
        "source_name": snapshot.source_name,
        "latency_ms": snapshot.latency_ms,
        "contracts": [_freeze_contract(contract) for contract in snapshot.contracts_list],
    }


def _thaw_snapshot(raw: dict[str, Any]) -> tuple[str, ChainSnapshot]:
    snapshot = ChainSnapshot(
        underlying=str(raw["underlying"]),
        timestamp_utc=_thaw_datetime(raw["timestamp_utc"]),
        source_name=str(raw["source_name"]),
        latency_ms=float(raw["latency_ms"]),
        contracts_list=tuple(_thaw_contract(item) for item in raw.get("contracts", [])),
    )
    return str(raw["snapshot_id"]), snapshot


def _freeze_bar(underlying: str, bar: Bar) -> dict[str, Any]:
    return {
        "underlying": underlying,
        "interval": bar.interval,
        "timestamp_utc": _freeze(bar.timestamp_utc),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _thaw_bar(raw: dict[str, Any]) -> tuple[str, Bar]:
    bar = Bar(
        interval=str(raw["interval"]),
        timestamp_utc=_thaw_datetime(raw["timestamp_utc"]),
        open=float(raw["open"]),
        high=float(raw["high"]),
        low=float(raw["low"]),
        close=float(raw["close"]),
        volume=float(raw["volume"]),
    )
    return str(raw["underlying"]), bar


def _freeze_entry(entry: JournalEntry) -> dict[str, Any]:
    return {
        "timestamp_utc": _freeze(entry.timestamp_utc),
        "kind": entry.kind,
        "payload": _freeze(entry.payload),
    }


def _freeze_profile(profile: OperatorProfile) -> dict[str, Any]:
    return {
        "risk_tolerance": profile.risk_tolerance,
        "portfolio_size": profile.portfolio_size,
        "knowledge": profile.knowledge,
        "options_ok": profile.options_ok,
    }


def _freeze_holding(holding: Holding) -> dict[str, Any]:
    return {
        "symbol": holding.symbol,
        "kind": holding.kind,
        "side": holding.side,
        "strike": holding.strike,
        "expiry": holding.expiry,
        "qty": holding.qty,
    }


def _thaw_holding(raw: dict[str, Any]) -> Holding:
    strike = raw.get("strike")
    expiry = raw.get("expiry")
    return Holding(
        symbol=str(raw.get("symbol", "")),
        kind=str(raw.get("kind", "")),
        side=str(raw.get("side", "")),
        strike=None if strike is None else float(strike),
        expiry=None if expiry is None else str(expiry),
        qty=float(raw.get("qty", 1.0)),
    )


def _thaw_profile(raw: dict[str, Any]) -> OperatorProfile:
    return OperatorProfile(
        risk_tolerance=str(raw["risk_tolerance"]),
        portfolio_size=float(raw["portfolio_size"]),
        knowledge=str(raw["knowledge"]),
        options_ok=bool(raw["options_ok"]),
    )


def _write_json(path: str, payload: Any) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp_path, path)


def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


KEEP_SNAPSHOTS_PER_UNDERLYING = 500
KEEP_BARS_PER_UNDERLYING = 500
KEEP_JOURNAL_ENTRIES = 20000
KEEP_IV_PER_SYMBOL = 600


def save_bundle(
    store_dir: str,
    store: SnapshotStore,
    journal: Journal,
    keep_snapshots: int = KEEP_SNAPSHOTS_PER_UNDERLYING,
    keep_bars: int = KEEP_BARS_PER_UNDERLYING,
    keep_journal: int = KEEP_JOURNAL_ENTRIES,
    keep_iv: int = KEEP_IV_PER_SYMBOL,
) -> None:
    os.makedirs(store_dir, exist_ok=True)
    counts: dict[str, int] = {}
    kept: list[tuple[str, ChainSnapshot]] = []
    for snapshot_id, snapshot in reversed(store.snapshots_in_order()):
        seen = counts.get(snapshot.underlying, 0)
        if seen >= keep_snapshots:
            continue
        counts[snapshot.underlying] = seen + 1
        kept.append((snapshot_id, snapshot))
    kept.reverse()
    _write_json(
        os.path.join(store_dir, SNAPSHOTS_FILE),
        [_freeze_snapshot(snapshot_id, snapshot) for snapshot_id, snapshot in kept],
    )
    _write_json(
        os.path.join(store_dir, BARS_FILE),
        [
            _freeze_bar(underlying, bar)
            for underlying, bars in store.all_bars().items()
            for bar in bars[-keep_bars:]
        ],
    )
    _write_json(
        os.path.join(store_dir, JOURNAL_FILE),
        [_freeze_entry(entry) for entry in journal.entries()[-keep_journal:]],
    )
    profile = store.read_profile()
    _write_json(
        os.path.join(store_dir, PROFILE_FILE),
        _freeze_profile(profile) if profile is not None else None,
    )
    _write_json(
        os.path.join(store_dir, IV_FILE),
        {
            symbol: {day: days[day] for day in sorted(days)[-keep_iv:]}
            for symbol, days in store.all_iv().items()
        },
    )
    _write_json(
        os.path.join(store_dir, HOLDINGS_FILE),
        [_freeze_holding(holding) for holding in store.read_holdings()],
    )


def load_bundle(store_dir: str) -> tuple[SnapshotStore, Journal]:
    store, journal = SnapshotStore(), Journal()
    snapshots_path = os.path.join(store_dir, SNAPSHOTS_FILE)
    if os.path.exists(snapshots_path):
        for raw in _read_json(snapshots_path):
            _, snapshot = _thaw_snapshot(raw)
            store.save(snapshot)
    bars_path = os.path.join(store_dir, BARS_FILE)
    if os.path.exists(bars_path):
        for raw in _read_json(bars_path):
            underlying, bar = _thaw_bar(raw)
            store.save_bar(underlying, bar)
    journal_path = os.path.join(store_dir, JOURNAL_FILE)
    if os.path.exists(journal_path):
        for raw in _read_json(journal_path):
            payload = _thaw_value(raw.get("payload", {}))
            journal.append(_thaw_datetime(raw["timestamp_utc"]), str(raw["kind"]), dict(payload))
    profile_path = os.path.join(store_dir, PROFILE_FILE)
    if os.path.exists(profile_path):
        raw_profile = _read_json(profile_path)
        if raw_profile is not None:
            store.save_profile(_thaw_profile(raw_profile))
    iv_path = os.path.join(store_dir, IV_FILE)
    if os.path.exists(iv_path):
        raw_iv = _read_json(iv_path)
        if isinstance(raw_iv, dict):
            for symbol, days in raw_iv.items():
                if isinstance(days, dict):
                    for day, value in days.items():
                        store.save_iv(str(symbol), str(day), float(value))
    holdings_path = os.path.join(store_dir, HOLDINGS_FILE)
    if os.path.exists(holdings_path):
        raw_holdings = _read_json(holdings_path)
        if isinstance(raw_holdings, list):
            store.save_holdings([_thaw_holding(item) for item in raw_holdings if isinstance(item, dict)])
    return store, journal


BUNDLE_FILES = (SNAPSHOTS_FILE, BARS_FILE, JOURNAL_FILE, PROFILE_FILE, IV_FILE, HOLDINGS_FILE)


def bundle_mtimes(store_dir: str) -> dict[str, float]:
    mtimes: dict[str, float] = {}
    for name in BUNDLE_FILES:
        path = os.path.join(store_dir, name)
        try:
            mtimes[name] = os.path.getmtime(path)
        except OSError:
            continue
    return mtimes


def refresh_bundle(store_dir: str, store: SnapshotStore, journal: Journal) -> bool:
    try:
        fresh_store, fresh_journal = load_bundle(store_dir)
    except (OSError, ValueError):
        return False
    store.restore(fresh_store)
    journal.merge(fresh_journal)
    return True
