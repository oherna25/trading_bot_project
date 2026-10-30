import os
from datetime import datetime, timezone

from bot.ivrank import histories_for, history_values, iv_percentile, iv_rank
from bot.journal import Journal
from bot.persist import load_bundle, save_bundle
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)


def test_iv_rank_endpoints():
    rising = [0.10 + 0.01 * n for n in range(20)]
    assert iv_rank(rising) == 100.0
    assert iv_rank(list(reversed(rising))) == 0.0


def test_iv_rank_midpoint_and_flat():
    assert iv_rank([0.10] * 10 + [0.30] * 9 + [0.20]) == 50.0
    assert iv_rank([0.25] * 20) == 50.0


def test_iv_rank_needs_twenty_points():
    assert iv_rank([0.10] * 19) is None
    assert iv_rank([]) is None


def test_iv_percentile_counts_values_below_current():
    assert iv_percentile([0.10] * 10 + [0.30] * 9 + [0.20]) == 50.0
    assert iv_percentile([0.25] * 20) == 0.0
    assert iv_percentile([0.10] * 5) is None


def test_history_values_sort_oldest_first():
    assert history_values({"2026-10-03": 0.3, "2026-10-01": 0.1, "2026-10-02": 0.2}) == [0.1, 0.2, 0.3]


def test_histories_for_reads_store_newest_last():
    store = SnapshotStore()
    store.save_iv("TEST", "2026-10-02", 0.2)
    store.save_iv("TEST", "2026-10-01", 0.1)
    assert histories_for(store, ["TEST", "MISSING"]) == {"TEST": [0.1, 0.2], "MISSING": []}


def test_store_iv_round_trip_and_cap():
    store = SnapshotStore()
    store.save_iv("TEST", "2026-10-01", 0.21)
    store.save_iv("TEST", "2026-10-01", 0.22)
    assert store.read_iv_history("TEST") == {"2026-10-01": 0.22}
    assert store.read_iv_history("MISSING") == {}
    for day in range(1, 5):
        store.save_iv("CAP", f"2026-10-0{day}", 0.10, keep_per_symbol=2)
    assert store.read_iv_history("CAP") == {"2026-10-03": 0.10, "2026-10-04": 0.10}


def test_persist_bundle_carries_iv(tmp_path):
    store, journal = SnapshotStore(), Journal()
    store.save_iv("TEST", "2026-10-26", 0.21)
    store.save_iv("TEST", "2026-10-27", 0.23)
    save_bundle(str(tmp_path), store, journal)
    assert os.path.exists(tmp_path / "iv.json")
    loaded, _ = load_bundle(str(tmp_path))
    assert loaded.read_iv_history("TEST") == {"2026-10-26": 0.21, "2026-10-27": 0.23}


def test_load_bundle_tolerates_missing_iv(tmp_path):
    store, journal = SnapshotStore(), Journal()
    save_bundle(str(tmp_path), store, journal)
    os.remove(tmp_path / "iv.json")
    loaded, _ = load_bundle(str(tmp_path))
    assert loaded.read_iv_history("TEST") == {}
