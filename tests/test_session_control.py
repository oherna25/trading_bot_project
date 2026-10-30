import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import run_app
from bot.feeds import default_context
from bot.journal import Journal
from bot.muse import (
    MuseChatRequest,
    SessionController,
    dispatch_tool,
    handle_chat,
    openapi_spec,
    parse_schedule,
    route_http,
    session_due,
    start_session_loop,
)
from bot.shapes import ChainSnapshot, OperatorConfig, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 26, 7, 30, tzinfo=timezone.utc)

CONFIG = OperatorConfig(
    max_day_loss=1000.0,
    day_profit_target=500.0,
    max_trade_size=5000.0,
)


def test_parse_schedule_weekdays():
    parsed = parse_schedule("30 7 * * 1-5")
    assert (parsed.minute, parsed.hour, parsed.weekdays) == (30, 7, frozenset({0, 1, 2, 3, 4}))


def test_parse_schedule_single_day():
    parsed = parse_schedule("0 9 * * 1")
    assert (parsed.minute, parsed.hour, parsed.weekdays) == (0, 9, frozenset({0}))


def test_parse_schedule_star_is_every_day():
    assert parse_schedule("0 9 * * *").weekdays == frozenset(range(7))


def test_parse_schedule_lists_and_ranges():
    assert parse_schedule("15 8 * * 0,6").weekdays == frozenset({5, 6})
    assert parse_schedule("0 7 * * 7").weekdays == frozenset({6})
    assert parse_schedule("0 7 * * 1-3,5").weekdays == frozenset({0, 1, 2, 4})


@pytest.mark.parametrize(
    "text",
    [
        "",
        "30 7 * *",
        "30 7 * * 1-5 extra",
        "61 7 * * 1-5",
        "-1 7 * * 1-5",
        "30 24 * * *",
        "xx 7 * * *",
        "30 yy * * *",
        "*/15 7 * * *",
        "30 7 1 * *",
        "30 7 * 2 *",
        "30 7 * * 8",
        "30 7 * * 5-1",
        "30 7 * * 1,,3",
        "30 7 * * 1-2-3",
        "30 7 * * mon",
    ],
)
def test_parse_schedule_rejects_malformed(text):
    with pytest.raises(ValueError):
        parse_schedule(text)


def test_session_due_matches_minute():
    schedule = parse_schedule("30 7 * * 1-5")
    assert session_due(schedule, datetime(2026, 10, 26, 7, 30), None) is True


def test_session_due_rejects_other_minutes_and_hours():
    schedule = parse_schedule("30 7 * * 1-5")
    assert session_due(schedule, datetime(2026, 10, 26, 7, 31), None) is False
    assert session_due(schedule, datetime(2026, 10, 26, 8, 30), None) is False


def test_session_due_gates_weekends():
    schedule = parse_schedule("30 7 * * 1-5")
    assert datetime(2026, 10, 24).weekday() == 5
    assert datetime(2026, 10, 25).weekday() == 6
    assert session_due(schedule, datetime(2026, 10, 24, 7, 30), None) is False
    assert session_due(schedule, datetime(2026, 10, 25, 7, 30), None) is False


def test_session_due_fires_once_per_minute():
    schedule = parse_schedule("30 7 * * 1-5")
    moment = datetime(2026, 10, 26, 7, 30)
    key = (2026, 10, 26, 7, 30)
    assert session_due(schedule, moment, None) is True
    assert session_due(schedule, moment, key) is False
    assert session_due(schedule, datetime(2026, 11, 2, 7, 30), key) is True


class FakeController:
    def __init__(self):
        self.calls = []
        self.fired = threading.Event()

    def run(self, underlying="SPY", live_models=False, grounding=False, cards=False, now=None,
            paper_trade=True):
        self.calls.append(
            {"underlying": underlying, "live_models": live_models, "grounding": grounding,
             "cards": cards, "paper_trade": paper_trade}
        )
        self.fired.set()
        return "fake brief", ["QQQ"]


