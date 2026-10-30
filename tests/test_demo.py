from datetime import datetime, timedelta, timezone
from urllib.parse import quote as urlencode

import pytest

from bot.demo import demo_cards, demo_html
from bot.journal import Journal
from bot.muse import route_http
from bot.shapes import Bar, ChainSnapshot, OperatorProfile, OptionContract, default_operator_config
from bot.store import SnapshotStore
from bot.tradecards import bar_at_or_before

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()


@pytest.fixture(autouse=True)
def _no_reason_backend(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})


def _contract(strike, side, oi, gamma):
    return OptionContract(
        strike=strike, expiry="2026-11-20", call_or_put=side, open_interest=oi,
        gamma=gamma, price=2.50, bid=2.40, ask=2.60, iv=0.30,
    )


def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    for day in (0, 1):
        stamp = NOW - timedelta(days=1 - day)
        store.save(
            ChainSnapshot(
                underlying="TEST",
                timestamp_utc=stamp,
                source_name="tradier_sandbox",
                latency_ms=9.0,
                contracts_list=(
                    _contract(95.0, "put", 3000.0, 0.05),
                    _contract(105.0, "call", 3000.0, 0.05),
                ),
            )
        )
        store.save_bar(
            "TEST",
            Bar(interval="1d", timestamp_utc=stamp,
                open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0),
        )
    store.save_profile(OperatorProfile(
        risk_tolerance="moderate", portfolio_size=50000.0, knowledge="intermediate", options_ok=True
    ))
    return store


def test_demo_page_walks_all_four_sections():
    page = demo_html("TEST", _seeded_store(), Journal(), NOW)
    assert page.startswith("<!doctype html>")
    assert "1. Regime read" in page
    assert "2. Top card" in page
    assert "3. Wheel screen" in page
    assert "4. Replay" in page
    assert "/card?underlying=TEST" in page


def test_demo_page_journals_no_fills():
    journal = Journal()
    demo_html("TEST", _seeded_store(), journal, NOW)
    assert [entry.kind for entry in journal.entries()].count("fill") == 0


def test_demo_route_and_root_serve_and_400_unknown():
    for path in ("/demo?underlying=TEST", "/?underlying=TEST"):
        response = route_http("GET", path, {}, _seeded_store(), Journal(), CONFIG, NOW)
        assert response.status == 200
        assert "TEST demo" in response.body.decode("utf-8")
    missing = route_http("GET", "/demo?underlying=NOPE", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert missing.status == 400


def test_card_permalink_renders_single_indexed_card():
    store = _seeded_store()
    snapshot_id, _ = store.read_latest("TEST")
    response = route_http(
        "GET", f"/card?underlying=TEST&snapshot={urlencode(snapshot_id)}&index=0",
        {}, store, Journal(), CONFIG, NOW,
    )
    assert response.status == 200
    assert response.body.decode("utf-8").count("<article>") == 1
    assert "TEST trade cards" in response.body.decode("utf-8")


def test_card_permalink_defaults_to_latest_and_validates():
    store = _seeded_store()
    default = route_http("GET", "/card?underlying=TEST", {}, store, Journal(), CONFIG, NOW)
    assert default.status == 200
    assert default.body.decode("utf-8").count("<article>") == 1
    bad_index = route_http(
        "GET", "/card?underlying=TEST&index=99", {}, store, Journal(), CONFIG, NOW
    )
    assert bad_index.status == 400
    bad_snapshot = route_http(
        "GET", "/card?underlying=TEST&snapshot=nope", {}, store, Journal(), CONFIG, NOW
    )
    assert bad_snapshot.status == 400
    non_integer = route_http(
        "GET", "/card?underlying=TEST&index=x", {}, store, Journal(), CONFIG, NOW
    )
    assert non_integer.status == 400


def _reason_stub(url, headers, payload, timeout):
    del url, headers, payload, timeout
    return {
        "choices": [{"message": {"content": "Model says go."}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def test_demo_cards_honors_passed_reason_env():
    _, cards = demo_cards(
        "TEST",
        _seeded_store(),
        Journal(),
        NOW,
        env={"META_MODEL_API_KEY": "key-1"},
        http_post=_reason_stub,
    )
    assert cards
    assert {card.reason_source for card in cards} == {"meta"}
    assert {card.reason for card in cards} == {"Model says go."}


def test_demo_cards_pins_old_snapshot_bar():
    store = _seeded_store()
    first_id = store.snapshots_in_order()[0][0]
    _, cards = demo_cards("TEST", store, Journal(), NOW, first_id)
    assert cards and all(card.snapshot_id == first_id for card in cards)
    bar = bar_at_or_before(store, "TEST", NOW - timedelta(days=1))
    assert bar is not None and bar.timestamp_utc == NOW - timedelta(days=1)
