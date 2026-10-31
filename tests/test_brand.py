from datetime import date, datetime, timedelta, timezone

import pytest

from bot.journal import Journal
from bot.muse import dispatch_tool, openapi_spec, route_http
from bot.shapes import Bar, ChainSnapshot, OperatorProfile, OptionContract, default_operator_config
from bot.store import SnapshotStore
from bot.tradecards import PROFILE_REQUIRED

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()


@pytest.fixture(autouse=True)
def _no_reason_backend(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})


def _seeded_store(with_profile=True) -> SnapshotStore:
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
    if with_profile:
        store.save_profile(OperatorProfile(risk_tolerance="moderate", portfolio_size=50000.0,
                                           knowledge="intermediate", options_ok=True))
    return store


def test_brand_constant_is_equalizer():
    from bot.theme import BRAND

    assert BRAND == "MarketwingmanBot"


def test_brand_marks_page_title_nav_and_demo_heading():
    from bot.theme import BRAND

    response = route_http("GET", "/demo?underlying=TEST", {}, _seeded_store(),
                          Journal(), CONFIG, NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert f"<title>TEST demo | {BRAND}</title>" in body
    assert "TEST demo" in body
    assert BRAND in body.split("<h1>")[1].split("</h1>")[0]
    assert f">{BRAND}</a>" in body


def test_openapi_title_uses_brand():
    from bot.theme import BRAND

    assert openapi_spec()["info"]["title"] == BRAND


def test_earnings_rows_uses_stored_dates_only():
    from bot.earnings import earnings_rows

    store = _seeded_store()
    store.save_earnings("TEST", (NOW.date() + timedelta(days=5)).isoformat())
    store.save_earnings("OLD", (NOW.date() - timedelta(days=1)).isoformat())
    store.save_holdings([])

    rows = earnings_rows(store, NOW)
    assert [row["symbol"] for row in rows] == ["TEST"]
    row = rows[0]
    assert row["days_out"] == 5
    assert row["regime"] in ("long-gamma", "short-gamma")
    assert row["wall_strike"] == pytest.approx(100.0)
    assert "wall" in row and row["spot"] == pytest.approx(102.0)


def test_earnings_rows_covers_holdings_without_snapshots():
    from bot.earnings import earnings_rows
    from bot.holdings import save_holdings_shapes

    store = _seeded_store()
    save_holdings_shapes(
        [{"symbol": "HELD", "kind": "stock", "side": "long", "qty": 10}],
        store, Journal(), NOW,
    )
    store.save_earnings("HELD", (NOW.date() + timedelta(days=3)).isoformat())
    rows = earnings_rows(store, NOW)
    assert [row["symbol"] for row in rows] == ["HELD"]
    assert rows[0]["held"] is True
    assert rows[0]["regime"] == "unknown"


def test_earnings_rows_makes_no_live_calls(monkeypatch):
    from bot.earnings import earnings_rows

    def explode(*args, **kwargs):
        raise AssertionError("earnings rows must not call out")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    monkeypatch.setenv("FINNHUB_API_KEY", "key-1")
    store = _seeded_store()
    store.save_earnings("TEST", (NOW.date() + timedelta(days=5)).isoformat())
    assert len(earnings_rows(store, NOW)) == 1


def test_earnings_page_renders_row_and_nav_link():
    from bot.theme import BRAND

    store = _seeded_store()
    store.save_earnings("TEST", (NOW.date() + timedelta(days=5)).isoformat())
    response = route_http("GET", "/earnings", {}, store, Journal(), CONFIG, NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert "TEST" in body and "5 days out" in body
    assert "$" in body
    assert 'href="/earnings"' in body
    assert BRAND in body


def test_earnings_tool_matches_page():
    store = _seeded_store()
    store.save_earnings("TEST", (NOW.date() + timedelta(days=5)).isoformat())
    reply = dispatch_tool("get_earnings_radar", {}, store, Journal(), CONFIG, NOW,
                          underlying="TEST")
    assert reply.ok
    assert "TEST" in reply.reply and "5 days out" in reply.reply
    assert "$" in reply.reply


def test_cards_page_offers_one_tap_paper_trade():
    response = route_http("GET", "/cards?underlying=TEST", {}, _seeded_store(),
                          Journal(), CONFIG, NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert "take this trade (paper)" in body
    assert 'name="tool" value="take_paper_trade"' in body


def test_demo_top_card_offers_one_tap_paper_trade():
    response = route_http("GET", "/demo?underlying=TEST", {}, _seeded_store(),
                          Journal(), CONFIG, NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert "take this trade (paper)" in body
    assert 'name="tool" value="take_paper_trade"' in body


def test_take_paper_trade_fills_and_pnl_reflects_it():
    store = _seeded_store()
    journal = Journal()
    reply = dispatch_tool("take_paper_trade", {"index": 0}, store, journal, CONFIG, NOW,
                          underlying="TEST")
    assert reply.ok
    assert reply.reply.startswith("paper trade: ")
    assert "TEST" in reply.reply
    assert journal.entries()[-1].kind == "fill"
    pnl = dispatch_tool("paper_pnl", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert "TEST: size" in pnl.reply
    page = route_http("GET", "/pnl?underlying=TEST", {}, store, journal, CONFIG, NOW)
    assert page.status == 200
    assert "TEST: size" in page.body.decode("utf-8")


def test_take_paper_trade_posts_through_action_route():
    store = _seeded_store()
    journal = Journal()
    response = route_http(
        "POST", "/action",
        {"tool": "take_paper_trade", "underlying": "TEST", "index": "0"},
        store, journal, CONFIG, NOW,
    )
    assert response.status == 200
    assert "paper trade:" in response.body.decode("utf-8")
    assert journal.entries()[-1].kind == "fill"


def test_take_paper_trade_needs_profile_with_loud_hint():
    reply = dispatch_tool("take_paper_trade", {"index": 0}, _seeded_store(with_profile=False),
                          Journal(), CONFIG, NOW, underlying="TEST")
    assert reply.ok
    assert PROFILE_REQUIRED in reply.reply
    assert "next:" in reply.reply


def test_take_paper_trade_rejects_bad_index_loudly():
    reply = dispatch_tool("take_paper_trade", {"index": 9}, _seeded_store(),
                          Journal(), CONFIG, NOW, underlying="TEST")
    assert not reply.ok
    assert "out of range" in reply.error
    assert "next:" in reply.error


def test_share_page_carries_brand_trade_and_demo_link():
    from bot.theme import BRAND

    response = route_http("GET", "/share?underlying=TEST&index=0", {}, _seeded_store(),
                          Journal(), CONFIG, NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert BRAND in body
    assert "TEST short" in body
    assert 'href="/demo?underlying=TEST"' in body


def test_share_text_carries_brand_and_demo_link():
    from bot.cards import share_card
    from bot.theme import BRAND

    text = share_card(0, "TEST", _seeded_store(), Journal(), NOW)
    assert BRAND in text
    assert "TEST short" in text
    assert "/demo?underlying=TEST" in text


def test_earnings_survive_a_bundle_round_trip(tmp_path):
    from bot.persist import load_bundle, save_bundle

    day = (NOW.date() + timedelta(days=5)).isoformat()
    store = _seeded_store()
    store.save_earnings("TEST", day)
    save_bundle(str(tmp_path), store, Journal())
    fresh, _ = load_bundle(str(tmp_path))
    assert fresh.read_earnings("TEST") == date.fromisoformat(day)
