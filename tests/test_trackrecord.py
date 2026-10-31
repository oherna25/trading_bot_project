from datetime import datetime, timedelta, timezone

import pytest

from bot.journal import Journal
from bot.muse import dispatch_tool, route_http
from bot.shapes import Bar, ChainSnapshot, OperatorProfile, OptionContract, default_operator_config
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()


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


def _round_trip_journal(store: SnapshotStore) -> Journal:
    journal = Journal()
    snapshot_id = store.read_latest("TEST")[0]
    journal.append(NOW, "fill", {"symbol": "TEST", "side": "buy", "price": 100.0,
                                "size": 10.0, "venue": "paper",
                                "card_snapshot": snapshot_id, "card_index": 0})
    journal.append(NOW + timedelta(hours=1), "fill", {"symbol": "TEST", "side": "sell",
                                                     "price": 110.0, "size": 10.0,
                                                     "venue": "paper"})
    return journal


def test_closed_trades_pair_fifo_and_win_rate():
    from bot.trackrecord import closed_trades, win_rate

    journal = _round_trip_journal(_seeded_store())
    trades = closed_trades(journal)
    assert len(trades) == 1
    assert trades[0]["symbol"] == "TEST"
    assert trades[0]["pnl"] == pytest.approx(100.0)
    assert win_rate(trades) == pytest.approx(1.0)


def test_trackrecord_text_names_win_rate_and_card_link():
    from bot.trackrecord import trackrecord_text

    store = _seeded_store()
    journal = _round_trip_journal(store)
    text = trackrecord_text(journal, store)
    assert "win rate" in text
    assert "100%" in text or "1/1" in text
    assert "/card?underlying=TEST" in text
    assert "$" in text


def test_trackrecord_text_links_board_when_no_card():
    from bot.trackrecord import trackrecord_text

    store = _seeded_store()
    journal = Journal()
    journal.append(NOW, "fill", {"symbol": "TEST", "side": "buy", "price": 100.0,
                                "size": 5.0, "venue": "paper"})
    journal.append(NOW + timedelta(hours=1), "fill", {"symbol": "TEST", "side": "sell",
                                                     "price": 101.0, "size": 5.0,
                                                     "venue": "paper"})
    text = trackrecord_text(journal, store)
    assert "/board" in text
    assert "/card?" not in text


def test_trackrecord_html_has_inline_svg_and_links():
    from bot.trackrecord import trackrecord_html

    store = _seeded_store()
    journal = _round_trip_journal(store)
    html = trackrecord_html(journal, store, "TEST", NOW)
    assert "<svg" in html
    assert "win rate" in html.lower()
    assert "/card?underlying=TEST" in html
    assert "Purple" not in html and "purple" not in html
    assert "indigo" not in html.lower()
    assert "linear-gradient" not in html


def test_take_card_trade_stamps_fill_with_card_ref():
    store = _seeded_store()
    journal = Journal()
    reply = dispatch_tool("take_paper_trade", {"index": 0}, store, journal, CONFIG, NOW,
                          underlying="TEST")
    assert reply.ok
    fills = [e for e in journal.entries() if e.kind == "fill"]
    assert fills
    assert "card_snapshot" in fills[-1].payload
    assert fills[-1].payload["card_index"] == 0


def test_trackrecord_tool_and_route_agree():
    store = _seeded_store()
    journal = _round_trip_journal(store)
    reply = dispatch_tool("get_trackrecord", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert reply.ok
    assert "win rate" in reply.reply
    response = route_http("GET", "/trackrecord?underlying=TEST", {}, store, journal, CONFIG, NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert "<svg" in body
    assert "win rate" in body.lower()


def test_dashboard_links_trackrecord():
    response = route_http("GET", "/dashboard?underlying=TEST", {}, _seeded_store(),
                          Journal(), CONFIG, NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert "/trackrecord" in body


def _stored_week() -> SnapshotStore:
    store = SnapshotStore()
    day = datetime(2026, 10, 19, 14, 30, tzinfo=timezone.utc)
    for offset in range(3):
        store.save(ChainSnapshot(
            underlying="TEST", timestamp_utc=day + timedelta(days=offset),
            source_name="marketdata_eod", latency_ms=5.0,
            contracts_list=(
                OptionContract(strike=90.0, expiry="2026-11-20", call_or_put="call",
                               open_interest=1000.0, gamma=0.05),
                OptionContract(strike=110.0, expiry="2026-11-20", call_or_put="put",
                               open_interest=2000.0, gamma=0.03),
            ),
        ))
        close = 100.0 + offset * 4.0
        store.save_bar("TEST", Bar(interval="1d", timestamp_utc=day + timedelta(days=offset),
                                  open=close, high=close, low=close, close=close, volume=1000.0))
        spy = 500.0 + offset * 5.0
        store.save_bar("SPY", Bar(interval="1d", timestamp_utc=day + timedelta(days=offset),
                                 open=spy, high=spy, low=spy, close=spy, volume=2000.0))
    return store


def test_replay_states_defaults_and_costs():
    from bot.replay import replay_report

    text, fills, summary = replay_report("TEST", _stored_week())
    assert fills
    assert "trade log:" in text
    assert "clean days" in text
    assert "distinct days" in text
    assert "defaults" in text
    assert "commission" in text.lower()
    assert "slippage" in text.lower()
    assert "costs:" in text.lower()
    assert summary["commission_per_fill"] == pytest.approx(0.0)
    assert summary["slippage_bps"] == pytest.approx(0.0)


def test_replay_benchmark_sits_next_to_pnl():
    from bot.replay import replay_report

    text, _, summary = replay_report("TEST", _stored_week())
    assert "buy-and-hold SPY" in text
    assert "benchmark" in text.lower()
    assert "spy_benchmark_pnl" in summary


def test_replay_custom_costs_reduce_pnl():
    from bot.replay import replay_report

    base_text, _, base = replay_report("TEST", _stored_week())
    assert "trade log:" in base_text
    _, _, costly = replay_report("TEST", _stored_week(), commission_per_fill=5.0,
                                 slippage_bps=10.0)
    assert costly["day_pnl"] < base["day_pnl"]
    assert costly["total_costs"] > 0


def test_run_replay_tool_accepts_cost_args():
    reply = dispatch_tool("run_replay", {"commission": 5.0, "slippage_bps": 10.0},
                          _stored_week(), Journal(), CONFIG, NOW, underlying="TEST")
    assert reply.ok
    assert "trade log:" in reply.reply
    assert "clean days" in reply.reply
    assert "costs:" in reply.reply.lower()
