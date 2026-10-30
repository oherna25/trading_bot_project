from datetime import datetime, timezone

from bot.chartread import CHART_MAX_BYTES, read_chart_image
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
ENV = {"META_MODEL_API_KEY": "key-1", "MODEL_DAY_BUDGET_USD": "5.0"}
PNG = b"\x89PNG\r\n\x1a\n" + b"chart-bytes" * 100


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


def _stub(seen, content="spot holds above the 100 wall"):
    def post(url, headers, payload, timeout):
        seen.append(payload)
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 40},
        }

    return post


def test_chart_needs_live_models():
    journal = Journal()
    reply = read_chart_image(PNG, "TEST", _seeded_store(), journal, NOW)
    assert reply == "chart reading needs live models"
    assert journal.entries()[-1].kind == "chart_skipped"


def test_chart_rejects_non_image():
    journal = Journal()
    reply = read_chart_image(b"not-an-image" * 10, "TEST", _seeded_store(), journal, NOW,
                             live_models=True, env=dict(ENV))
    assert reply.startswith("chart image rejected")
    assert journal.entries()[-1].kind == "chart_skipped"


def test_chart_rejects_oversize():
    journal = Journal()
    big = b"\x89PNG\r\n\x1a\n" + b"x" * (CHART_MAX_BYTES + 1)
    reply = read_chart_image(big, "TEST", _seeded_store(), journal, NOW,
                             live_models=True, env=dict(ENV))
    assert "too large" in reply


def test_chart_reads_with_engine_context():
    seen = []
    journal = Journal()
    reply = read_chart_image(PNG, "TEST", _seeded_store(), journal, NOW,
                             live_models=True, env=dict(ENV), http_post=_stub(seen))
    assert "100 wall" in reply
    payload = seen[0]
    assert payload["model"] == "muse-spark-1.3"
    parts = payload["messages"][1]["content"]
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "TEST" in parts[0]["text"] and "flip" in parts[0]["text"]
    kinds = [entry.kind for entry in journal.entries()]
    assert "model_receipt" in kinds and "chart_read" in kinds


def test_chart_regions_join_prompt():
    seen = []
    journal = Journal()
    regions = [{"x": 10, "y": 20, "w": 300, "h": 200, "label": "price panel"}]
    read_chart_image(PNG, "TEST", _seeded_store(), journal, NOW,
                     live_models=True, env=dict(ENV), http_post=_stub(seen), regions=regions)
    assert "price panel" in seen[0]["messages"][1]["content"][0]["text"]


def test_chart_without_snapshot_still_reads():
    seen = []
    journal = Journal()
    reply = read_chart_image(PNG, "TEST", SnapshotStore(), journal, NOW,
                             live_models=True, env=dict(ENV), http_post=_stub(seen))
    assert "100 wall" in reply
    assert "no stored snapshot" in seen[0]["messages"][1]["content"][0]["text"]


def test_chart_model_error_stays_graceful():
    def down(*args, **kwargs):
        raise RuntimeError("spark down")

    journal = Journal()
    reply = read_chart_image(PNG, "TEST", _seeded_store(), journal, NOW,
                             live_models=True, env=dict(ENV), http_post=down)
    assert reply == "chart reading failed: RuntimeError"
    assert "model_receipt" not in [entry.kind for entry in journal.entries()]
