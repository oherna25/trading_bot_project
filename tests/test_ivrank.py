import os
from datetime import datetime, timedelta, timezone

from bot.ivrank import backfill_iv_history, histories_for, history_values, iv_percentile, iv_rank
from bot.journal import Journal
from bot.persist import load_bundle, save_bundle
from bot.shapes import ChainSnapshot, OptionContract
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


def _priced_snapshot(symbol, stamp, iv):
    return ChainSnapshot(
        underlying=symbol,
        timestamp_utc=stamp,
        source_name="tradier_sandbox",
        latency_ms=9.0,
        contracts_list=(
            OptionContract(
                strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=1000.0,
                gamma=0.05, price=2.50, bid=2.40, ask=2.60, iv=iv,
            ),
            OptionContract(
                strike=100.0, expiry="2026-11-20", call_or_put="put", open_interest=2000.0,
                gamma=0.03, price=3.00, bid=2.90, ask=3.10, iv=None,
            ),
        ),
    )


def _backfill_store(count=22):
    store = SnapshotStore()
    for n in range(count):
        store.save(_priced_snapshot("TEST", NOW - timedelta(hours=n), 0.20 + 0.001 * n))
    return store


def test_backfill_samples_every_snapshot_and_enables_rank():
    store, journal = _backfill_store(), Journal()
    reply = backfill_iv_history(store, journal, NOW)
    assert "22 samples across 1 symbols" in reply
    assert "rankable: TEST" in reply
    assert len(store.read_iv_history("TEST")) == 22
    assert iv_rank(histories_for(store, ["TEST"])["TEST"]) == 0.0
    assert [e.kind for e in journal.entries()] == ["iv_backfilled"]


def test_backfill_is_idempotent_and_skips_recorded_days():
    store, journal = _backfill_store(3), Journal()
    store.save_iv("TEST", "2026-10-27", 0.99)
    first = backfill_iv_history(store, journal, NOW)
    assert "3 samples" in first
    assert store.read_iv_history("TEST")["2026-10-27"] == 0.99
    second = backfill_iv_history(store, journal, NOW)
    assert "no new samples" in second
    assert len(store.read_iv_history("TEST")) == 4


def test_backfill_reports_snapshots_without_priced_iv():
    store = SnapshotStore()
    store.save(_priced_snapshot("BARE", NOW, None))
    reply = backfill_iv_history(store, Journal(), NOW)
    assert "no new samples" in reply
    assert "1 snapshots without priced IV" in reply
    assert store.read_iv_history("BARE") == {}


def test_backfill_chat_keyword_and_tool():
    from bot.muse import TOOL_REGISTRY, dispatch_tool
    from bot.research import route_chat
    from bot.shapes import default_operator_config

    store = _backfill_store(2)
    reply = route_chat("backfill iv history", "TEST", store, Journal(), default_operator_config(), NOW)
    assert "2 samples" in reply
    assert "backfill_iv" in TOOL_REGISTRY
    stub_config = default_operator_config()
    tool_reply = dispatch_tool("backfill_iv", {}, store, Journal(), stub_config, NOW, underlying="TEST")
    assert tool_reply.ok
    assert "no new samples" in tool_reply.reply
