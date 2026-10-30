from datetime import datetime, timezone

import pytest

from bot.debate import attach_debate, card_debate, debate_card
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract, default_operator_config
from bot.store import SnapshotStore
from bot.tradecards import TradeCard

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()
ENV = {"META_MODEL_API_KEY": "key-1", "MODEL_DAY_BUDGET_USD": "5"}


def _card(**overrides):
    base = dict(
        symbol="TEST", direction="short", spread="single put 100.0", strike=100.0,
        expiry="2026-11-20", call_or_put="put", hedge_strike=None, confidence=0.65,
        snapshot_id="TEST:x", max_loss=250.0, breakeven=97.5, net_debit=2.5, spread_pct=8.0,
        wall_exposure=-1_500_000.0, regime="short-gamma", spot=102.0, flip_point=None,
        reason="r", reason_source="fallback", earnings_date="2026-11-02", iv_rank=72.0,
    )
    base.update(overrides)
    return TradeCard(**base)


def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=NOW,
            source_name="tradier_sandbox", latency_ms=9.0,
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
    return store


def _stub_post(url, headers, payload, timeout):
    return {
        "choices": [{"message": {"content": f"{payload['model']} argues."}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_fallback_debate_grounds_both_sides():
    text = debate_card(_card(), Journal(), NOW)
    assert text.startswith("BULL [rules]: Take the short")
    assert "screens at 0.65 confidence" in text
    assert "BEAR [rules]: Fade the short" in text
    assert "max loss $250" in text
    assert "earnings 2026-11-02 lands inside the window" in text
    assert "8.0% wide market bleeds edge" in text
    assert text.endswith("verdict: take the short (0.65)")


def test_fallback_verdict_passes_below_half():
    text = debate_card(_card(confidence=0.30), Journal(), NOW)
    assert "the screen itself scores below 0.50" in text
    assert text.endswith("verdict: pass (0.30 below 0.50)")


def test_live_models_argue_and_journal_receipts():
    journal = Journal()
    text = debate_card(_card(), journal, NOW, live_models=True, env=dict(ENV), http_post=_stub_post)
    assert "BULL [muse-spark-1.3]: muse-spark-1.3 argues." in text
    assert "BEAR [muse-wonder-1.0]: muse-wonder-1.0 argues." in text
    assert [e.kind for e in journal.entries()].count("model_receipt") == 2


def test_failed_models_fall_back_per_side():
    def boom(url, headers, payload, timeout):
        raise TimeoutError("down")

    journal = Journal()
    text = debate_card(_card(), journal, NOW, live_models=True, env=dict(ENV), http_post=boom)
    assert "BULL [rules]:" in text and "BEAR [rules]:" in text
    assert [e.kind for e in journal.entries()].count("model_fallback") == 2


def test_card_debate_resolves_snapshot_and_index():
    text = card_debate("TEST", _seeded_store(), Journal(), NOW)
    assert "BULL [rules]:" in text and "verdict:" in text
    with pytest.raises(ValueError, match="out of range"):
        card_debate("TEST", _seeded_store(), Journal(), NOW, index=99)
    with pytest.raises(ValueError, match="no snapshot stored"):
        card_debate("NOPE", _seeded_store(), Journal(), NOW)


def test_debate_route_tool_and_chat_wiring():
    from bot.muse import TOOL_REGISTRY, dispatch_tool, route_http
    from bot.research import route_chat

    page = route_http(
        "GET", "/card?underlying=TEST&index=0&debate=1", {}, _seeded_store(), Journal(), CONFIG, NOW
    )
    assert page.status == 200
    body = page.body.decode("utf-8")
    assert "bull vs bear" in body and "BULL [rules]:" in body
    plain = route_http(
        "GET", "/card?underlying=TEST&index=0", {}, _seeded_store(), Journal(), CONFIG, NOW
    )
    assert "bull vs bear" not in plain.body.decode("utf-8")
    assert "debate_card" in TOOL_REGISTRY
    tool_reply = dispatch_tool("debate_card", {"index": 1}, _seeded_store(), Journal(), CONFIG, NOW, underlying="TEST")
    assert tool_reply.ok and "BEAR [rules]:" in tool_reply.reply
    assert "BULL [rules]:" in route_chat("debate", "TEST", _seeded_store(), Journal(), CONFIG, NOW)
    assert "out of range" in route_chat("debate 5", "TEST", _seeded_store(), Journal(), CONFIG, NOW)


def test_attach_debate_splices_before_body_close():
    page = "<html><body><p>card</p></body></html>"
    out = attach_debate(page, "BULL: x")
    assert out == "<html><body><p>card</p><section><h2>bull vs bear</h2><pre>BULL: x</pre></section></body></html>"
