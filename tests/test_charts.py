from datetime import datetime, timezone

from bot.charts import (
    CALL_COLOR,
    PUT_COLOR,
    board_rows,
    chart_strikes,
    gamma_chart_html,
    gamma_svg,
    regime_board_html,
)
from bot.exposure import compute_gex
from bot.journal import Journal
from bot.muse import route_http
from bot.shapes import Bar, ChainSnapshot, OptionContract, default_operator_config
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()


def _contract(strike, side, oi, gamma):
    return OptionContract(
        strike=strike, expiry="2026-11-20", call_or_put=side, open_interest=oi,
        gamma=gamma, price=2.50, bid=2.40, ask=2.60, iv=0.30,
    )


def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                _contract(95.0, "put", 3000.0, 0.05),
                _contract(105.0, "call", 3000.0, 0.05),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0))
    store.save(
        ChainSnapshot(
            underlying="DEMO",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(_contract(50.0, "call", 1000.0, 0.05),),
        )
    )
    store.save_bar("DEMO", Bar(interval="1d", timestamp_utc=NOW,
                               open=49.0, high=51.0, low=48.0, close=50.0, volume=500.0))
    return store


def _profile(store, symbol="TEST"):
    snapshot_id, snapshot = store.read_latest(symbol)
    bar = store.read_latest_bar(symbol)
    return snapshot_id, snapshot, compute_gex(snapshot, snapshot_id, spot=bar.close)


def test_compute_gex_splits_call_and_put_per_strike():
    _, _, profile = _profile(_seeded_store())
    assert profile.per_strike_call_exposure[105.0] > 0
    assert profile.per_strike_put_exposure[95.0] < 0
    assert profile.per_strike_call_exposure.get(95.0, 0.0) == 0.0
    assert profile.per_strike_put_exposure.get(105.0, 0.0) == 0.0
    assert profile.flip_point == 105.0


def test_chart_strikes_sorted_and_capped():
    _, _, profile = _profile(_seeded_store())
    assert chart_strikes(profile) == [95.0, 105.0]
    assert len(chart_strikes(profile, max_strikes=1)) == 1


def test_gamma_svg_marks_calls_puts_and_flip():
    _, _, profile = _profile(_seeded_store())
    svg = gamma_svg(profile)
    assert svg.startswith("<svg")
    assert CALL_COLOR in svg
    assert PUT_COLOR in svg
    assert "flip 105.0" in svg
    assert svg.count("<rect") == 2


def test_gamma_chart_html_refreshes_and_labels():
    store = _seeded_store()
    snapshot_id, snapshot = store.read_latest("TEST")
    bar = store.read_latest_bar("TEST")
    page = gamma_chart_html("TEST", snapshot, _profile(store)[2], bar, NOW)
    assert page.startswith("<!doctype html>")
    assert 'http-equiv="refresh" content="60"' in page
    assert "TEST gamma by strike" in page
    assert "flip 105.0" in page
    assert snapshot_id in page


def test_board_rows_cover_every_underlying():
    rows = board_rows(_seeded_store())
    assert [row["symbol"] for row in rows] == ["DEMO", "TEST"]
    test = next(row for row in rows if row["symbol"] == "TEST")
    assert test["regime"] == "long-gamma"
    assert test["flip"] == 105.0
    assert test["flip_distance_pct"] == 5.0
    assert test["wall_strike"] == 95.0


def test_regime_board_html_renders_table():
    page = regime_board_html(_seeded_store())
    assert "<table>" in page
    assert "DEMO" in page and "TEST" in page
    assert "+5.0%" in page
    assert 'http-equiv="refresh"' in page


def test_regime_board_html_empty_store():
    assert "no snapshots stored" in regime_board_html(SnapshotStore())


def test_chart_route_serves_page_and_400s_unknown():
    response = route_http("GET", "/chart?underlying=TEST", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    assert "gamma by strike" in response.body.decode("utf-8")
    missing = route_http("GET", "/chart?underlying=NOPE", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert missing.status == 400


def test_board_route_serves_page():
    response = route_http("GET", "/board", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    assert "Dealer positioning board" in response.body.decode("utf-8")
