from datetime import datetime, timezone

from bot.docread import read_document
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
ENV = {"ANTHROPIC_API_KEY": "key-1", "MODEL_DAY_BUDGET_USD": "5.0"}
PNG = b"\x89PNG\r\n\x1a\n" + b"doc-bytes" * 100

def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=NOW, source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(
                OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                               open_interest=1000.0, gamma=0.05),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW, open=100.0, high=103.0,
                               low=99.0, close=102.0, volume=1200.0))
    return store


def _router(log):
    def post(url, headers, payload, timeout):
        log.append((url, payload))
        return {
            "choices": [{"message": {"content": "table shows 100 strike bid 2.40"}}],
            "usage": {"prompt_tokens": 400, "completion_tokens": 20},
        }

    return post


def test_document_needs_live_models():
    journal = Journal()
    reply = read_document(PNG, "", "TEST", _seeded_store(), journal, NOW)
    assert reply == "document reading needs live models"
    assert journal.entries()[-1].kind == "doc_skipped"


def test_document_rejects_non_image():
    journal = Journal()
    reply = read_document(b"nope" * 10, "", "TEST", _seeded_store(), journal, NOW,
                          live_models=True, env=dict(ENV))
    assert reply.startswith("document rejected")


def test_document_reads_with_one_model_call():
    log = []
    journal = Journal()
    reply = read_document(PNG, "extract strikes", "TEST", _seeded_store(), journal, NOW,
                          live_models=True, env=dict(ENV), http_post=_router(log))
    assert "100 strike" in reply
    assert len(log) == 1
    assert "extract strikes" in log[0][1]["messages"][1]["content"][0]["text"]


def test_document_without_keys_reports_unavailable():
    log = []
    journal = Journal()
    reply = read_document(PNG, "", "TEST", _seeded_store(), journal, NOW,
                          live_models=True, env={}, http_post=_router(log))
    assert reply == "chart reading unavailable: missing ANTHROPIC_API_KEY"
    assert log == []
