from datetime import datetime, timedelta, timezone

from bot.exposure import compute_gex
from bot.ideas import rank_ideas
from bot.journal import Journal
from bot.tradecards import (
    TradeCard,
    build_trade_cards,
    cards_response,
    cards_to_json,
    clear_reason_cache,
    fallback_reason,
    format_trade_cards,
    trade_cards_html,
)
from bot.shapes import Bar, ChainSnapshot, OperatorProfile, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

ENV = {"META_MODEL_API_KEY": "key-1"}


def _chat_stub(*args):
    return {
        "choices": [{"message": {"content": "Model says go."}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }

JSON_KEYS = [
    "symbol", "direction", "spread", "strike", "expiry", "call_or_put", "hedge_strike",
    "confidence", "snapshot_id", "max_loss", "breakeven", "net_debit", "spread_pct",
    "wall_exposure", "regime", "spot", "flip_point", "reason", "reason_source",
    "earnings_date", "iv_rank",
]


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


def _entries(store: SnapshotStore):
    snapshot_id, snapshot = store.read_latest("TEST")
    return [(snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), store.read_latest_bar("TEST"))]


def test_card_stats_equal_idea_plus_profile_numbers():
    store = _seeded_store()
    journal = Journal()
    entries = _entries(store)
    profile = entries[0][2]
    ideas = rank_ideas(entries, None, now=NOW)
    cards = build_trade_cards(entries, None, NOW, journal, env={})
    assert len(cards) == len(ideas) == 2
    for card, idea in zip(cards, ideas):
        assert card.symbol == idea.symbol
        assert card.direction == idea.direction
        assert card.spread == idea.spread
        assert card.strike == idea.strike
        assert card.expiry == idea.expiry
        assert card.call_or_put == idea.call_or_put
        assert card.hedge_strike == idea.hedge_strike
        assert card.confidence == idea.confidence
        assert card.snapshot_id == idea.snapshot_id
        assert card.max_loss == idea.max_loss
        assert card.breakeven == idea.breakeven
        assert card.net_debit == idea.net_debit
        assert card.spread_pct == idea.spread_pct
        assert card.wall_exposure == profile.per_strike_exposure.get(idea.strike)
        assert card.regime == "short-gamma"
        assert card.spot == 102.0
        assert card.flip_point == profile.flip_point
        assert card.reason_source == "fallback"


def test_fallback_reason_mentions_direction_spread_and_capped_loss():
    cards = build_trade_cards(_entries(_seeded_store()), None, NOW, Journal(), env={})
    assert cards
    for card in cards:
        assert card.direction in card.reason
        assert card.spread in card.reason
    vertical = build_trade_cards(_vertical_entries(), None, NOW, Journal(), env={})
    assert vertical[0].max_loss is not None
    assert "capped loss" in vertical[0].reason


def _vertical_entries():
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                               open_interest=2000.0, gamma=0.09, price=2.50, bid=2.40, ask=2.60),
                OptionContract(strike=105.0, expiry="2026-11-20", call_or_put="call",
                               open_interest=1000.0, gamma=0.04, price=1.50, bid=1.40, ask=1.50),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW, open=100.0,
                               high=103.0, low=99.0, close=102.0, volume=1200.0))
    snapshot_id, snapshot = store.read_latest("TEST")
    return [(snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), store.read_latest_bar("TEST"))]


def test_fallback_reason_uses_spot_vs_flip_side():
    above = fallback_reason({"direction": "long", "spread": "s", "spot": 105.0, "flip_point": 100.0})
    assert "spot 105.0 above flip 100.0" in above
    below = fallback_reason({"direction": "short", "spread": "s", "spot": 95.0, "flip_point": 100.0})
    assert "spot 95.0 below flip 100.0" in below


def test_cards_to_json_keys_exact_and_safe():
    cards = build_trade_cards(_entries(_seeded_store()), None, NOW, Journal(), env={})
    doc = cards_to_json(cards)
    assert set(doc) == {"cards"}
    assert len(doc["cards"]) == 2
    for row in doc["cards"]:
        assert list(row.keys()) == JSON_KEYS
    import json

    json.dumps(doc)


