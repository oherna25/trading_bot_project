from datetime import datetime, timedelta, timezone

from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OperatorProfile, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)


def _snapshot(stamp, underlying="TEST"):
    return ChainSnapshot(
        underlying=underlying,
        timestamp_utc=stamp,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                           open_interest=100.0, gamma=0.05),
        ),
    )


def _bar(stamp):
    return Bar(interval="1d", timestamp_utc=stamp, open=100.0, high=101.0,
               low=99.0, close=100.5, volume=1000.0)


def test_save_bar_dedupes_reingest():
    store = SnapshotStore()
    assert store.save_bar("TEST", _bar(NOW)) is True
    assert store.save_bar("TEST", _bar(NOW)) is False
    assert store.save_bar("TEST", _bar(NOW + timedelta(days=1))) is True
    assert len(store.read_bars("TEST")) == 2


def test_read_latest_and_previous():
    store = SnapshotStore()
    first = store.save(_snapshot(NOW))
    second = store.save(_snapshot(NOW + timedelta(hours=1)))
    assert store.read_latest("TEST")[0] == second
    assert store.read_latest("MISSING") is None
    assert store.read_previous("TEST", second)[0] == first
    assert store.read_previous("TEST", first) is None
    assert store.read_previous("MISSING", second) is None


def test_prune_keeps_latest_per_underlying():
    store = SnapshotStore()
    for day in range(5):
        store.save(_snapshot(NOW + timedelta(days=day), "TEST"))
        store.save(_snapshot(NOW + timedelta(days=day), "QQQ"))
    dropped = store.prune(keep_per_underlying=2)
    assert dropped == 6
    assert len(store.snapshots_in_order()) == 4
    assert store.read_latest("TEST")[1].timestamp_utc == NOW + timedelta(days=4)
    assert store.read_latest("QQQ")[1].timestamp_utc == NOW + timedelta(days=4)


def test_restore_replaces_contents():
    live = SnapshotStore()
    live.save(_snapshot(NOW, "OLD"))
    fresh = SnapshotStore()
    fresh.save(_snapshot(NOW, "NEW"))
    fresh.save_bar("NEW", _bar(NOW))
    live.restore(fresh)
    assert live.list_underlyings() == ["NEW"]
    assert live.read_latest("OLD") is None
    assert len(live.read_bars("NEW")) == 1


def test_merge_from_appends_new_ids_in_other_order():
    live, other = SnapshotStore(), SnapshotStore()
    first = live.save(_snapshot(NOW))
    second = other.save(_snapshot(NOW + timedelta(hours=1)))
    third = other.save(_snapshot(NOW + timedelta(hours=2), "QQQ"))
    live.merge_from(other)
    assert [sid for sid, _ in live.snapshots_in_order()] == [first, second, third]


def test_merge_from_latest_keeps_newest_when_other_is_newer():
    live, other = SnapshotStore(), SnapshotStore()
    live.save(_snapshot(NOW))
    other.save(_snapshot(NOW + timedelta(hours=1)))
    live.merge_from(other)
    assert live.read_latest("TEST")[1].timestamp_utc == NOW + timedelta(hours=1)


def test_merge_from_latest_keeps_newest_when_live_is_newer():
    live, other = SnapshotStore(), SnapshotStore()
    live.save(_snapshot(NOW + timedelta(hours=2)))
    other.save(_snapshot(NOW))
    live.merge_from(other)
    assert live.read_latest("TEST")[1].timestamp_utc == NOW + timedelta(hours=2)


def test_merge_from_dedupes_bars():
    live, other = SnapshotStore(), SnapshotStore()
    live.save_bar("TEST", _bar(NOW))
    other.save_bar("TEST", _bar(NOW))
    other.save_bar("TEST", _bar(NOW + timedelta(days=1)))
    live.merge_from(other)
    assert [b.timestamp_utc for b in live.read_bars("TEST")] == [NOW, NOW + timedelta(days=1)]


def test_merge_from_merges_iv_and_keeps_live_profile():
    live, other = SnapshotStore(), SnapshotStore()
    live.save_iv("SPY", "2026-10-26", 0.20)
    other.save_iv("SPY", "2026-10-27", 0.25)
    mine = OperatorProfile(risk_tolerance="low", portfolio_size=1000.0, knowledge="new", options_ok=False)
    theirs = OperatorProfile(risk_tolerance="high", portfolio_size=999.0, knowledge="pro", options_ok=True)
    live.save_profile(mine)
    other.save_profile(theirs)
    live.merge_from(other)
    assert live.read_iv_history("SPY") == {"2026-10-26": 0.20, "2026-10-27": 0.25}
    assert live.read_profile() == mine


def test_journal_merge_adds_missing_entries_in_order():
    live = Journal()
    live.append(NOW, "signal", {"n": 1})
    disk = Journal()
    disk.append(NOW, "signal", {"n": 1})
    disk.append(NOW - timedelta(hours=1), "snapshot_ingested", {"n": 0})
    added = live.merge(disk)
    assert added == 1
    assert [e.kind for e in live.entries()] == ["snapshot_ingested", "signal"]
