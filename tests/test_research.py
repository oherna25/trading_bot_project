from datetime import datetime, timedelta, timezone

import pytest

from bot.diff import compact_dollars
from bot.exposure import compute_gex
from bot.research import format_brief, route_chat, scheduled_brief, wall_migration_text
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, GexProfile, OperatorConfig, OperatorProfile, OptionContract, Signal
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

CONFIG = OperatorConfig(
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
    store.save_profile(OperatorProfile(
        risk_tolerance="moderate", portfolio_size=50000.0, knowledge="intermediate", options_ok=True
    ))
    return store


def test_chat_brief_carries_profile_numbers():
    store = _seeded_store()
    reply = route_chat("give me the gex brief", "TEST", store, Journal(), CONFIG, NOW)
    assert "total exposure: -$104K" in reply
    assert "walls: 100 -$104K" in reply
    assert "strike 100.0:" not in reply
    assert "spot 102.00" in reply
    assert "spot 102.00 (quote 0m)" in reply
    assert "coverage 100%" in reply
    assert "calls $520K / puts -$624K" in reply
    assert "change vs previous: n/a (first snapshot)" in reply
    assert "direction: short confidence: 0.50 (regime fallback)" in reply


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
    assert "change vs previous: $624K" in second


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
    assert "total exposure: $1K" in vex
    assert "GEX brief" not in vex
    dex = route_chat("give me the dex brief", "TEST", store, journal, CONFIG, NOW)
    assert "DEX brief" in dex
    assert "total exposure: $1K" in dex
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


def test_trade_cards_answers_beat_ideas_ordering(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})
    store = _seeded_store()
    reply = route_chat("trade card ideas", "TEST", store, Journal(), CONFIG, NOW)
    assert reply.startswith("trade cards (2):")
    assert "short via" in reply
    assert "trade ideas (" not in reply


def _migration_store() -> SnapshotStore:
    store = SnapshotStore()
    for hour, oi in ((9, 1000.0), (14, 3000.0)):
        store.save(
            ChainSnapshot(
                underlying="TEST",
                timestamp_utc=NOW.replace(hour=hour, minute=30),
                source_name="tradier_sandbox",
                latency_ms=9.0,
                contracts_list=(
                    OptionContract(
                        strike=100.0, expiry="2026-11-20", call_or_put="call",
                        open_interest=oi, gamma=0.05, price=2.50, bid=2.40, ask=2.60, iv=0.30,
                    ),
                ),
            )
        )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=99.0, high=103.0, low=98.0, close=102.0, volume=1200.0))
    return store


def test_wall_migration_narrates_wall_growth():
    text = wall_migration_text("TEST", _migration_store(), NOW)
    assert "walls since the open:" in text
    assert "100: $520K -> $2M (+$1M)" in text
    assert "day exposure:" in text


def test_wall_migration_empty_with_one_snapshot():
    assert wall_migration_text("TEST", _seeded_store(), NOW) == ""


def test_brief_appends_migration_with_two_snapshots():
    reply = route_chat("gex brief", "TEST", _migration_store(), Journal(), CONFIG, NOW)
    assert "walls since the open:" in reply


def test_scheduled_brief_matches_stored_profile():
    store = _seeded_store()
    journal = Journal()
    reply = scheduled_brief("TEST", store, journal, NOW)
    snapshot_id, snapshot = store.read_latest("TEST")
    profile = compute_gex(snapshot, snapshot_id, spot=102.0)
    assert f"total exposure: {compact_dollars(profile.total_exposure)}" in reply
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


def test_brief_direction_matches_ideas_and_regime_without_flip():
    store = _seeded_store()
    journal = Journal()
    reply = route_chat("give me the gex brief", "TEST", store, journal, CONFIG, NOW)
    assert "direction: short" in reply
    assert "regime short-gamma" in reply
    ideas_reply = route_chat("show me trade ideas", "TEST", store, journal, CONFIG, NOW)
    assert "TEST short" in ideas_reply
    assert "TEST long" not in ideas_reply