def test_session_endpoint_runs_controller():
    fake = FakeController()
    response = route_http(
        "POST",
        "/session",
        {"underlying": "QQQ", "live_models": True, "grounding": True, "cards": True},
        SnapshotStore(),
        Journal(),
        CONFIG,
        NOW,
        session_controller=fake,
    )
    assert response.status == 200
    assert json.loads(response.body) == {"ok": True, "reply": "fake brief", "underlyings": ["QQQ"]}
    assert fake.calls == [
        {"underlying": "QQQ", "live_models": True, "grounding": True, "cards": True, "paper_trade": True}
    ]


def test_session_endpoint_defaults_underlying():
    fake = FakeController()
    response = route_http(
        "POST", "/session", {}, SnapshotStore(), Journal(), CONFIG, NOW, session_controller=fake
    )
    assert response.status == 200
    assert fake.calls[0]["underlying"] == "SPY"


@pytest.mark.parametrize("payload", [["not", "a", "dict"], "text", {"underlying": 123}, {"underlying": ["SPY"]}])
def test_session_endpoint_rejects_malformed_body(payload):
    response = route_http(
        "POST", "/session", payload, SnapshotStore(), Journal(), CONFIG, NOW,
        session_controller=FakeController(),
    )
    assert response.status == 400
    assert json.loads(response.body)["ok"] is False


def test_session_endpoint_requires_controller():
    response = route_http("POST", "/session", {}, SnapshotStore(), Journal(), CONFIG, NOW)
    assert response.status == 503


def test_session_endpoint_matches_chat_auth():
    denied = route_http(
        "POST", "/session", {}, SnapshotStore(), Journal(), CONFIG, NOW,
        auth_token="secret", session_controller=FakeController(),
    )
    assert denied.status == 401
    wrong = route_http(
        "POST", "/session", {}, SnapshotStore(), Journal(), CONFIG, NOW,
        auth_token="secret", authorization="Bearer wrong", session_controller=FakeController(),
    )
    assert wrong.status == 401
    allowed = route_http(
        "POST", "/session", {}, SnapshotStore(), Journal(), CONFIG, NOW,
        auth_token="secret", authorization="Bearer secret", session_controller=FakeController(),
    )
    assert allowed.status == 200


def test_session_endpoint_runs_real_controller_offline(tmp_path):
    store, journal = SnapshotStore(), Journal()
    controller = SessionController(store, journal, str(tmp_path), threading.RLock(), default_context)
    response = route_http(
        "POST", "/session", {"underlying": "SPY"}, store, journal, CONFIG, NOW,
        session_controller=controller,
    )
    assert response.status == 200
    body = json.loads(response.body)
    assert body["ok"] is True
    assert body["underlyings"] == ["SPY"]
    assert "SPY" in body["reply"] and "brief" in body["reply"]
    saved = [entry for entry in journal.entries() if entry.kind == "session_saved"]
    assert len(saved) == 1
    assert saved[0].payload["underlyings"] == ["SPY"]
    assert (tmp_path / "snapshots.json").exists()
    assert (tmp_path / "journal.json").exists()
    assert "SPY" in store.list_underlyings()


def test_openapi_lists_session():
    assert "post" in openapi_spec()["paths"]["/session"]


def _seeded_spy_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="SPY",
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
    return store


def test_session_run_does_not_block_locked_reads(tmp_path):
    store = _seeded_spy_store()
    journal = Journal()

    def slow_factory(symbol):
        time.sleep(1.0)
        return default_context(symbol)

    controller = SessionController(store, journal, str(tmp_path), threading.RLock(), slow_factory)
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.setdefault("result", controller.run("SPY", now=NOW)))
    started = time.monotonic()
    thread.start()
    time.sleep(0.2)
    with controller.lock:
        reply = handle_chat(
            MuseChatRequest(text="gex brief", underlying="SPY"), store, journal, CONFIG, NOW
        )
    read_elapsed = time.monotonic() - started
    thread.join(timeout=15)
    assert thread.is_alive() is False
    assert reply.ok is True
    assert read_elapsed < 1.0
    assert "SPY" in outcome["result"][0]


def test_mcp_tools_list_includes_run_session():
    response = route_http(
        "POST", "/mcp", {"method": "tools/list"}, SnapshotStore(), Journal(), CONFIG, NOW
    )
    names = [tool["name"] for tool in json.loads(response.body)["tools"]]
    assert "run_session" in names


