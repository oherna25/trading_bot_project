from datetime import datetime, timezone

import pytest

from bot.cards import share_card, share_html, share_text
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OperatorProfile, OptionContract
from bot.store import SnapshotStore
from bot.tradecards import PROFILE_REQUIRED, TradeCard

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
ENV = {"ANTHROPIC_API_KEY": "key-1", "MODEL_DAY_BUDGET_USD": "5.0"}


def _card(**overrides):
    base = dict(
        symbol="TEST", direction="short", spread="single put 100.0", strike=100.0,
        expiry="2026-11-20", call_or_put="put", hedge_strike=None, confidence=0.65,
        snapshot_id="TEST:x", max_loss=250.0, breakeven=97.5, net_debit=2.5, spread_pct=8.0,
        wall_exposure=-1_500_000.0, regime="short-gamma", spot=102.0, flip_point=None,
        reason="r", reason_source="fallback",
    )
    base.update(overrides)
    return TradeCard(**base)


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


def test_share_text_covers_card():
    text = share_text(_card())
    assert "TEST short single put 100.0 2026-11-20" in text
    assert "65%" in text and "short-gamma" in text
    assert "max loss $250" in text
    assert "why: r" in text
    assert "art" not in text.split("why:")[1].splitlines()[1]


def test_share_card_returns_caption_without_a_model_call():
    def explode(*args, **kwargs):
        raise AssertionError("no model call for a caption")

    text = share_card(0, "TEST", _seeded_store(), Journal(), NOW, env={}, http_post=explode)
    assert "TEST short" in text
    assert "/dashboard?underlying=TEST" in text


def test_share_card_needs_profile():
    journal = Journal()
    assert share_card(0, "TEST", _seeded_store(with_profile=False), journal, NOW) == PROFILE_REQUIRED


def test_share_card_index_out_of_range():
    journal = Journal()
    with pytest.raises(ValueError, match="out of range"):
        share_card(9, "TEST", _seeded_store(), journal, NOW)


def test_share_html_is_caption_only():
    page = share_html(_card())
    assert "TEST short" in page
    assert "<img alt=\"trade card art\"" not in page
