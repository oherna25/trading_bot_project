from datetime import datetime, timezone

import pytest

from bot.exposure import compute_dex, compute_gex, compute_vex
from bot.shapes import ChainSnapshot, OptionContract

STAMP = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)


def _gex_snapshot() -> ChainSnapshot:
    return ChainSnapshot(
        underlying="TEST",
        timestamp_utc=STAMP,
        source_name="tradier_sandbox",
        latency_ms=12.5,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=1000.0, gamma=0.05),
            OptionContract(strike=105.0, expiry="2026-11-20", call_or_put="call", open_interest=1000.0, gamma=0.10),
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="put", open_interest=2000.0, gamma=0.03),
            OptionContract(strike=95.0, expiry="2026-11-20", call_or_put="put", open_interest=1000.0, gamma=0.02),
            OptionContract(strike=110.0, expiry="2026-11-20", call_or_put="call", open_interest=999.0, gamma=None),
        ),
    )


def _vex_snapshot() -> ChainSnapshot:
    return ChainSnapshot(
        underlying="TEST",
        timestamp_utc=STAMP,
        source_name="tradier_sandbox",
        latency_ms=12.5,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=100.0, vega=0.20),
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="put", open_interest=100.0, vega=0.10),
        ),
    )


def test_gex_per_strike_and_total():
    profile = compute_gex(_gex_snapshot(), "TEST:snap-1")
    assert profile.snapshot_id == "TEST:snap-1"
    assert profile.per_strike_exposure == pytest.approx({95.0: -2000.0, 100.0: -1000.0, 105.0: 10000.0, 110.0: 0.0})
    assert profile.total_exposure == pytest.approx(7000.0)


def test_gex_flip_point_interpolates_between_strikes():
    profile = compute_gex(_gex_snapshot(), "TEST:snap-1")
    assert profile.flip_point == pytest.approx(101.5)


def test_gex_dollar_scales_by_spot_squared_per_one_percent():
    profile = compute_gex(_gex_snapshot(), "TEST:snap-1", spot=100.0)
    assert profile.spot == 100.0
    assert profile.total_exposure == pytest.approx(7000.0 * 100.0 * 100.0 * 0.01)
    assert profile.per_strike_exposure[105.0] == pytest.approx(10000.0 * 100.0 * 100.0 * 0.01)
    assert profile.flip_point == pytest.approx(101.5)


def test_gex_front_expiry_and_call_put_split():
    profile = compute_gex(_gex_snapshot(), "TEST:snap-1")
    assert profile.front_expiry == "2026-11-20"
    assert profile.front_exposure == pytest.approx(7000.0)
    assert profile.call_exposure == pytest.approx(15000.0)
    assert profile.put_exposure == pytest.approx(-8000.0)


def test_gex_missing_gamma_contributes_zero():
    snapshot = ChainSnapshot(
        underlying="TEST",
        timestamp_utc=STAMP,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=500.0, gamma=None),
        ),
    )
    profile = compute_gex(snapshot, "TEST:snap-2")
    assert profile.per_strike_exposure == {100.0: 0.0}
    assert profile.total_exposure == 0.0
    assert profile.flip_point is None


def test_vex_per_strike_and_total():
    profile = compute_vex(_vex_snapshot(), "TEST:snap-1")
    assert profile.snapshot_id == "TEST:snap-1"
    assert profile.per_strike_exposure == pytest.approx({100.0: 1000.0})
    assert profile.total_exposure == pytest.approx(1000.0)


def test_dex_uses_signed_delta_times_open_interest():
    snapshot = ChainSnapshot(
        underlying="TEST",
        timestamp_utc=STAMP,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=100.0, delta=0.55),
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="put", open_interest=100.0, delta=-0.45),
            OptionContract(strike=105.0, expiry="2026-11-20", call_or_put="call", open_interest=50.0, delta=None),
        ),
    )
    profile = compute_dex(snapshot, "TEST:snap-1")
    assert profile.per_strike_exposure == pytest.approx({100.0: 1000.0})
    assert profile.total_exposure == pytest.approx(1000.0)


def test_gex_normalizes_side_case_and_ignores_unknown_side():
    snapshot = ChainSnapshot(
        underlying="TEST",
        timestamp_utc=STAMP,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="CALL", open_interest=10.0, gamma=0.10),
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="put", open_interest=10.0, gamma=0.10),
            OptionContract(strike=105.0, expiry="2026-11-20", call_or_put="straddle", open_interest=10.0, gamma=0.10),
        ),
    )
    profile = compute_gex(snapshot, "TEST:snap-case")
    assert profile.total_exposure == pytest.approx(0.0)
    assert profile.call_exposure == pytest.approx(100.0)
    assert profile.put_exposure == pytest.approx(-100.0)
    assert 105.0 not in profile.per_strike_exposure
