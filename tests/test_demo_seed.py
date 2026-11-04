from datetime import datetime, timezone

import pytest

from bot.demo_seed import DEMO_SYMBOLS, seed_demo
from bot.exposure import compute_gex
from bot.ideas import rank_ideas
from bot.journal import Journal
from bot.muse import route_http
from bot.shapes import default_operator_config
from bot.store import SnapshotStore
from bot.wheel import histories_for, screen_premium
from run_demo import build_demo, parse_args

NOW = datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()


def _seeded():
    store, journal = SnapshotStore(), Journal()
    seed_demo(store, journal, NOW)
    return store, journal


def test_seed_covers_every_demo_surface_without_keys():
    store, journal = _seeded()
    assert store.read_profile() is not None and store.read_profile().options_ok
    assert [d.strategy_id for d in store.list_deployments()] == ["put_credit_spread"]
    assert journal.entries()[-1].kind == "demo_seeded"
    for symbol in DEMO_SYMBOLS:
        found = store.read_latest(symbol)
        assert found is not None
        snapshot_id, snapshot = found
        bar = store.read_latest_bar(symbol)
        assert bar is not None
        assert len(store.read_bars(symbol)) >= 5
        assert len(store.read_iv_history(symbol)) >= 20
        assert all(
            c.gamma is not None and c.delta is not None and c.bid is not None and c.ask is not None
            and c.open_interest >= 100
            for c in snapshot.contracts_list
        )
        profile = compute_gex(snapshot, snapshot_id, spot=bar.close)
        assert profile.total_exposure != 0
        assert profile.flip_point is not None
        assert rank_ideas([(snapshot_id, snapshot, profile, bar)], now=NOW)
        candidates, _ = screen_premium(
            [(snapshot_id, snapshot, bar)], NOW, iv_history=histories_for(store, [symbol])
        )
        assert candidates


def test_seed_expiries_stay_in_wheel_band():
    store, _ = _seeded()
    _, snapshot = store.read_latest("SPY")
    dtes = {(datetime.fromisoformat(c.expiry).date() - NOW.date()).days for c in snapshot.contracts_list}
    assert dtes and all(7 <= dte <= 30 for dte in dtes)


def test_seed_is_idempotent():
    store, journal = _seeded()
    snapshots = len(store.snapshots_in_order())
    seed_demo(store, journal, NOW)
    assert len(store.snapshots_in_order()) == snapshots
    assert len(store.list_deployments()) == 1


def test_demo_routes_render_seeded_pages():
    store, journal = _seeded()
    for path in ("/demo?underlying=SPY", "/cards?underlying=SPY", "/deployments/page"):
        response = route_http("GET", path, {}, store, journal, CONFIG, NOW)
        assert response.status == 200, path
    body = route_http("GET", "/demo?underlying=SPY", {}, store, journal, CONFIG, NOW).body.decode()
    assert "1. Regime read" in body and "trade cards: none found" not in body


def test_build_demo_persists_when_store_dir_given(tmp_path):
    store, _, symbols = build_demo(str(tmp_path))
    assert symbols == list(DEMO_SYMBOLS)
    assert (tmp_path / "snapshots.json").exists()
    assert store.read_latest("SPY") is not None
    again, _, _ = build_demo(str(tmp_path))
    assert len(again.list_deployments()) == 1


def test_run_demo_defaults_are_local_and_key_free():
    args = parse_args([])
    assert args.host == "127.0.0.1"
    assert args.store_dir == ""
    with pytest.raises(SystemExit):
        parse_args(["--no-such-flag"])