def test_html_escapes_reason_with_script_tags():
    card = TradeCard(
        symbol="TEST", direction="long", spread="100/105 call vertical", strike=100.0,
        expiry="2026-11-20", call_or_put="call", hedge_strike=105.0, confidence=0.8,
        snapshot_id="TEST:x", max_loss=120.0, breakeven=101.2, net_debit=120.0,
        spread_pct=8.0, wall_exposure=5000.0, regime="long-gamma", spot=102.0,
        flip_point=99.0, reason="<script>alert(1)</script>", reason_source="fallback",
    )
    page = trade_cards_html([card], "TEST")
    assert page.startswith("<!doctype html>")
    assert "<article>" in page
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "TEST long 100/105 call vertical expiry 2026-11-20" in page


def test_empty_ideas_render_none_found():
    assert format_trade_cards([]) == "trade cards: none found"


def test_format_trade_cards_rounds_header_numbers():
    card = TradeCard(
        symbol="TEST", direction="long", spread="single", strike=100.0,
        expiry="2026-11-20", call_or_put="call", hedge_strike=None,
        confidence=0.5, snapshot_id="TEST:x", max_loss=40.000000000000036,
        breakeven=97.50000000000001, net_debit=40.0, spread_pct=None,
        wall_exposure=-2352664863.869247, regime="short-gamma",
        spot=102.00000000000001, flip_point=99.0, reason="r",
        reason_source="fallback",
    )
    text = format_trade_cards([card])
    assert "max loss 40.0" in text
    assert "breakeven 97.5" in text
    assert "wall -$2.4B" in text
    assert "spot 102.0" in text
    assert "40.000000000000036" not in text


def test_cards_to_json_rounds_floats():
    card = TradeCard(
        symbol="TEST", direction="short", spread="774/775 put vertical", strike=775.0,
        expiry="2026-11-20", call_or_put="put", hedge_strike=774.0, confidence=0.7255512375820845,
        snapshot_id="TEST:x", max_loss=45.000000000000014, breakeven=773.6363636363636,
        net_debit=86.66666666666667, spread_pct=8.000000000000007, wall_exposure=-271686.6666666667,
        regime="short-gamma", spot=774.4999999999999, flip_point=None, reason="r",
        reason_source="fallback",
    )
    (row,) = cards_to_json([card])["cards"]
    assert row["confidence"] == 0.7256
    assert row["max_loss"] == 45.0
    assert row["breakeven"] == 773.64
    assert row["net_debit"] == 86.67
    assert row["spread_pct"] == 8.0
    assert row["wall_exposure"] == -271686.67
    assert row["spot"] == 774.5
    assert row["flip_point"] is None


def test_card_wall_matches_ideas_wall_on_same_snapshot():
    store = _seeded_store()
    entries = _entries(store)
    ideas = rank_ideas(entries, None, now=NOW)
    cards = build_trade_cards(entries, None, NOW, Journal(), env={})
    assert len(cards) == len(ideas) == 2
    for card, idea in zip(cards, ideas):
        from bot.diff import compact_dollars

        assert compact_dollars(card.wall_exposure) in idea.thesis
    page = trade_cards_html([], "TEST")
    assert "none found" in page
    assert "<article>" not in page
    journal = Journal()
    reply = cards_response("2 day trade cards", "TEST", _seeded_store(), journal, NOW, env={})
    assert reply == "trade cards: none found"
    entry = journal.entries()[-1]
    assert entry.kind == "ideas"
    assert entry.payload["count"] == 0
    assert entry.payload["cards"] == 0


def test_meta_stub_success_journals_model_reason():
    journal = Journal()
    cards = build_trade_cards(
        _entries(_seeded_store()), None, NOW, journal,
        env=dict(ENV), http_post=_chat_stub,
    )
    assert [card.reason for card in cards] == ["Model says go.", "Model says go."]
    assert {card.reason_source for card in cards} == {"meta"}
    kinds = [entry.kind for entry in journal.entries()]
    assert kinds == ["trade_card_reason", "trade_card_reason"]
    payload = journal.entries()[0].payload
    assert payload["symbol"] == "TEST"
    assert payload["snapshot_id"] == cards[0].snapshot_id
    assert isinstance(payload["latency_ms"], float)


