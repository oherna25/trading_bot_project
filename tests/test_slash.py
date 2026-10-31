from datetime import datetime, timezone

import pytest

from bot.journal import Journal
from bot.muse import dispatch_tool, route_http
from bot.research import route_chat
from bot.shapes import (
    Bar,
    ChainSnapshot,
    Holding,
    OperatorConfig,
    OperatorProfile,
    OptionContract,
)
from bot.slash import SLASH_COMMANDS, commands_text, help_text, info_text, parse_slash, slash_response
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

CONFIG = OperatorConfig(
    max_day_loss=1000.0,
    day_profit_target=500.0,
    max_trade_size=5000.0,
)

PROFILE = OperatorProfile(
    risk_tolerance="moderate",
    portfolio_size=50000.0,
    knowledge="intermediate",
    options_ok=True,
)


@pytest.fixture(autouse=True)
def _no_reason_backend(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})
    monkeypatch.setattr("bot.ideas._read_dotenv", lambda *args, **kwargs: {})


def _seeded_store(with_profile: bool = True) -> SnapshotStore:
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
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    if with_profile:
        store.save_profile(PROFILE)
    return store


def _slash(text: str, store: SnapshotStore | None = None, underlying: str = "TEST") -> str:
    store = store if store is not None else _seeded_store()
    return slash_response(text, underlying, store, Journal(), CONFIG, NOW)


def test_parse_splits_name_and_rest_case_insensitive():
    assert parse_slash("/Cards  monthly ideas ") == ("cards", "monthly ideas")
    assert parse_slash("  /SHOCK -2%") == ("shock", "-2%")
    assert parse_slash("/brief") == ("brief", "")


def test_parse_requires_leading_slash():
    with pytest.raises(ValueError):
        parse_slash("brief")


def test_registry_covers_every_command_with_tool_and_usage():
    assert set(SLASH_COMMANDS) == {
        "profile", "dashboard", "alerts", "holdings", "brief", "cards",
        "wheel", "diff", "shock", "replay", "pnl", "help", "commands", "info",
        "coach", "tier", "share", "close",
    }
    for command in SLASH_COMMANDS.values():
        assert command.tool
        assert command.usage.startswith(f"/{command.name}")


def test_unknown_command_returns_help_listing_every_command():
    reply = _slash("/nope")
    assert reply == help_text()
    for command in SLASH_COMMANDS.values():
        assert command.usage in reply


def test_help_command_returns_help():
    assert _slash("/help") == help_text()


def test_commands_lists_every_command_with_description():
    reply = _slash("/commands")
    assert reply == commands_text()
    for command in SLASH_COMMANDS.values():
        assert command.usage in reply
    assert "/commands - list every command with descriptions" in reply
    assert "/shock <percent> - shock spot and reprice walls and cards" in reply


def test_info_explains_app_features():
    reply = _slash("/info")
    assert reply == info_text()
    for feature in (
        "briefs:", "cards:", "ideas:", "wheel:", "diff:", "shock:", "replay:",
        "paper:", "alerts:", "holdings:", "debate:", "profile:", "pages:",
        "voice:", "sessions:",
    ):
        assert feature in reply
    assert "DISCORD_WEBHOOK_URL" in reply
    assert "META_MODEL_API_KEY" in reply
    assert "POST /session" in reply


def test_profile_dispatch_reads_saved_profile():
    reply = _slash("/profile")
    assert "risk_tolerance=moderate" in reply
    assert "saved" not in reply


def test_dashboard_dispatch_shows_header_and_board_line():
    reply = _slash("/dashboard")
    assert "dashboard 2026-10-27" in reply
    assert "1 underlyings, 1 snapshots" in reply
    assert "TEST: short-gamma" in reply
    assert "wall 100 -$104K" in reply


def test_alerts_dispatch_reports_none_when_quiet():
    assert _slash("/alerts").startswith("alerts (0) at 2026-10-27T14:30:00+00:00")


def test_alerts_aggregates_every_underlying():
    store = SnapshotStore()
    for symbol, strike, spot in (("SPY", 100.0, 100.2), ("IWM", 200.0, 200.4)):
        store.save(
            ChainSnapshot(
                underlying=symbol,
                timestamp_utc=NOW,
                source_name="tradier_sandbox",
                latency_ms=9.0,
                contracts_list=(
                    OptionContract(
                        strike=strike, expiry="2026-11-20", call_or_put="call",
                        open_interest=5000.0, gamma=0.05, price=2.50,
                        bid=2.40, ask=2.60, iv=0.30,
                    ),
                ),
            )
        )
        store.save_bar(
            symbol,
            Bar(interval="1d", timestamp_utc=NOW, open=spot - 1.0, high=spot + 1.0,
                low=spot - 2.0, close=spot, volume=1000.0),
        )
    reply = _slash("/alerts", store=store)
    assert "ALERT: SPY" in reply
    assert "ALERT: IWM" in reply