def test_change_line_labels_negative_previous_direction():
    store = _seeded_store()
    journal = Journal()
    route_chat("gex brief", "TEST", store, journal, CONFIG, NOW)
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
    less = route_chat("gex brief", "TEST", store, journal, CONFIG, NOW + timedelta(hours=1))
    assert "change vs previous: $624K (sign flip)" in less
    store = _seeded_store()
    journal = Journal()
    route_chat("gex brief", "TEST", store, journal, CONFIG, NOW)
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW + timedelta(hours=1),
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="put", open_interest=1000.0,
                    gamma=0.03, price=3.00, bid=2.90, ask=3.10, iv=0.32,
                ),
            ),
        )
    )
    more = route_chat("gex brief", "TEST", store, journal, CONFIG, NOW + timedelta(hours=1))
    assert "change vs previous: -$208K (-200.0%, more negative)" in more


def _direct_brief(current_total, previous_total):
    snapshot = ChainSnapshot(
        underlying="TEST", timestamp_utc=NOW, source_name="tradier_sandbox",
        latency_ms=9.0, contracts_list=(),
    )
    signal = Signal(direction="short", thesis="t", confidence=0.5, snapshot_id="s")
    current = GexProfile(snapshot_id="s", per_strike_exposure={}, total_exposure=current_total, flip_point=None)
    previous = GexProfile(snapshot_id="p", per_strike_exposure={}, total_exposure=previous_total, flip_point=None)
    return format_brief(current, signal, snapshot, None, NOW, previous)


def test_change_line_marks_sign_flip_without_percent():
    assert "change vs previous: $154K (sign flip)" in _direct_brief(50000.0, -104040.0)
    assert "change vs previous: -$154K (sign flip)" in _direct_brief(-104040.0, 50000.0)


def test_change_line_keeps_percent_for_same_sign_negatives():
    assert "change vs previous: -$208K (-200.0%, more negative)" in _direct_brief(-312120.0, -104040.0)
    assert "change vs previous: $52K (+50.0%, less negative)" in _direct_brief(-52020.0, -104040.0)


def test_trade_cards_output_carries_reason_text(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})
    store = _seeded_store()
    reply = route_chat("show me trade cards", "TEST", store, Journal(), CONFIG, NOW)
    assert reply.startswith("trade cards (2):")
    assert "short via single" in reply
    assert "wall exposure" in reply


def test_wheel_answers_beat_ideas_ordering():
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(
                    strike=95.0, expiry="2026-11-13", call_or_put="put", open_interest=500.0,
                    delta=-0.20, bid=1.20, ask=1.30, iv=0.30,
                ),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0))
    reply = route_chat("wheel ideas", "TEST", store, Journal(), CONFIG, NOW)
    assert reply.startswith("wheel ideas (1):")
    assert "SELL_PUT" in reply
    assert "trade ideas (" not in reply


def _stale_spot_store():
    store = SnapshotStore()
    store.save(ChainSnapshot(
        underlying="TEST", timestamp_utc=NOW - timedelta(minutes=3),
        source_name="tradier_sandbox", latency_ms=9.0,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                           open_interest=1000.0, gamma=0.05),
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="put",
                           open_interest=2000.0, gamma=0.03),
        ),
    ))
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW - timedelta(minutes=59),
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    return store


def test_brief_flags_stale_quote():
    reply = route_chat("brief", "TEST", _stale_spot_store(), Journal(), CONFIG, NOW)
    assert "(quote 59m)" in reply
    assert "STALE -- refresh the quote before trading" in reply


def test_brief_fresh_quote_has_no_stale_flag():
    reply = route_chat("brief", "TEST", _seeded_store(), Journal(), CONFIG, NOW)
    assert "STALE" not in reply


def test_brief_carries_so_what_line():
    reply = route_chat("brief", "TEST", _seeded_store(), Journal(), CONFIG, NOW)
    assert "so what: short-gamma: dealers amplify moves" in reply
    assert "nearest magnet: 100" in reply


def test_vex_dex_briefs_carry_so_what_lines():
    store = _seeded_store()
    vex = route_chat("vex brief", "TEST", store, Journal(), CONFIG, NOW)
    dex = route_chat("dex brief", "TEST", store, Journal(), CONFIG, NOW)
    assert "so what: a 1pt IV rise is worth" in vex
    assert "so what: net " in dex and "share-equivalents" in dex
