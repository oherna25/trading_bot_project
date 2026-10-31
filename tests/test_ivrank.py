import os
from datetime import datetime, timedelta, timezone

import pytest

from bot.ivrank import (
    _invertible_mid,
    backfill_iv_history,
    histories_for,
    history_values,
    implied_volatility,
    iv_percentile,
    iv_rank,
)
from bot.journal import Journal
from bot.persist import load_bundle, save_bundle
from bot.shapes import Bar, ChainSnapshot, OptionContract
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


def _bs_price(mid_vol, spot, strike, years, call_or_put, rate):
    import math

    def norm(x):
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    discount = math.exp(-rate * years)
    d1 = (math.log(spot / strike) + (rate + mid_vol * mid_vol / 2.0) * years) / (mid_vol * math.sqrt(years))
    d2 = d1 - mid_vol * math.sqrt(years)
    if call_or_put == "call":
        return spot * norm(d1) - strike * discount * norm(d2)
    return strike * discount * norm(-d2) - spot * norm(-d1)


def test_implied_volatility_matches_textbook_values():
    assert implied_volatility(10.4506, 100.0, 100.0, 1.0, "call", rate=0.05) == pytest.approx(0.2, abs=1e-3)
    assert implied_volatility(5.5735, 100.0, 100.0, 1.0, "put", rate=0.05) == pytest.approx(0.2, abs=1e-3)


def test_implied_volatility_round_trips_known_vols():
    for vol in (0.15, 0.30, 0.60):
        for side in ("call", "put"):
            price = _bs_price(vol, 100.0, 105.0, 0.25, side, 0.04)
            assert implied_volatility(price, 100.0, 105.0, 0.25, side, rate=0.04) == pytest.approx(vol, abs=1e-4)


def test_implied_volatility_edge_cases_return_none():
    assert implied_volatility(2.50, 100.0, 100.0, 0.0, "call") is None
    assert implied_volatility(2.50, 100.0, 100.0, -1.0, "call") is None
    assert implied_volatility(0.0, 100.0, 100.0, 1.0, "call") is None
    assert implied_volatility(-1.0, 100.0, 100.0, 1.0, "put") is None
    assert implied_volatility(1.0, 100.0, 90.0, 1.0, "call") is None
    assert implied_volatility(1.0, 90.0, 100.0, 1.0, "put") is None
    assert implied_volatility(150.0, 100.0, 100.0, 1.0, "put") is None
    assert implied_volatility(150.0, 100.0, 100.0, 1.0, "call") is None


def _ivless_store(count=22):
    store = SnapshotStore()
    for n in range(count):
        store.save(_priced_snapshot("TEST", NOW - timedelta(hours=n), None))
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW - timedelta(hours=count + 1),
                               open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0))
    return store


def test_backfill_inverts_iv_from_priced_snapshots_and_enables_rank():
    store, journal = _ivless_store(), Journal()
    reply = backfill_iv_history(store, journal, NOW)
    assert "22 samples across 1 symbols" in reply
    assert "rankable: TEST" in reply
    assert len(store.read_iv_history("TEST")) == 22
    assert iv_rank(histories_for(store, ["TEST"])["TEST"]) is not None
    (entry,) = [e for e in journal.entries() if e.kind == "iv_backfilled"]
    assert entry.payload["inverted"] == 22


def test_backfill_prefers_vendor_iv_per_snapshot_without_mixing():
    store, journal = SnapshotStore(), Journal()
    store.save(_priced_snapshot("TEST", NOW - timedelta(hours=1), 0.25))
    store.save(_priced_snapshot("TEST", NOW, None))
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW - timedelta(hours=2),
                               open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0))
    reply = backfill_iv_history(store, journal, NOW)
    assert "2 samples" in reply
    history = store.read_iv_history("TEST")
    assert history[(NOW - timedelta(hours=1)).isoformat()] == 0.25
    (entry,) = [e for e in journal.entries() if e.kind == "iv_backfilled"]
    assert entry.payload["inverted"] == 1


def _filter_contract(strike, side, mid, oi=1000.0, expiry="2026-11-20"):
    return OptionContract(
        strike=strike, expiry=expiry, call_or_put=side, open_interest=oi,
        gamma=0.05, price=mid, bid=mid - 0.05, ask=mid + 0.05, iv=None,
    )


def test_invertible_mid_keeps_near_money_liquid_contracts():
    ref = NOW.date()
    assert _invertible_mid(_filter_contract(102.0, "call", 2.50), 100.0, ref) is not None
    assert _invertible_mid(_filter_contract(98.0, "put", 2.50), 100.0, ref) is not None
    assert _invertible_mid(_filter_contract(100.0, "call", 2.50), 100.0, ref) is not None


def test_invertible_mid_drops_deep_itm_penny_short_dated_and_thin():
    ref = NOW.date()
    assert _invertible_mid(_filter_contract(90.0, "call", 12.00), 100.0, ref) is None
    assert _invertible_mid(_filter_contract(110.0, "put", 12.00), 100.0, ref) is None
    assert _invertible_mid(_filter_contract(102.0, "call", 0.05), 100.0, ref) is None
    assert _invertible_mid(_filter_contract(102.0, "call", 2.50, oi=10.0), 100.0, ref) is None
    assert _invertible_mid(_filter_contract(102.0, "call", 2.50, expiry="2026-10-27"), 100.0, ref) is None
    assert _invertible_mid(_filter_contract(102.0, "call", 2.50, expiry="2026-10-28"), 100.0, ref) is not None
    assert _invertible_mid(_filter_contract(102.0, "call", 2.50, expiry="2027-06-01"), 100.0, ref) is None


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


def test_backfill_never_uses_a_future_bar():
    store = SnapshotStore()
    store.save(ChainSnapshot(
        underlying="TEST", timestamp_utc=NOW, source_name="t", latency_ms=1.0,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                           open_interest=500.0, gamma=0.05, price=2.50, bid=2.40, ask=2.60),
        ),
    ))
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW + timedelta(days=10),
                               open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0))
    reply = backfill_iv_history(store, Journal(), NOW)
    assert "no new samples" in reply
    assert store.read_iv_history("TEST") == {}