def test_holdings_dispatch_reports_none_when_empty():
    reply = _slash("/holdings")
    assert reply.startswith("holdings: none saved")


def test_holdings_dispatch_reads_book():
    store = _seeded_store()
    store.save_holdings([Holding(symbol="TEST", kind="stock", side="long", strike=None, expiry=None, qty=10.0)])
    reply = _slash("/holdings", store)
    assert "your book" in reply


def test_brief_dispatch_returns_gex_brief():
    reply = _slash("/brief")
    assert "GEX brief" in reply
    assert "total exposure: -$104K" in reply


def test_cards_dispatch_returns_trade_cards():
    reply = _slash("/cards")
    assert reply.startswith("trade cards")
    assert "short via" in reply


def test_cards_dispatch_passes_trailing_query():
    reply = _slash("/cards monthly")
    assert reply.startswith("trade cards")


def test_wheel_dispatch_returns_wheel_screen():
    reply = _slash("/wheel")
    assert reply.startswith("wheel ideas")


def test_diff_dispatch_returns_snapshot_diff():
    reply = _slash("/diff")
    assert "diff TEST" in reply


def test_shock_dispatch_returns_shock_table():
    reply = _slash("/shock -2%")
    assert "TEST shock -2.0%" in reply


def test_shock_without_percent_returns_usage():
    reply = _slash("/shock")
    assert reply == "usage: /shock <percent>, e.g. /shock -2%"


def test_replay_dispatch_returns_replay_summary():
    reply = _slash("/replay")
    assert "replay TEST" in reply


def test_pnl_dispatch_returns_paper_pnl():
    reply = _slash("/pnl")
    assert "paper pnl" in reply


def test_tool_error_returns_failure_line():
    reply = _slash("/brief", _seeded_store(), underlying="MISSING")
    assert reply == (
        "/brief failed: no snapshot stored for MISSING (store has: TEST)"
        " -- say load MISSING and I'll fetch it"
    )


def test_route_chat_slash_profile_beats_questionnaire_keyword():
    reply = route_chat("/profile", "TEST", _seeded_store(), Journal(), CONFIG, NOW)
    assert "risk_tolerance=moderate" in reply
    assert "questionnaire" not in reply


def test_route_chat_plain_profile_still_returns_questionnaire():
    reply = route_chat("profile", "TEST", _seeded_store(), Journal(), CONFIG, NOW)
    assert "questionnaire" in reply


def test_dashboard_route_returns_all_sections():
    response = route_http("GET", "/dashboard?underlying=TEST", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    assert response.content_type == "text/html; charset=utf-8"
    body = response.body.decode("utf-8")
    assert "<h2>Digest</h2>" in body
    assert "<h2>Top card</h2>" in body
    assert "<h2>Holdings</h2>" in body
    assert "dashboard 2026-10-27" in body
    assert "trade cards" in body
    assert "holdings: none saved" in body
    assert 'href="/chart?underlying=TEST"' in body
    assert 'href="/board"' in body
    assert 'href="/cards?underlying=TEST"' in body
    assert "<th>Symbol</th><th>Regime</th><th>Spot</th><th>Flip</th><th>Wall</th>" in body
    assert "<td>TEST</td>" in body
    assert '<span class="short">short-gamma</span>' in body
    assert "<span>underlyings</span>" in body
    assert "<span>active alerts</span>" in body
    assert "<span>holdings</span>" in body
    assert "<dt>Confidence</dt>" in body
    assert "<dt>Reason</dt>" in body
    assert "/card?underlying=TEST&amp;snapshot=" in body
    assert 'http-equiv="refresh" content="60"' in body
    assert "background:#0d1117" in body


def test_dashboard_route_empty_store_returns_400():
    response = route_http("GET", "/dashboard", {}, SnapshotStore(), Journal(), CONFIG, NOW)
    assert response.status == 400
    assert "no snapshots stored" in response.body.decode("utf-8")


def test_new_tools_dispatch_directly():
    store = _seeded_store(with_profile=False)
    journal = Journal()
    empty_profile = dispatch_tool("get_profile", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert empty_profile.ok is True
    assert empty_profile.reply == 'no profile is saved; chat "profile" to answer the questionnaire'
    store.save_profile(PROFILE)
    profile = dispatch_tool("get_profile", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert profile.ok is True
    assert "risk_tolerance=moderate" in profile.reply
    assert "saved" not in profile.reply
    dashboard = dispatch_tool("get_dashboard", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert dashboard.ok is True
    assert "dashboard 2026-10-27" in dashboard.reply
    alerts = dispatch_tool("get_alerts", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert alerts.ok is True
    assert alerts.reply.startswith("alerts (0) at 2026-10-27T14:30:00+00:00")
    holdings = dispatch_tool("get_holdings", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert holdings.ok is True
    assert holdings.reply.startswith("holdings: none saved")
