from datetime import datetime, timezone

from bot.docread import read_document, segment_image
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
ENV = {"META_MODEL_API_KEY": "key-1", "MODEL_DAY_BUDGET_USD": "5.0"}
PNG = b"\x89PNG\r\n\x1a\n" + b"doc-bytes" * 100
BOXES = {
    "boxes": [
        {"x": 10, "y": 20, "w": 300, "h": 200, "label": "chain table"},
        {"x": 1, "y": 2, "w": 3, "h": 4, "label": "header"},
    ]
}


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


def _router(log, segment_body=BOXES):
    def post(url, headers, payload, timeout):
        log.append((url, payload))
        if url.endswith("/segmentations"):
            assert payload["model"] == "sam-3.1"
            return dict(segment_body)
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


def test_segment_image_returns_boxes():
    log = []
    journal = Journal()
    regions = segment_image(PNG, journal, NOW, "snap-1", dict(ENV), _router(log))
    assert [region["label"] for region in regions] == ["chain table", "header"]
    kinds = [entry.kind for entry in journal.entries()]
    assert "image_segmented" in kinds and "model_receipt" in kinds
    receipt = next(e for e in journal.entries() if e.kind == "model_receipt")
    assert receipt.payload["model_id"] == "sam-3.1"


def test_segment_image_skips_bad_boxes():
    log = []
    journal = Journal()
    body = {"boxes": [{"x": "bad"}, {"x": 1, "y": 2, "w": 3, "h": 4}]}
    regions = segment_image(PNG, journal, NOW, "", dict(ENV), _router(log, body))
    assert [region["label"] for region in regions] == ["region"]


def test_document_reads_with_region_hints():
    log = []
    journal = Journal()
    reply = read_document(PNG, "extract strikes", "TEST", _seeded_store(), journal, NOW,
                          live_models=True, env=dict(ENV), http_post=_router(log))
    assert "100 strike" in reply
    chat_payload = next(payload for url, payload in log if not url.endswith("/segmentations"))
    assert "chain table" in chat_payload["messages"][1]["content"][0]["text"]
    assert "extract strikes" in chat_payload["messages"][1]["content"][0]["text"]


def test_document_survives_segment_failure():
    log = []

    def flaky(url, headers, payload, timeout):
        log.append(url)
        if url.endswith("/segmentations"):
            raise RuntimeError("sam down")
        return {
            "choices": [{"message": {"content": "full image read"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    journal = Journal()
    reply = read_document(PNG, "", "TEST", _seeded_store(), journal, NOW,
                          live_models=True, env=dict(ENV), http_post=flaky)
    assert reply == "full image read"
    assert "segment_skipped" in [entry.kind for entry in journal.entries()]


def test_document_without_keys_reports_unavailable():
    log = []
    journal = Journal()
    reply = read_document(PNG, "", "TEST", _seeded_store(), journal, NOW,
                          live_models=True, env={}, http_post=_router(log))
    assert reply == "chart reading unavailable: missing META_MODEL_API_KEY"
    assert log == []
