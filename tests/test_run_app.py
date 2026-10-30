import os

import run_app
from bot.feeds import FeedUnavailableError


def test_parse_args_defaults():
    args = run_app.parse_args([])
    assert args.underlying == "SPY"
    assert args.store_dir == "data/trading-bot"
    assert args.host == "0.0.0.0"
    assert args.port is None
    assert args.refresh_seconds == 900.0


def test_parse_args_overrides():
    args = run_app.parse_args(
        ["--underlying", "QQQ", "--store-dir", "/tmp/bundle", "--host", "127.0.0.1", "--port", "9001",
         "--refresh-seconds", "60"]
    )
    assert args.underlying == "QQQ"
    assert args.store_dir == "/tmp/bundle"
    assert args.host == "127.0.0.1"
    assert args.port == 9001
    assert args.refresh_seconds == 60.0


def test_main_serves_loaded_bundle(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_live_brief(underlying, store, journal, now, paper_trade=False):
        seen["underlying"] = underlying
        seen["store"] = store
        seen["journal"] = journal
        seen["paper_trade"] = paper_trade
        return "brief text"

    def fake_serve(store, journal, config, host, port, store_dir, **kwargs):
        seen["serve_store"] = store
        seen["serve_journal"] = journal
        seen["config"] = config
        seen["host"] = host
        seen["port"] = port
        seen["store_dir"] = store_dir

    monkeypatch.setattr(run_app, "live_brief", fake_live_brief)
    monkeypatch.setattr(run_app, "serve", fake_serve)
    result = run_app.main(
        ["--underlying", "QQQ", "--store-dir", str(tmp_path), "--host", "127.0.0.1", "--port", "9001"]
    )
    assert result == "brief text"
    assert seen["underlying"] == "QQQ"
    assert seen["paper_trade"] is False
    assert seen["serve_store"] is seen["store"]
    assert seen["serve_journal"] is seen["journal"]
    assert seen["store_dir"] == str(tmp_path)
    assert seen["config"].max_day_loss == 1000.0
    assert seen["config"].day_profit_target == 500.0
    assert seen["config"].max_trade_size == 5000.0
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 9001
    out = capsys.readouterr().out
    assert "brief text" in out
    assert "127.0.0.1" in out and "9001" in out
    assert os.path.exists(tmp_path / "snapshots.json")


def test_main_feed_failure_still_serves(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.chdir(tmp_path)
    seen = {}

    def failing_live_brief(underlying, store, journal, now, paper_trade=False):
        raise FeedUnavailableError("all chain feeds failed for QQQ")

    def fake_serve(store, journal, config, host, port, store_dir, **kwargs):
        seen["called"] = True
        seen["host"] = host
        seen["port"] = port

    monkeypatch.setattr(run_app, "live_brief", failing_live_brief)
    monkeypatch.setattr(run_app, "serve", fake_serve)
    result = run_app.main(["--underlying", "QQQ", "--store-dir", str(tmp_path)])
    assert "all chain feeds failed for QQQ" in result
    assert seen == {"called": True, "host": "0.0.0.0", "port": 8000}
    out = capsys.readouterr().out
    assert "all chain feeds failed for QQQ" in out
    assert "0.0.0.0" in out and "8000" in out
    assert os.path.exists(tmp_path / "journal.json")


def test_main_port_env_flows_to_serve(monkeypatch, tmp_path):
    monkeypatch.setenv("PORT", "9002")
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_serve(store, journal, config, host, port, store_dir, **kwargs):
        seen["host"] = host
        seen["port"] = port

    monkeypatch.setattr(run_app, "live_brief", lambda *args, **kwargs: "brief text")
    monkeypatch.setattr(run_app, "serve", fake_serve)
    run_app.main(["--store-dir", str(tmp_path)])
    assert seen == {"host": "0.0.0.0", "port": 9002}


def test_main_wires_saved_profile_into_limits(monkeypatch, tmp_path):
    from bot.journal import Journal
    from bot.persist import save_bundle
    from bot.profile import build_profile
    from bot.store import SnapshotStore

    monkeypatch.chdir(tmp_path)
    store = SnapshotStore()
    store.save_profile(
        build_profile(
            {
                "risk_tolerance": "moderate",
                "portfolio_size": "50000",
                "knowledge": "intermediate",
                "options_ok": "yes",
            }
        )
    )
    save_bundle(str(tmp_path), store, Journal())
    seen = {}

    def fake_serve(store, journal, config, host, port, store_dir, **kwargs):
        seen["config"] = config

    monkeypatch.setattr(run_app, "live_brief", lambda *args, **kwargs: "brief text")
    monkeypatch.setattr(run_app, "serve", fake_serve)
    run_app.main(["--store-dir", str(tmp_path)])
    assert seen["config"].max_day_loss == 1000.0
    assert seen["config"].max_trade_size == 2500.0


def test_main_briefs_multiple_underlyings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    seen = []

    def fake_live_brief(underlying, store, journal, now, paper_trade=False):
        seen.append((underlying, paper_trade))
        return f"brief {underlying}"

    monkeypatch.setattr(run_app, "live_brief", fake_live_brief)
    monkeypatch.setattr(run_app, "serve", lambda *args, **kwargs: None)
    result = run_app.main(["--underlying", "SPY,QQQ", "--store-dir", str(tmp_path)])
    assert seen == [("SPY", False), ("QQQ", False)]
    assert result == "brief SPY\n---\nbrief QQQ"


def test_main_startup_briefs_stored_symbols_requested_first(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from bot.journal import Journal
    from bot.persist import save_bundle
    from bot.shapes import ChainSnapshot
    from bot.store import SnapshotStore

    monkeypatch.chdir(tmp_path)
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="QQQ", timestamp_utc=datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc),
            source_name="tradier_sandbox", latency_ms=9.0, contracts_list=(),
        )
    )
    save_bundle(str(tmp_path), store, Journal())
    seen = []

    def fake_live_brief(underlying, store, journal, now, paper_trade=False):
        seen.append((underlying, paper_trade))
        return f"brief {underlying}"

    monkeypatch.setattr(run_app, "live_brief", fake_live_brief)
    monkeypatch.setattr(run_app, "serve", lambda *args, **kwargs: None)
    result = run_app.main(["--underlying", "SPY", "--store-dir", str(tmp_path)])
    assert seen == [("SPY", False), ("QQQ", False)]
    assert result == "brief SPY\n---\nbrief QQQ"


def test_main_passes_refresh_to_serve(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_serve(store, journal, config, host, port, store_dir, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(run_app, "live_brief", lambda *args, **kwargs: "brief text")
    monkeypatch.setattr(run_app, "serve", fake_serve)
    run_app.main(["--underlying", "SPY,QQQ", "--store-dir", str(tmp_path), "--refresh-seconds", "60"])
    assert seen == {"refresh_seconds": 60.0, "refresh_symbols": ["SPY", "QQQ"],
                    "session_schedule": "", "session_underlyings": ["SPY", "QQQ"]}


def test_materialize_file_secrets_prefers_real_env(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "FROM_FILE").write_text("file-value\n")
    (secrets / "FROM_BOTH").write_text("file-value\n")
    monkeypatch.setenv("FROM_BOTH", "env-value")
    monkeypatch.delenv("FROM_FILE", raising=False)
    run_app.materialize_file_secrets(str(secrets))
    assert os.environ["FROM_FILE"] == "file-value"
    assert os.environ["FROM_BOTH"] == "env-value"


def test_materialize_file_secrets_missing_dir_skips(tmp_path):
    run_app.materialize_file_secrets(str(tmp_path / "no-secrets-here"))
