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
    monkeypatch.delenv("LLAMA_SERVER_BIN", raising=False)
    with pytest.raises(ValueError, match="local runner missing"):
        localmodels.run_local("analyst", "hi", env)


def test_server_argv_carries_no_prompt():
    argv = localmodels.server_argv("llama-server", "w.gguf", 18080, 4096, 4)
    assert "-p" not in argv
    assert "secret prompt" not in argv
    assert argv[:2] == ["llama-server", "-m"]
    assert "--port" in argv and "18080" in argv


class _FakePopen:
    instances: list = []

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self._poll = None
        self.terminated = False
        _FakePopen.instances.append(self)

    def poll(self):
        return self._poll

    @property
    def pid(self):
        return 4242

    def terminate(self):
        self.terminated = True
        self._poll = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._poll = 9


class _FakeHTTP:
    def __init__(self, content="ok", health_ok=True, error=None):
        self.content = content
        self.health_ok = health_ok
        self.error = error
        self.requests: list = []

    def __call__(self, request, timeout=None):
        import json
        from urllib.error import URLError

        if self.error is not None:
            raise self.error
        if isinstance(request, str):
            if not self.health_ok:
                raise URLError("down")
            return _FakeResponse(json.dumps({"status": "ok"}).encode("utf-8"))
        data = request.data or b"{}"
        self.requests.append(json.loads(data.decode("utf-8")))
        return _FakeResponse(json.dumps({"content": self.content}).encode("utf-8"))


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _patched_server(monkeypatch, **kwargs):
    from bot.localmodels import LocalServer

    _FakePopen.instances.clear()
    http = _FakeHTTP()
    monkeypatch.setattr("bot.localmodels.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("bot.localmodels.urllib.request.urlopen", http)
    server = LocalServer("llama-server", "w.gguf", port=18081, **kwargs)
    return server, http


def test_server_starts_once_and_stops_process(monkeypatch):
    server, _ = _patched_server(monkeypatch)
    server.start()
    server.start()
    assert len(_FakePopen.instances) == 1
    assert server.running is True
    assert server.pid == 4242
    assert server._argv()[0] == "llama-server"
    server.stop()
    assert _FakePopen.instances[0].terminated is True
    assert server.running is False


def test_registry_reuses_server_and_restarts_dead_ones(monkeypatch):
    from bot import localmodels

    _FakePopen.instances.clear()
    monkeypatch.setattr("bot.localmodels.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("bot.localmodels.urllib.request.urlopen", _FakeHTTP())
    monkeypatch.setattr("bot.localmodels._free_port", lambda: 18083)
    first = localmodels.get_local_server("llama-server", "w.gguf")
    assert localmodels.get_local_server("llama-server", "w.gguf") is first
    assert len(_FakePopen.instances) == 1
    first._process._poll = 1
    second = localmodels.get_local_server("llama-server", "w.gguf")
    assert second is not first
    assert len(_FakePopen.instances) == 2
    localmodels.stop_local_servers()
    assert all(p.terminated or p.poll() is not None for p in _FakePopen.instances)


def test_complete_posts_prompt_in_body(monkeypatch):
    server, http = _patched_server(monkeypatch)
    body = server.complete("my secret")
    assert body["text"] == "ok"
    assert body["latency_ms"] >= 0.0
    assert http.requests == [{"prompt": "my secret", "n_predict": 512, "cache_prompt": True}]


def test_complete_maps_transport_errors(monkeypatch):
    from urllib.error import URLError

    server, http = _patched_server(monkeypatch)
    http.error = TimeoutError("slow")
    with pytest.raises(ValueError, match="local run timed out"):
        server.complete("hi", timeout=0.1)
    http.error = URLError("refused")
    with pytest.raises(ValueError, match="local run failed"):
        server.complete("hi")


def test_server_start_failure_raises(monkeypatch):
    from bot.localmodels import LocalServer

    class DeadPopen(_FakePopen):
        def __init__(self, argv, **kwargs):
            super().__init__(argv, **kwargs)
            self._poll = 3

    _FakePopen.instances.clear()
    monkeypatch.setattr("bot.localmodels.subprocess.Popen", DeadPopen)
    monkeypatch.setattr("bot.localmodels.urllib.request.urlopen", _FakeHTTP())
    server = LocalServer("llama-server", "w.gguf", port=18082, start_timeout=1.0)
    with pytest.raises(ValueError, match="local server did not start"):
        server.start()


def test_run_local_uses_shared_server(tmp_path, monkeypatch):
    env = {"GLIMMER_GGUF_PATH": _weights(tmp_path), "LLAMA_SERVER_BIN": "llama-server"}
    calls: list = []

    class FakeServer:
        def complete(self, prompt, timeout=180.0):
            calls.append((prompt, timeout))
            return {"text": "t\nDIRECTION: long\nCONFIDENCE: 0.7", "latency_ms": 3.0}

    monkeypatch.setattr(localmodels, "get_local_server", lambda *a, **k: FakeServer())
    monkeypatch.setattr(localmodels.shutil, "which", lambda name: "/bin/llama-server")
    text, receipt = localmodels.run_local("analyst", "read SPY", env)
    assert calls == [("read SPY", 180.0)]
    assert "DIRECTION: long" in text
    assert receipt.model_id == "muse-glimmer-3b"


def _seeded_parts():
    from bot.exposure import compute_gex
    from bot.shapes import Bar, ChainSnapshot, OptionContract
    from bot.store import SnapshotStore

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
