import json
from datetime import datetime, timezone

import pytest

from bot.commands import PORTFOLIO_USAGE
from bot.journal import Journal
from bot.muse import _body_payload, dispatch_tool, route_http
from bot.research import route_chat
from bot.shapes import Bar, ChainSnapshot, OperatorConfig, OperatorProfile, OptionContract
from bot.shock import SHOCK_USAGE
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = OperatorConfig(max_day_loss=1000.0, day_profit_target=500.0, max_trade_size=5000.0)
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 100


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


def _book_journal() -> Journal:
    journal = Journal()
    journal.append(NOW, "fill", {"symbol": "TEST", "side": "buy", "price": 100.0,
                                "size": 10.0, "venue": "paper"})
    return journal


def test_chat_coach():
    reply = route_chat("coach me on my trading", "TEST", _seeded_store(), Journal(), CONFIG, NOW)
    assert "coach [rules]" in reply


def test_chat_portfolio_close():
    store = _seeded_store()
    store.save_bar("WIDE", Bar(interval="1d", timestamp_utc=NOW, open=80.0, high=81.0,
                               low=79.0, close=80.0, volume=100.0))
    journal = _book_journal()
    journal.append(NOW, "fill", {"symbol": "WIDE", "side": "buy", "price": 100.0,
                                "size": 10.0, "venue": "paper"})
    reply = route_chat("close my losers", "TEST", store, journal, CONFIG, NOW)
    assert "paper venue only" in reply and "WIDE" in reply


def test_chat_close_inside_brief_stays_brief():
    reply = route_chat("will TEST close above the flip", "TEST", _seeded_store(),
                       Journal(), CONFIG, NOW)
    assert "GEX brief" in reply


def test_chat_shock_news():
    reply = route_chat("shock TEST -2% why", "TEST", _seeded_store(), Journal(), CONFIG, NOW)
    assert "TEST shock" in reply and "news: enable live models" in reply


def test_chat_chart_and_doc_usage():
    store = _seeded_store()
    reply = route_chat("read this chart image", "TEST", store, Journal(), CONFIG, NOW)
    assert "/chartread" in reply
    reply = route_chat("read my document scan", "TEST", store, Journal(), CONFIG, NOW)
    assert "/docread" in reply


def test_chat_share_card():
    reply = route_chat("share card 0", "TEST", _seeded_store(), Journal(), CONFIG, NOW)
    assert "TEST short" in reply and "art: needs live models" in reply


def test_chat_tier_status():
    reply = route_chat("tier status", "TEST", _seeded_store(), Journal(), CONFIG, NOW)
    assert "spend" in reply and "local:" in reply


def test_tool_coach_review():
    reply = dispatch_tool("get_coach_review", {"live_models": False}, _seeded_store(),
                          Journal(), CONFIG, NOW, underlying="TEST")
    assert reply.ok and "coach [rules]" in reply.reply


def test_tool_portfolio_command_needs_command():
    reply = dispatch_tool("portfolio_command", {}, _seeded_store(), Journal(), CONFIG, NOW,
                          underlying="TEST")
    assert reply.ok and reply.reply == PORTFOLIO_USAGE


def test_tool_shock_news_needs_pct():
    reply = dispatch_tool("shock_news", {}, _seeded_store(), Journal(), CONFIG, NOW,
                          underlying="TEST")
    assert reply.ok and reply.reply == SHOCK_USAGE


def test_tool_chart_image_rejects_bad_base64():
    reply = dispatch_tool("read_chart_image", {"image_b64": "!!!"}, _seeded_store(),
                          Journal(), CONFIG, NOW, underlying="TEST")
    assert not reply.ok and "bad base64" in reply.error


def test_tool_document_rejects_bad_base64():
    reply = dispatch_tool("read_document", {"image_b64": "!!!"}, _seeded_store(),
                          Journal(), CONFIG, NOW, underlying="TEST")
    assert not reply.ok and "bad base64" in reply.error


def test_tool_share_card_bad_index():
    reply = dispatch_tool("share_card", {"index": 9}, _seeded_store(), Journal(), CONFIG, NOW,
                          underlying="TEST")
    assert not reply.ok and "out of range" in reply.error


def test_tool_narrated_alerts_empty_store():
    reply = dispatch_tool("get_narrated_alerts", {}, SnapshotStore(), Journal(), CONFIG, NOW,
                          underlying="TEST")
    assert reply.ok and reply.reply == "alerts: none"


def test_tool_tier_status():
    reply = dispatch_tool("get_tier_status", {}, _seeded_store(), Journal(), CONFIG, NOW,
                          underlying="TEST")
    assert reply.ok and "spend" in reply.reply


def test_slash_coach_tier_share_close():
    store = _seeded_store()
    assert "coach [rules]" in route_chat("/coach", "TEST", store, Journal(), CONFIG, NOW)
    assert "spend" in route_chat("/tier", "TEST", store, Journal(), CONFIG, NOW)
    assert "TEST short" in route_chat("/share 0", "TEST", store, Journal(), CONFIG, NOW)
    journal = _book_journal()
    reply = route_chat("/close my losers", "TEST", store, journal, CONFIG, NOW)
    assert "paper venue only" in reply


def test_slash_share_bad_index_usage():
    reply = route_chat("/share nine", "TEST", _seeded_store(), Journal(), CONFIG, NOW)
    assert reply == "usage: /share [index]"


def test_http_chartread_offline():
    response = route_http("POST", "/chartread?underlying=TEST&live_models=0", PNG,
                          _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    assert json.loads(response.body)["reply"] == "chart reading needs live models"


def test_http_docread_offline():
    response = route_http("POST", "/docread?underlying=TEST&live_models=0", PNG,
                          _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    assert json.loads(response.body)["reply"] == "document reading needs live models"


def test_http_chartread_rejects_empty():
    response = route_http("POST", "/chartread?underlying=TEST", b"", _seeded_store(),
                          Journal(), CONFIG, NOW)
    assert response.status == 400


def test_body_payload_caps_images():
    payload, error = _body_payload("/chartread", b"\x89PNG\r\n\x1a\n" + b"x" * (6 * 1024 * 1024))
    assert payload is None and error is not None and error.status == 413
    payload, error = _body_payload("/docread", PNG)
    assert error is None and payload == PNG


def test_http_share_page_and_errors():
    response = route_http("GET", "/share?underlying=TEST&index=0", {}, _seeded_store(),
                          Journal(), CONFIG, NOW)
    assert response.status == 200
    assert "<img" not in response.body.decode("utf-8")
    assert "needs live models" in response.body.decode("utf-8")
    store = SnapshotStore()
    response = route_http("GET", "/share?underlying=TEST&index=0", {}, store, Journal(),
                          CONFIG, NOW)
    assert response.status == 403
    response = route_http("GET", "/share?underlying=TEST&index=9", {}, _seeded_store(),
                          Journal(), CONFIG, NOW)
    assert response.status == 400
