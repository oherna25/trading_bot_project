import json
from datetime import datetime, timezone

import pytest

from bot.journal import Journal
from bot.muse import TOOL_REGISTRY, FormData, dispatch_tool, route_http
from bot.persist import load_bundle, save_bundle
from bot.shapes import (
    Bar,
    ChainSnapshot,
    DashboardSettings,
    OperatorConfig,
    OperatorProfile,
    OptionContract,
    dashboard_settings_from,
)
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = OperatorConfig(max_day_loss=1000.0, day_profit_target=500.0, max_trade_size=5000.0)


@pytest.fixture(autouse=True)
def _no_reason_backend(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})


def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=NOW, source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(
                OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                               open_interest=1000.0, gamma=0.05, price=2.50,
                               bid=2.40, ask=2.60, iv=0.30),
                OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="put",
                               open_interest=2000.0, gamma=0.03, price=3.00,
                               bid=2.90, ask=3.10, iv=0.32),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW, open=100.0, high=103.0,
                               low=99.0, close=102.0, volume=1200.0))
    store.save_profile(OperatorProfile(risk_tolerance="moderate", portfolio_size=50000.0,
                                       knowledge="intermediate", options_ok=True))
    return store


def test_settings_default_and_round_trip():
    store = SnapshotStore()
    assert store.read_settings() == DashboardSettings()
    store.save_settings(DashboardSettings(theme="light", default_underlying="QQQ", refresh_seconds=30))
    assert store.read_settings().theme == "light"
    assert store.read_settings().default_underlying == "QQQ"
    assert store.read_settings().refresh_seconds == 30


def test_settings_persist_round_trip(tmp_path):
    store = _seeded_store()
    store.save_settings(DashboardSettings(theme="light", default_underlying="QQQ", refresh_seconds=30))
    save_bundle(str(tmp_path), store, Journal())
    fresh, _ = load_bundle(str(tmp_path))
    assert fresh.read_settings() == DashboardSettings(
        theme="light", default_underlying="QQQ", refresh_seconds=30
    )


def test_settings_parse_rejects_loudly():
    for raw, needle in (
        ({"theme": "neon"}, "theme"),
        ({"default_underlying": "  "}, "default_underlying"),
        ({"refresh_seconds": -5}, "refresh_seconds"),
        ({"refresh_seconds": "often"}, "refresh_seconds"),
    ):
        try:
            dashboard_settings_from(raw)
        except ValueError as exc:
            assert needle in str(exc)
        else:
            raise AssertionError(f"{raw} should have raised")


def test_settings_route_renders_form():
    response = route_http("GET", "/settings", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert body.startswith("<!doctype html>")
    assert 'name="theme"' in body
    assert 'name="default_underlying"' in body
    assert 'name="refresh_seconds"' in body


def test_settings_post_json_saves_and_answers():
    store = _seeded_store()
    response = route_http(
        "POST", "/settings",
        {"theme": "light", "default_underlying": "QQQ", "refresh_seconds": 30},
        store, Journal(), CONFIG, NOW,
    )
    assert response.status == 200
    assert json.loads(response.body.decode("utf-8"))["ok"] is True
    assert store.read_settings().theme == "light"


def test_settings_post_form_redirects_back():
    store = _seeded_store()
    response = route_http(
        "POST", "/settings",
        FormData({"theme": "light", "next": "/board"}),
        store, Journal(), CONFIG, NOW,
    )
    assert response.status == 302
    assert ("Location", "/board") in response.headers
    assert store.read_settings().theme == "light"


def test_settings_post_bad_input_is_loud():
    store = _seeded_store()
    response = route_http(
        "POST", "/settings", {"theme": "neon"}, store, Journal(), CONFIG, NOW
    )
    assert response.status == 400
    assert "theme" in json.loads(response.body.decode("utf-8"))["error"]
    assert store.read_settings() == DashboardSettings()


def test_theme_toggle_reflects_on_dashboard_and_board():
    store = _seeded_store()
    journal = Journal()
    dashboard = route_http("GET", "/dashboard?underlying=TEST", {}, store, journal, CONFIG, NOW)
    assert 'data-theme="dark"' in dashboard.body.decode("utf-8")
    route_http("POST", "/settings", FormData({"theme": "light"}), store, journal, CONFIG, NOW)
    for path in ("/dashboard?underlying=TEST", "/board"):
        response = route_http("GET", path, {}, store, journal, CONFIG, NOW)
        assert response.status == 200
        assert 'data-theme="light"' in response.body.decode("utf-8")


def test_default_underlying_applies_to_pages():
    store = _seeded_store()
    store.save_settings(DashboardSettings(default_underlying="TEST"))
    response = route_http("GET", "/dashboard", {}, store, Journal(), CONFIG, NOW)
    assert response.status == 200
    assert "TEST dashboard" in response.body.decode("utf-8")


def test_registry_pins_new_tools():
    for name in ("set_settings", "get_settings", "load_snapshot", "set_profile"):
        assert name in TOOL_REGISTRY


def test_set_settings_tool_round_trips():
    store = _seeded_store()
    reply = dispatch_tool(
        "set_settings", {"theme": "light", "default_underlying": "QQQ"},
        store, Journal(), CONFIG, NOW, underlying="TEST",
    )
    assert reply.ok is True
    assert store.read_settings().theme == "light"
    assert store.read_settings().default_underlying == "QQQ"
    read = dispatch_tool("get_settings", {}, store, Journal(), CONFIG, NOW, underlying="TEST")
    assert read.ok is True
    assert "QQQ" in read.reply


def test_load_snapshot_tool_answers_offline():
    reply = dispatch_tool(
        "load_snapshot", {"symbol": "TEST"}, _seeded_store(), Journal(), CONFIG, NOW,
        underlying="TEST",
    )
    assert reply.ok is True
    assert "TEST" in reply.reply


def test_action_route_reaches_handler():
    response = route_http(
        "GET", "/action?tool=paper_pnl&underlying=TEST", {}, _seeded_store(),
        Journal(), CONFIG, NOW,
    )
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert body.startswith("<!doctype html>")
    assert "paper pnl" in body


def test_action_route_rejects_writes_and_unknown_tools():
    store = _seeded_store()
    denied = route_http(
        "GET", "/action?tool=set_holdings&underlying=TEST", {}, store, Journal(), CONFIG, NOW
    )
    assert denied.status == 400
    unknown = route_http(
        "GET", "/action?tool=nope_missing&underlying=TEST", {}, store, Journal(), CONFIG, NOW
    )
    assert unknown.status == 400


def test_holdings_form_post_reaches_handler():
    store = _seeded_store()
    response = route_http(
        "POST", "/holdings",
        FormData({"holdings": '[{"symbol": "SPY", "kind": "stock", "side": "long", "qty": 10}]',
                  "next": "/dashboard?underlying=TEST"}),
        store, Journal(), CONFIG, NOW,
    )
    assert response.status == 302
    assert store.read_holdings()[0].symbol == "SPY"


def test_profile_form_post_reaches_handler():
    store = _seeded_store()
    response = route_http(
        "POST", "/profile",
        FormData({"risk_tolerance": "moderate", "portfolio_size": "50000",
                  "knowledge": "intermediate", "options_ok": "yes",
                  "next": "/dashboard?underlying=TEST"}),
        store, Journal(), CONFIG, NOW,
    )
    assert response.status == 302
    assert store.read_profile() is not None
    assert store.read_profile().portfolio_size == 50000.0