def test_dispatch_run_session_returns_brief_with_controller():
    fake = FakeController()
    reply = dispatch_tool(
        "run_session", {"live_models": True}, SnapshotStore(), Journal(), CONFIG, NOW,
        underlying="QQQ", session_controller=fake,
    )
    assert reply.ok is True
    assert reply.reply == "fake brief"
    assert fake.calls == [
        {"underlying": "QQQ", "live_models": True, "grounding": False, "cards": False, "paper_trade": True}
    ]


def test_dispatch_run_session_without_controller_fails():
    reply = dispatch_tool("run_session", {}, SnapshotStore(), Journal(), CONFIG, NOW)
    assert reply.ok is False
    assert reply.error == "sessions not configured"


def test_mcp_run_session_route_passes_controller_under_auth():
    envelope = {
        "method": "tools/call",
        "params": {"name": "run_session", "arguments": {"cards": True}, "underlying": "QQQ"},
    }
    denied = route_http(
        "POST", "/mcp", envelope, SnapshotStore(), Journal(), CONFIG, NOW,
        auth_token="secret", session_controller=FakeController(),
    )
    assert denied.status == 401
    fake = FakeController()
    allowed = route_http(
        "POST", "/mcp", envelope, SnapshotStore(), Journal(), CONFIG, NOW,
        auth_token="secret", authorization="Bearer secret", session_controller=fake,
    )
    assert allowed.status == 200
    assert json.loads(allowed.body) == {"ok": True, "reply": "fake brief", "error": ""}
    assert fake.calls == [
        {"underlying": "QQQ", "live_models": False, "grounding": False, "cards": True, "paper_trade": True}
    ]
    missing = route_http(
        "POST", "/mcp", envelope, SnapshotStore(), Journal(), CONFIG, NOW,
        auth_token="secret", authorization="Bearer secret",
    )
    assert missing.status == 200
    body = json.loads(missing.body)
    assert body["ok"] is False
    assert body["error"] == "sessions not configured"


def test_session_loop_fires_once_on_matching_minute(capsys):
    target = datetime.now().astimezone() + timedelta(seconds=0.5)
    schedule = parse_schedule(
        f"{target.minute} {target.hour} * * {(target.weekday() + 1) % 7 or 7}"
    )
    fake = FakeController()
    halt = start_session_loop(fake, schedule, ["SPY"], interval_seconds=0.05)
    try:
        assert fake.fired.wait(5.0) is True
        halt.wait(0.3)
    finally:
        halt.set()
    assert len(fake.calls) == 1
    assert "fake brief" in capsys.readouterr().out


def test_session_loop_skips_non_matching_minute():
    now_local = datetime.now().astimezone()
    other_minute = (now_local.minute + 30) % 60
    schedule = parse_schedule(f"{other_minute} {now_local.hour} * * *")
    fake = FakeController()
    halt = start_session_loop(fake, schedule, ["SPY"], interval_seconds=0.05)
    try:
        assert fake.fired.wait(0.3) is False
    finally:
        halt.set()
    assert fake.calls == []


def test_session_run_expands_to_stored_underlyings(tmp_path):
    store = SnapshotStore()
    store.save(ChainSnapshot(underlying="SPY", timestamp_utc=NOW, source_name="seed", latency_ms=1.0))
    journal = Journal()
    controller = SessionController(store, journal, str(tmp_path), threading.RLock(), default_context)
    text, symbols = controller.run("QQQ", now=NOW)
    assert symbols == ["QQQ", "SPY"]
    assert "QQQ" in text and "SPY" in text


