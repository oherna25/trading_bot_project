import base64
from datetime import datetime, timezone

import pytest

from bot.cards import (
    clear_share_cache,
    milestone_text,
    render_milestone_art,
    render_signal_art,
    share_card,
    share_html,
    share_text,
)
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OperatorProfile, OptionContract
from bot.store import SnapshotStore
from bot.tradecards import PROFILE_REQUIRED, TradeCard

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
ENV = {"META_MODEL_API_KEY": "key-1", "MODEL_DAY_BUDGET_USD": "5.0"}
ART = base64.b64encode(b"fake-art-bytes").decode("ascii")


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_share_cache()
    yield
    clear_share_cache()


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


def _art_stub(log):
    def post(url, headers, payload, timeout):
        log.append(payload)
        assert payload["model"] == "muse-image-1.0"
        assert "no text" in payload["prompt"]
        return {"data": [{"b64_json": ART}]}

    return post


def test_share_text_covers_card():
    text = share_text(_card(), art_hash="ab" * 32)
    assert "TEST short single put 100.0 2026-11-20" in text
    assert "0.65" in text and "short-gamma" in text
    assert "max loss $250" in text
    assert "why: r" in text
    assert "art abababababab" in text


def test_share_text_without_art_notes_models():
    assert "art: needs live models" in share_text(_card())


def test_share_card_offline_returns_caption():
    def explode(*args, **kwargs):
        raise AssertionError("no model call offline")

    journal = Journal()
    text = share_card(0, "TEST", _seeded_store(), journal, NOW, env=dict(ENV), http_post=explode)
    assert "TEST short" in text
    assert "art: needs live models" in text
    assert journal.entries()[-1].kind == "share_skipped"


def test_share_card_live_renders_art():
    log = []
    journal = Journal()
    text = share_card(0, "TEST", _seeded_store(), journal, NOW, live_models=True,
                      env=dict(ENV), http_post=_art_stub(log))
    assert "TEST short" in text and "art " in text
    assert "needs live models" not in text
    kinds = [entry.kind for entry in journal.entries()]
    assert "signal_card" in kinds and "model_receipt" in kinds


def test_signal_art_caches_by_prompt():
    log = []
    journal = Journal()
    first = render_signal_art(_card(), journal, NOW, dict(ENV), _art_stub(log))
    second = render_signal_art(_card(), journal, NOW, dict(ENV), _art_stub(log))
    assert len(log) == 1
    assert first and second and first["art_hash"] == second["art_hash"]
    cached = [e for e in journal.entries() if e.payload.get("cached") is True]
    assert len(cached) == 1


def test_share_card_needs_profile():
    journal = Journal()
    assert share_card(0, "TEST", _seeded_store(with_profile=False), journal, NOW) == PROFILE_REQUIRED


def test_share_card_index_out_of_range():
    journal = Journal()
    with pytest.raises(ValueError, match="out of range"):
        share_card(9, "TEST", _seeded_store(), journal, NOW)


def test_share_html_embeds_art():
    import hashlib

    page = share_html(_card(), art=b"fake-art-bytes", url=None)
    assert "<img" in page and "data:image/png;base64," in page
    assert "TEST short" in page
    assert f"art {hashlib.sha256(b'fake-art-bytes').hexdigest()[:12]}" in page
    assert "needs live models" not in page


def test_milestone_text_signs():
    assert milestone_text(250.0) == "up $250 today"
    assert milestone_text(-30.0) == "down $30 today"
    assert milestone_text(0.0) == "flat today"


def test_milestone_art_live_and_skipped():
    log = []
    journal = Journal()
    art = render_milestone_art("up $250 today", journal, NOW, dict(ENV), _art_stub(log))
    assert art is not None
    assert "milestone_card" in [entry.kind for entry in journal.entries()]
    assert render_milestone_art("up $1 today", Journal(), NOW, {}) is None
