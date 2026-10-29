import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest

from bot.journal import Journal
from bot.muse import resolve_port, route_http
from bot.research import build_dashboard_link
from bot.shapes import Bar, ChainSnapshot, OperatorConfig, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
SNAPSHOT_ID = "TEST:2026-10-27T14:30:00+00:00"
QUOTED_ID = "TEST%3A2026-10-27T14%3A30%3A00%2B00%3A00"

CONFIG = OperatorConfig(
    dashboard_base_url="https://ops.example.internal",
    dashboard_link_expiry_seconds=3600,
    max_day_loss=1000.0,
    day_profit_target=500.0,
    max_trade_size=5000.0,
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
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=1000.0,
                    gamma=0.05, price=2.50, bid=2.40, ask=2.60, iv=0.30,
                ),
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="put", open_interest=2000.0,
                    gamma=0.03, price=3.00, bid=2.90, ask=3.10, iv=0.32,
                ),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW - timedelta(days=1),
                               open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0))
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    return store


def _json_body(response):
    assert response.content_type == "application/json"
    return json.loads(response.body.decode("utf-8"))


def test_dashboard_html_returns_page():
    response = route_http("GET", f"/dashboard/{QUOTED_ID}", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    assert response.content_type == "text/html; charset=utf-8"
    text = response.body.decode("utf-8")
    assert text.startswith("<!doctype html>")
    assert "TEST" in text


def test_dashboard_json_returns_screen_keys():
    response = route_http("GET", f"/dashboard/{QUOTED_ID}.json", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    body = _json_body(response)
    assert set(body) == {"header", "strip", "grid", "options", "card", "positions", "journal_view"}
    assert body["header"]["price"] == pytest.approx(102.0)
    assert body["options"]["qualifying_count"] == 2


def test_dashboard_unknown_snapshot_returns_404():
    missing = quote("TEST:2026-01-01T00:00:00+00:00", safe="")
    response = route_http("GET", f"/dashboard/{missing}", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 404
    response = route_http("GET", f"/dashboard/{missing}.json", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 404


def test_dashboard_malformed_id_returns_404():
    for rest in ("not-an-id", "TEST:not-a-time", "", "TEST:", ":2026-01-01", "/dashboard/"):
        response = route_http("GET", f"/dashboard/{rest}", {}, _seeded_store(), Journal(), CONFIG, NOW)
        assert response.status == 404


def test_dashboard_raw_plus_offset_survives():
    response = route_http("GET", f"/dashboard/{SNAPSHOT_ID}", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200


def test_dashboard_head_matches_get():
    for path in (f"/dashboard/{QUOTED_ID}", f"/dashboard/{QUOTED_ID}.json", "/healthz"):
        get = route_http("GET", path, {}, _seeded_store(), Journal(), CONFIG, NOW)
        head = route_http("HEAD", path, {}, _seeded_store(), Journal(), CONFIG, NOW)
        assert head.status == get.status
        assert head.content_type == get.content_type
        assert head.body == get.body


def test_healthz_returns_ok():
    response = route_http("GET", "/healthz", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    assert _json_body(response) == {"ok": True}


def test_resolve_port_precedence(monkeypatch):
    monkeypatch.setenv("PORT", "9002")
    assert resolve_port(9001) == 9001
    assert resolve_port(None) == 9002
    monkeypatch.delenv("PORT")
    assert resolve_port(None) == 8000
    monkeypatch.setenv("PORT", "not-a-port")
    with pytest.raises(ValueError):
        resolve_port(None)


def test_minted_link_quotes_snapshot_id():
    link = build_dashboard_link(SNAPSHOT_ID, CONFIG, NOW)
    assert link.url == f"https://ops.example.internal/dashboard/{QUOTED_ID}"


def test_minted_link_dereferences_through_route():
    link = build_dashboard_link(SNAPSHOT_ID, CONFIG, NOW)
    path = link.url.removeprefix(CONFIG.dashboard_base_url)
    response = route_http("GET", path, {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200


def test_pinned_numbers_equal_chat_numbers_on_same_snapshot():
    store = _seeded_store()
    journal = Journal()
    chat = route_http(
        "POST", "/chat",
        {"text": "give me the gex brief", "underlying": "TEST"},
        store, journal, CONFIG, NOW,
    )
    reply = _json_body(chat)["reply"]
    assert "total exposure: $-104,040" in reply
    screen = route_http("GET", f"/dashboard/{QUOTED_ID}.json", {}, store, journal, CONFIG, NOW)
    body = _json_body(screen)
    assert body["header"]["price"] == pytest.approx(102.0)
    assert body["header"]["day_change"] == pytest.approx(2.0)
    assert body["grid"]["trigger"] in reply
    assert body["header"]["status"] in reply
    assert body["options"]["regime"] == "short-gamma"
    assert body["card"]["open_interest"] == pytest.approx(2000.0)


def test_dashboard_html_escapes_journal_json_keeps_fidelity():
    store = _seeded_store()
    journal = Journal()
    journal.append(NOW, "note", {"text": "<script>alert(1)</script>"})
    html = route_http("GET", f"/dashboard/{QUOTED_ID}", {}, store, journal, CONFIG, NOW)
    text = html.body.decode("utf-8")
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    doc = route_http("GET", f"/dashboard/{QUOTED_ID}.json", {}, store, journal, CONFIG, NOW)
    payloads = [entry["payload"] for entry in _json_body(doc)["journal_view"]]
    assert {"text": "<script>alert(1)</script>"} in payloads


def test_expired_dashboard_link_returns_410():
    store = _seeded_store()
    journal = Journal()
    journal.append(NOW - timedelta(seconds=7200), "dashboard_link_sent", {"snapshot_id": SNAPSHOT_ID})
    fresh = route_http("GET", f"/dashboard/{QUOTED_ID}", {}, store, journal, CONFIG, NOW - timedelta(seconds=7199))
    assert fresh.status == 200
    stale = route_http("GET", f"/dashboard/{QUOTED_ID}", {}, store, journal, CONFIG, NOW)
    assert stale.status == 410
    assert _json_body(stale)["error"] == "dashboard link expired"
    stale_json = route_http("GET", f"/dashboard/{QUOTED_ID}.json", {}, store, journal, CONFIG, NOW)
    assert stale_json.status == 410


def test_top_contract_skips_far_otm_wide_spread_strike():
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="SPY",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(
                    strike=570.0, expiry="2026-10-30", call_or_put="put", open_interest=151574.0,
                    gamma=0.001, delta=0.0, price=0.50, bid=0.10, ask=0.60,
                ),
                OptionContract(
                    strike=770.0, expiry="2026-10-30", call_or_put="put", open_interest=5000.0,
                    gamma=0.05, delta=-0.45, price=5.00, bid=4.90, ask=5.10,
                ),
            ),
        )
    )
    store.save_bar("SPY", Bar(interval="1d", timestamp_utc=NOW,
                               open=765.0, high=772.0, low=764.0, close=769.0, volume=1.0))
    quoted = quote("SPY:2026-10-27T14:30:00+00:00", safe="")
    body = _json_body(route_http("GET", f"/dashboard/{quoted}.json", {}, store, Journal(), CONFIG, NOW))
    assert body["options"]["qualifying_count"] == 1
    assert body["card"]["open_interest"] == pytest.approx(5000.0)
    assert body["card"]["header"].startswith("770.0")


def test_header_day_change_uses_single_bar_open_to_close():
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=1000.0,
                    gamma=0.05, price=2.50, bid=2.40, ask=2.60,
                ),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    body = _json_body(route_http("GET", f"/dashboard/{QUOTED_ID}.json", {}, store, Journal(), CONFIG, NOW))
    assert body["header"]["day_change"] == pytest.approx(2.0)


def test_dashboard_serves_write_nothing_to_journal():
    store = _seeded_store()
    journal = Journal()
    route_http("POST", "/chat", {"text": "gex brief", "underlying": "TEST"}, store, journal, CONFIG, NOW)
    before = len(journal.entries())
    assert before > 0
    for path in (f"/dashboard/{QUOTED_ID}", f"/dashboard/{QUOTED_ID}.json"):
        response = route_http("GET", path, {}, store, journal, CONFIG, NOW)
        assert response.status == 200
    assert len(journal.entries()) == before