def _directional_factory(symbol):
    from bot.feeds import FeedContext

    chain = {
        "options": {
            "option": [
                {"strike": 95.0, "expiration_date": "2026-11-20", "option_type": "put",
                 "open_interest": 3000.0, "greeks": {"gamma": 0.05}},
                {"strike": 105.0, "expiration_date": "2026-11-20", "option_type": "call",
                 "open_interest": 3000.0, "greeks": {"gamma": 0.05}},
            ]
        }
    }
    bars = {"bars": {symbol: [
        {"t": "2026-10-26T00:00:00Z", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
    ]}}

    def fake_get(url, headers=None, params=None, timeout=None):
        if "expirations" in url:
            return {"expirations": {"date": ["2026-11-20"]}}
        if "options/chains" in url:
            return chain
        if "/bars" in url:
            return bars
        raise AssertionError(f"unexpected url {url}")

    return FeedContext(
        underlying=symbol,
        credentials={"TRADIER_ACCESS_TOKEN": "test", "APCA_API_KEY_ID": "t", "APCA_API_SECRET_KEY": "t"},
        http_get=fake_get,
    )


def test_session_run_trades_by_default_and_opts_out(tmp_path):
    store, journal = SnapshotStore(), Journal()
    controller = SessionController(store, journal, str(tmp_path), threading.RLock(), _directional_factory)
    text, _ = controller.run("SPY", now=NOW)
    assert "\npaper trade: " in text
    assert sum(1 for e in journal.entries() if e.kind == "fill") == 1
    store2, journal2 = SnapshotStore(), Journal()
    controller2 = SessionController(store2, journal2, str(tmp_path), threading.RLock(), _directional_factory)
    text2, _ = controller2.run("SPY", now=NOW, paper_trade=False)
    assert "paper trade: " not in text2
    assert not [e for e in journal2.entries() if e.kind == "fill"]


def test_session_endpoint_expands_to_stored_underlyings(tmp_path):
    store = SnapshotStore()
    store.save(ChainSnapshot(underlying="SPY", timestamp_utc=NOW, source_name="seed", latency_ms=1.0))
    journal = Journal()
    controller = SessionController(store, journal, str(tmp_path), threading.RLock(), default_context)
    response = route_http(
        "POST", "/session", {"underlying": "QQQ"}, store, journal, CONFIG, NOW,
        session_controller=controller,
    )
    assert response.status == 200
    body = json.loads(response.body)
    assert body["underlyings"] == ["QQQ", "SPY"]
    assert "QQQ" in body["reply"] and "SPY" in body["reply"]


def test_session_loop_picks_up_tickers_added_after_startup(tmp_path):
    store = SnapshotStore()
    store.save(ChainSnapshot(underlying="SPY", timestamp_utc=NOW, source_name="seed", latency_ms=1.0))
    journal = Journal()
    controller = SessionController(store, journal, str(tmp_path), threading.RLock(), default_context)
    target = datetime.now().astimezone() + timedelta(seconds=0.5)
    schedule = parse_schedule(
        f"{target.minute} {target.hour} * * {(target.weekday() + 1) % 7 or 7}"
    )
    halt = start_session_loop(controller, schedule, ["SPY"], interval_seconds=0.05)
    try:
        with controller.lock:
            store.save(ChainSnapshot(underlying="QQQ", timestamp_utc=NOW, source_name="seed", latency_ms=1.0))
        deadline = time.monotonic() + 10.0
        saved = []
        while time.monotonic() < deadline:
            with controller.lock:
                saved = [entry for entry in journal.entries() if entry.kind == "session_saved"]
            if saved:
                break
            time.sleep(0.05)
    finally:
        halt.set()
    assert saved, "session loop never fired"
    assert saved[-1].payload["underlyings"] == ["SPY", "QQQ"]


def test_run_app_session_flag_defaults():
    args = run_app.parse_args([])
    assert args.session_schedule == ""
    assert args.session_underlying is None


def test_run_app_passes_session_to_serve(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_serve(store, journal, config, host, port, store_dir, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(run_app, "live_brief", lambda *args, **kwargs: "brief text")
    monkeypatch.setattr(run_app, "serve", fake_serve)
    run_app.main(
        ["--store-dir", str(tmp_path), "--session-schedule", "30 7 * * 1-5",
         "--session-underlying", "QQQ"]
    )
    assert seen["session_schedule"] == "30 7 * * 1-5"
    assert seen["session_underlyings"] == ["QQQ"]


def test_run_app_session_underlying_defaults_to_underlying(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_serve(store, journal, config, host, port, store_dir, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(run_app, "live_brief", lambda *args, **kwargs: "brief text")
    monkeypatch.setattr(run_app, "serve", fake_serve)
    run_app.main(["--underlying", "SPY,QQQ", "--store-dir", str(tmp_path)])
    assert seen["session_schedule"] == ""
    assert seen["session_underlyings"] == ["SPY", "QQQ"]
