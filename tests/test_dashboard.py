from datetime import datetime, timezone

import pytest

from bot.dashboard import dashboard_html
from bot.journal import Journal
from bot.muse import route_http
from bot.shapes import Bar, ChainSnapshot, OperatorProfile, OptionContract, default_operator_config
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()


@pytest.fixture(autouse=True)
def _no_reason_backend(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})


def _contract(strike, side, oi, gamma):
    return OptionContract(
        strike=strike, expiry="2026-11-20", call_or_put=side, open_interest=oi,
        gamma=gamma, price=2.50, bid=2.40, ask=2.60, iv=0.30,
    )


def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=NOW, source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(_contract(95.0, "put", 3000.0, 0.05), _contract(105.0, "call", 3000.0, 0.05)),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0))
    store.save_profile(OperatorProfile(
        risk_tolerance="moderate", portfolio_size=50000.0, knowledge="intermediate", options_ok=True
    ))
    return store


def test_symbol_dashboard_hosts_the_chat_box_under_the_stat_tiles():
    body = dashboard_html(_seeded_store(), Journal(), NOW, "TEST")
    assert body.count("<h1>") == 1
    assert 'id="chat-form"' in body
    assert "Ask the desk" in body
    assert body.index('class="stats"') < body.index('class="chatbox"') < body.index("<h2>Digest</h2>")
    for chip in ("gex brief", "trade cards", "claude audit", "wheel ideas", "shock TEST -2%"):
        assert f'data-q="{chip}"' in body
    assert "demo" not in body.lower()


def test_symbol_dashboard_does_not_reload_under_the_chat_box():
    store = _seeded_store()
    assert 'http-equiv="refresh"' not in dashboard_html(store, Journal(), NOW, "TEST")
    assert 'http-equiv="refresh"' in dashboard_html(store, Journal(), NOW)


def test_chat_box_sends_live_models_flag(monkeypatch):
    from bot import models
    from bot.shapes import UserModelConfig

    monkeypatch.setattr(
        models, "portable_config",
        lambda env=None: (UserModelConfig("k", "https://x/v1", "m"), "baseten", ""),
    )
    body = dashboard_html(_seeded_store(), Journal(), NOW, "TEST")
    assert 'id="chat-live" checked' in body
    assert "live_models:lv.checked" in body


def test_chat_box_symbol_cannot_break_out_of_the_script():
    body = dashboard_html(_seeded_store(), Journal(), NOW, "X</script><b>")
    assert "X</script>" not in body
    assert 'underlying:"X\\u003c/script>' in body


def test_demo_page_is_gone():
    from bot.muse import openapi_spec
    from bot.theme import NAV_LINKS

    response = route_http("GET", "/demo?underlying=TEST", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 404
    assert "/demo" not in openapi_spec()["paths"]
    assert "/demo" not in [href for _, href in NAV_LINKS]
