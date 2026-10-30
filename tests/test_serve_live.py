import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from bot.journal import Journal
from bot.muse import create_server
from bot.persist import save_bundle
from bot.shapes import Bar, ChainSnapshot, OperatorConfig, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

CONFIG = OperatorConfig(
    max_day_loss=1000.0,
    day_profit_target=500.0,
    max_trade_size=5000.0,
)


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
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    return store


class LiveServer:
    def __init__(self, store, journal, config=CONFIG, store_dir=None, token=None):
        self.server = create_server(store, journal, config, "127.0.0.1", 0, store_dir, token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.thread.join(timeout=10)
        self.server.server_close()

    def get(self, path, token=None):
        request = urllib.request.Request(self.base + path)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()

    def post(self, path, payload, token=None):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()


def _status_of(call):
    try:
        return call()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_serve_cards_health_and_chat_over_socket(monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    monkeypatch.delenv("BASETEN_REASON_MODEL_ID", raising=False)
    store = _seeded_store()
    journal = Journal()
    journal.append(NOW, "snapshot_ingested", {"snapshot_id": "TEST:snap", "source_name": "tradier_sandbox"})
    with LiveServer(store, journal) as live:
        status, body = live.get("/healthz")
        assert status == 200
        assert json.loads(body) == {"ok": True}
        status, body = live.get("/health")
        assert status == 200
        health = json.loads(body)
        assert health["feed_used"] == "tradier_sandbox"
        assert health["underlyings"] == ["TEST"]
        status, body = live.get("/cards?underlying=TEST")
        assert status == 200
        text = body.decode("utf-8")
        assert text.startswith("<!doctype html>")
        assert "<article>" in text
        assert "long via" in text
        status, body = live.get("/cards.json?underlying=TEST&query=monthly")
        assert status == 200
        doc = json.loads(body)
        assert doc["underlying"] == "TEST"
        assert doc["count"] == 1
        assert len(doc["cards"]) == 1
        assert doc["cards"][0]["symbol"] == "TEST"
        assert doc["cards"][0]["reason_source"] == "fallback"
        status, body = _status_of(lambda: live.get("/cards.json?underlying=NOPE"))
        assert status == 400
        assert "NOPE" in json.loads(body)["error"]
        status, body = live.post("/chat", {"text": "gex brief", "underlying": "TEST"})
        assert status == 200
        assert "total exposure:" in json.loads(body)["reply"]


def test_serve_enforces_bearer_token_over_socket(monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    monkeypatch.delenv("BASETEN_REASON_MODEL_ID", raising=False)
    with LiveServer(_seeded_store(), Journal(), token="secret") as live:
        status, _ = _status_of(lambda: live.get("/cards?underlying=TEST"))
        assert status == 401
        status, _ = _status_of(lambda: live.get("/cards.json?underlying=TEST"))
        assert status == 401
        status, _ = _status_of(lambda: live.post("/chat", {"text": "hi"}))
        assert status == 401
        status, _ = live.get("/health")
        assert status == 200
        status, body = live.get("/cards?underlying=TEST", token="secret")
        assert status == 200
        assert b"<article>" in body
        status, body = live.get("/cards.json?underlying=TEST", token="secret")
        assert status == 200
        assert json.loads(body)["count"] == 1
        status, body = live.post("/chat", {"text": "gex brief", "underlying": "TEST"}, token="secret")
        assert status == 200
        assert json.loads(body)["ok"] is True


def test_serve_reloads_bundle_written_by_another_process(tmp_path):
    store = _seeded_store()
    journal = Journal()
    save_bundle(str(tmp_path), store, journal)
    with LiveServer(store, journal, store_dir=str(tmp_path)) as live:
        status, body = live.post("/chat", {"text": "gex brief", "underlying": "QQQ"})
        assert json.loads(body)["ok"] is False
        other_store = _seeded_store()
        other_store.save(
            ChainSnapshot(
                underlying="QQQ",
                timestamp_utc=NOW + timedelta(hours=1),
                source_name="marketdata_eod",
                latency_ms=9.0,
                contracts_list=(
                    OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                                   open_interest=500.0, gamma=0.05),
                ),
            )
        )
        other_store.save_bar("QQQ", Bar(interval="1d", timestamp_utc=NOW,
                                        open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
        save_bundle(str(tmp_path), other_store, journal)
        status, body = live.post("/chat", {"text": "gex brief", "underlying": "QQQ"})
        assert status == 200
        assert json.loads(body)["ok"] is True
        assert "QQQ GEX brief" in json.loads(body)["reply"]
