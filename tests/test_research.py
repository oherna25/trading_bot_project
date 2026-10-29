from datetime import datetime, timedelta, timezone

import pytest

from bot.dashboard import dashboard_view
from bot.exposure import compute_gex
from bot.research import build_dashboard_link, route_chat, scheduled_brief
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OperatorConfig, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

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


def test_chat_brief_carries_profile_numbers():
    store = _seeded_store()
    reply = route_chat("give me the gex brief", "TEST", store, Journal(), CONFIG, NOW)
    assert "total exposure: $-104,040" in reply
    assert "walls: 100 $-104,040" in reply
    assert "strike 100.0:" not in reply
    assert "spot 102.00" in reply
    assert "coverage 100%" in reply
    assert "calls $520,200 / puts $-624,240" in reply
    assert "change vs previous: n/a (first snapshot)" in reply
    assert "direction: flat" in reply


def test_chat_brief_shows_change_vs_previous_snapshot():
    store = _seeded_store()
    journal = Journal()
    first = route_chat("gex brief", "TEST", store, journal, CONFIG, NOW)
    assert "change vs previous: n/a" in first
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW + timedelta(hours=1),
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=1000.0,
                    gamma=0.05, price=2.50, bid=2.40, ask=2.60, iv=0.30,
                ),
            ),
        )
    )
    second = route_chat("gex brief", "TEST", store, journal, CONFIG, NOW + timedelta(hours=1))
    assert "change vs previous: $624,240" in second


def test_vex_and_dex_requests_return_their_own_briefs():
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                               open_interest=100.0, gamma=0.05, vega=0.20, delta=0.55),
                OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="put",
                               open_interest=100.0, gamma=0.03, vega=0.10, delta=-0.45),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    journal = Journal()
    vex = route_chat("give me the vex brief", "TEST", store, journal, CONFIG, NOW)
    assert "VEX brief" in vex
    assert "total exposure: 1000.0" in vex
    assert "GEX brief" not in vex
    dex = route_chat("give me the dex brief", "TEST", store, journal, CONFIG, NOW)
    assert "DEX brief" in dex
    assert "total exposure: 1000.0" in dex
    assert "GEX brief" not in dex


def test_paper_pnl_marks_journaled_fills():
    from bot.research import paper_pnl_text
    from bot.risk import build_intent, submit_paper
    from bot.shapes import RiskLimits, RiskState, Signal

    store = _seeded_store()
    journal = Journal()
    state = RiskState(account_size=100000.0, risk_per_trade=0.01, day_pnl=0.0,
                      halt_active=False, kill_active=False)
    limits = RiskLimits(max_trade_size=5000.0, day_profit_target=500.0, max_day_loss=1000000.0)
    signal = Signal(direction="long", thesis="t", confidence=0.6, snapshot_id="TEST:snap-1")
    intent = build_intent(signal, "sig-1", state, journal, NOW)
    submit_paper(intent, 100.0, state, None, limits, journal, NOW, ask=100.0)
    text = paper_pnl_text(journal, store)
    assert "TEST: size 1000 avg 100.00 mark 102.00 pnl +2000.00" in text
    assert "total paper pnl: +2000.00" in text


def test_dashboard_request_returns_pinned_link():
    store = _seeded_store()
    journal = Journal()
    reply = route_chat("show me the dashboard", "TEST", store, journal, CONFIG, NOW)
    assert reply.startswith("dashboard for snapshot TEST:")
    assert "https://ops.example.internal/dashboard/TEST%3A2026-10-27T14%3A30%3A00%2B00%3A00" in reply
    assert journal.entries()[-1].kind == "dashboard_link_sent"


def test_dashboard_link_uses_config_host_and_expiry():
    link = build_dashboard_link("TEST:snap-9", CONFIG, NOW)
    assert link.url == "https://ops.example.internal/dashboard/TEST%3Asnap-9"
    assert link.expires_utc == NOW + timedelta(seconds=3600)


def test_scheduled_brief_matches_stored_profile():
    store = _seeded_store()
    journal = Journal()
    reply = scheduled_brief("TEST", store, journal, NOW)
    snapshot_id, snapshot = store.read_latest("TEST")
    profile = compute_gex(snapshot, snapshot_id, spot=102.0)
    assert f"total exposure: ${profile.total_exposure:,.0f}" in reply
    assert "(age 0m, source tradier_sandbox)" in reply
    assert journal.entries()[-1].kind == "brief_sent"


def test_scheduled_brief_alerts_on_regime_flip_and_stale_snapshot():
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW - timedelta(days=2),
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="put",
                               open_interest=1000.0, gamma=0.05),
            ),
        )
    )
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW - timedelta(days=1),
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                               open_interest=1000.0, gamma=0.05),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW - timedelta(days=1),
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    journal = Journal()
    reply = scheduled_brief("TEST", store, journal, NOW)
    assert "ALERT: snapshot is stale" in reply
    assert "ALERT: regime flipped short-gamma -> long-gamma" in reply
    assert [e.kind for e in journal.entries() if e.kind == "alert"] == ["alert", "alert"]


def test_dashboard_numbers_match_chat_numbers_on_same_snapshot():
    store = _seeded_store()
    journal = Journal()
    chat_reply = route_chat("gex brief please", "TEST", store, journal, CONFIG, NOW)
    snapshot_id, snapshot = store.read_latest("TEST")
    profile = compute_gex(snapshot, snapshot_id, spot=102.0)
    signal_entry = next(e for e in journal.entries() if e.kind == "signal")
    assert signal_entry.payload["snapshot_id"] == snapshot_id
    from bot.research import handle_chat_query
    _, signal = handle_chat_query("gex brief please", "TEST", store, journal, NOW)
    screen = dashboard_view(snapshot_id, signal, store, journal, [])
    assert f"total exposure: ${profile.total_exposure:,.0f}" in chat_reply
    assert screen.header.price == pytest.approx(102.0)
    assert screen.header.day_change == pytest.approx(2.0)
    assert screen.header.status == signal.status
    assert screen.grid.conviction == pytest.approx(signal.confidence)
    assert screen.grid.trigger == signal.trigger
    assert screen.options.qualifying_count == 2
    assert screen.options.regime == "short-gamma"
    assert screen.card.open_interest == pytest.approx(2000.0)
    assert screen.card.spread_pct == pytest.approx((3.10 - 2.90) / 3.00 * 100.0)
