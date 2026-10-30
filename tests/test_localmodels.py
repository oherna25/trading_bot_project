from datetime import datetime, timezone

import pytest

from bot import localmodels
from bot.journal import Journal

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)


def _weights(tmp_path, name="glimmer-3b.gguf") -> str:
    path = tmp_path / name
    path.write_bytes(b"gguf-stub")
    return str(path)


def test_local_unavailable_without_paths():
    assert localmodels.local_available({}) is False
    assert localmodels.local_weights({}) is None


def test_local_unavailable_when_file_missing(tmp_path):
    env = {"GLIMMER_GGUF_PATH": str(tmp_path / "nope.gguf")}
    assert localmodels.local_available(env) is False


def test_local_prefers_glimmer(tmp_path):
    env = {
        "GLIMMER_GGUF_PATH": _weights(tmp_path, "glimmer.gguf"),
        "SPARK_OPEN_GGUF_PATH": _weights(tmp_path, "spark-open.gguf"),
    }
    name, _ = localmodels.local_weights(env)
    assert name == "glimmer"


def test_local_falls_back_to_spark_open(tmp_path):
    env = {"SPARK_OPEN_GGUF_PATH": _weights(tmp_path, "spark-open.gguf")}
    name, _ = localmodels.local_weights(env)
    assert name == "spark-open"


def test_run_local_with_stub_runner(tmp_path):
    env = {"GLIMMER_GGUF_PATH": _weights(tmp_path)}

    def runner(prompt, weights_path, timeout):
        assert "SPY" in prompt
        assert weights_path == env["GLIMMER_GGUF_PATH"]
        return {"text": "thesis\nDIRECTION: long\nCONFIDENCE: 0.7", "latency_ms": 12.0}

    text, receipt = localmodels.run_local("analyst", "read SPY", env, runner=runner)
    assert "DIRECTION: long" in text
    assert receipt.model_id == "muse-glimmer-3b"
    assert receipt.role_name == "local_analyst"
    assert receipt.cost_usd == 0.0
    assert receipt.latency_ms == 12.0


def test_run_local_needs_weights():
    with pytest.raises(ValueError, match="no local weights"):
        localmodels.run_local("analyst", "hi", {}, runner=lambda *a: {"text": "x"})


def test_run_local_rejects_empty_runner_text(tmp_path):
    env = {"GLIMMER_GGUF_PATH": _weights(tmp_path)}
    with pytest.raises(ValueError, match="empty local reply"):
        localmodels.run_local("analyst", "hi", env, runner=lambda *a: {"text": "  "})


def test_default_runner_needs_binary(tmp_path, monkeypatch):
    env = {"GLIMMER_GGUF_PATH": _weights(tmp_path)}
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("LLAMA_BIN", raising=False)
    with pytest.raises(ValueError, match="local runner missing"):
        localmodels.run_local("analyst", "hi", env)


def _seeded_parts():
    from bot.exposure import compute_gex
    from bot.shapes import Bar, ChainSnapshot, OptionContract
    from bot.store import SnapshotStore, snapshot_id_for

    store = SnapshotStore()
    snapshot = ChainSnapshot(
        underlying="TEST", timestamp_utc=NOW, source_name="tradier_sandbox", latency_ms=9.0,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                           open_interest=1000.0, gamma=0.05),
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="put",
                           open_interest=2000.0, gamma=0.03),
        ),
    )
    snapshot_id = store.save(snapshot)
    bar = Bar(interval="1d", timestamp_utc=NOW, open=100.0, high=103.0,
              low=99.0, close=102.0, volume=1200.0)
    profile = compute_gex(snapshot, snapshot_id, spot=bar.close)
    return snapshot_id, snapshot, profile, bar


def test_analyst_backend_uses_local_tier(tmp_path):
    from bot.research import analyst_backend

    env = {"GLIMMER_GGUF_PATH": _weights(tmp_path)}
    journal = Journal()
    snapshot_id, snapshot, profile, bar = _seeded_parts()
    signal = analyst_backend(
        "brief", snapshot_id, snapshot, profile, bar, journal, NOW,
        live_models=True, env=env,
        local_runner=lambda *a: {"text": "local read\nDIRECTION: long\nCONFIDENCE: 0.8"},
    )
    assert signal.direction == "long"
    assert signal.confidence == 0.8
    assert "local read" in signal.thesis
    receipts = [e for e in journal.entries() if e.kind == "model_receipt"]
    assert receipts and receipts[0].payload["model_id"] == "muse-glimmer-3b"
    assert receipts[0].payload["cost_usd"] == 0.0


def test_analyst_backend_rescues_spark_failure_with_local(tmp_path):
    from bot.research import analyst_backend

    def down(*args, **kwargs):
        raise RuntimeError("spark down")

    env = {"META_MODEL_API_KEY": "k", "MODEL_DAY_BUDGET_USD": "5.0",
           "GLIMMER_GGUF_PATH": _weights(tmp_path)}
    journal = Journal()
    snapshot_id, snapshot, profile, bar = _seeded_parts()
    signal = analyst_backend(
        "brief", snapshot_id, snapshot, profile, bar, journal, NOW,
        live_models=True, env=env, http_post=down,
        local_runner=lambda *a: {"text": "rescued\nDIRECTION: short\nCONFIDENCE: 0.6"},
    )
    assert signal.direction == "short"
    assert "rescued" in signal.thesis


def test_local_grounding_marks_skipped(tmp_path):
    from bot.research import analyst_backend

    env = {"GLIMMER_GGUF_PATH": _weights(tmp_path)}
    journal = Journal()
    snapshot_id, snapshot, profile, bar = _seeded_parts()
    analyst_backend(
        "brief", snapshot_id, snapshot, profile, bar, journal, NOW,
        live_models=True, grounding=True, env=env,
        local_runner=lambda *a: {"text": "read\nDIRECTION: flat\nCONFIDENCE: 0.5"},
    )
    kinds = [entry.kind for entry in journal.entries()]
    assert "grounding_skipped" in kinds