def test_meta_reason_cached_per_card_stats():
    clear_reason_cache()
    calls = []

    def counting(*args):
        calls.append(1)
        return {
            "choices": [{"message": {"content": "Model says go."}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }

    entries = _entries(_seeded_store())
    first = build_trade_cards(entries, None, NOW, Journal(), env=dict(ENV), http_post=counting)
    assert len(calls) == len(first) == 2
    journal = Journal()
    second = build_trade_cards(entries, None, NOW, journal, env=dict(ENV), http_post=counting)
    assert len(calls) == 2
    assert [card.reason for card in second] == ["Model says go.", "Model says go."]
    assert {card.reason_source for card in second} == {"meta"}
    assert [e.payload["cached"] for e in journal.entries()] == [True, True]
    clear_reason_cache()
    build_trade_cards(entries, None, NOW, Journal(), env=dict(ENV), http_post=counting)
    assert len(calls) == 4


def test_cards_carry_earnings_into_renders_and_reason():
    from datetime import date

    journal = Journal()
    cards = build_trade_cards(
        _entries(_seeded_store()), None, NOW, journal, env={},
        earnings={"TEST": date(2026, 11, 5)},
        iv_history={"TEST": [0.0] * 12 + [100.0] * 12 + [50.0]},
    )
    flagged = [card for card in cards if card.expiry == "2026-11-20"]
    assert flagged != []
    assert {card.earnings_date for card in flagged} == {"2026-11-05"}
    assert {card.iv_rank for card in flagged} == {50.0}
    assert "earnings 2026-11-05 inside expiry" in flagged[0].reason
    text = format_trade_cards(flagged)
    assert "earnings 2026-11-05" in text
    assert "iv rank 50.0" in text
    page = trade_cards_html(flagged, "TEST")
    assert "earnings 2026-11-05 inside expiry" in page
    assert "iv rank 50.0" in page
    payload = cards_to_json(flagged)["cards"][0]
    assert payload["earnings_date"] == "2026-11-05"
    assert payload["iv_rank"] == 50.0


def test_meta_exception_falls_back_and_journals():
    def failing(*args):
        raise RuntimeError("boom")

    journal = Journal()
    cards = build_trade_cards(_entries(_seeded_store()), None, NOW, journal, env=dict(ENV), http_post=failing)
    assert {card.reason_source for card in cards} == {"fallback"}
    assert cards[0].direction in cards[0].reason
    kinds = [entry.kind for entry in journal.entries()]
    assert kinds == ["reason_fallback", "reason_fallback"]
    payload = journal.entries()[0].payload
    assert payload["reason"] == "RuntimeError"


def test_missing_env_falls_back_without_calling_http():
    def exploding(*args):
        raise AssertionError("must not call http without env")

    journal = Journal()
    cards = build_trade_cards(_entries(_seeded_store()), None, NOW, journal, env={}, http_post=exploding)
    assert {card.reason_source for card in cards} == {"fallback"}
    payload = journal.entries()[0].payload
    assert payload["reason"] == "missing META_MODEL_API_KEY"


def test_card_wall_matches_brief_dollar_gex():
    from bot.tradecards import query_trade_cards

    store = _seeded_store()
    cards = query_trade_cards("TEST", "TEST", store, Journal(), NOW, env={})
    assert cards
    snapshot_id, snapshot = store.read_latest("TEST")
    bar = store.read_latest_bar("TEST")
    assert bar is not None
    brief = compute_gex(snapshot, snapshot_id, spot=bar.close)
    for card in cards:
        assert card.wall_exposure == brief.per_strike_exposure.get(card.strike)


def test_cards_response_journals_ideas_with_card_count():
    journal = Journal()
    reply = cards_response("trade cards", "TEST", _seeded_store(), journal, NOW, env={})
    assert reply.startswith("trade cards (2):")
    entry = journal.entries()[-1]
    assert entry.kind == "ideas"
    assert entry.payload["query"] == "trade cards"
    assert entry.payload["symbols"] == ["TEST"]
    assert entry.payload["count"] == 2
    assert entry.payload["cards"] == 2
