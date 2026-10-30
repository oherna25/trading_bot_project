from datetime import datetime, timezone

from bot.journal import Journal
from bot.news import shock_news_response
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
ENV = {"META_MODEL_API_KEY": "key-1", "MODEL_DAY_BUDGET_USD": "5.0"}


def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=NOW, source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(
                OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                               open_interest=1000.0, gamma=0.05),
                OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="put",
                               open_interest=2000.0, gamma=0.03),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW, open=100.0, high=103.0,
                               low=99.0, close=102.0, volume=1200.0))
    return store


def _grounded_stub(seen):
    def post(url, headers, payload, timeout):
        seen.append(payload)
        assert payload["tools"] == [{"type": "web_search"}]
        return {
            "output": [{"content": [{"type": "output_text", "text": "CPI surprise hit tech (source)."}]}],
            "usage": {"input_tokens": 60, "output_tokens": 15},
        }

    return post


def test_shock_news_without_live_models_keeps_facts():
    journal = Journal()
    reply = shock_news_response("TEST", _seeded_store(), journal, NOW, -2.0)
    assert "TEST shock -2.0%" in reply
    assert "news: enable live models" in reply
    assert journal.entries()[-1].kind == "shock_news_skipped"


def test_shock_news_appends_driver():
    seen = []
    journal = Journal()
    reply = shock_news_response("TEST", _seeded_store(), journal, NOW, -2.0,
                                live_models=True, env=dict(ENV), http_post=_grounded_stub(seen))
    assert "TEST shock -2.0%" in reply
    assert "likely driver: CPI surprise" in reply
    assert "TEST" in seen[0]["input"] and "-2.0" in seen[0]["input"]
    assert "model_receipt" in [entry.kind for entry in journal.entries()]


def test_shock_news_survives_grounding_failure():
    def down(*args, **kwargs):
        raise RuntimeError("search down")

    journal = Journal()
    reply = shock_news_response("TEST", _seeded_store(), journal, NOW, -2.0,
                                live_models=True, env=dict(ENV), http_post=down)
    assert "TEST shock -2.0%" in reply
    assert "news: no fresh driver found" in reply


def test_shock_news_passthrough_without_snapshot():
    def explode(*args, **kwargs):
        raise AssertionError("no model call without facts")

    journal = Journal()
    reply = shock_news_response("TEST", SnapshotStore(), journal, NOW, -2.0,
                                live_models=True, env=dict(ENV), http_post=explode)
    assert reply == "shock TEST: no snapshot stored"
