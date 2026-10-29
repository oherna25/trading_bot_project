import os

import run_app
from bot.feeds import FeedUnavailableError


def test_parse_args_defaults():
    args = run_app.parse_args([])
    assert args.underlying == "SPY"
    assert args.store_dir == "data/trading-bot"
    assert args.host == "0.0.0.0"
    assert args.port == 8000


def test_parse_args_overrides():
    args = run_app.parse_args(
        ["--underlying", "QQQ", "--store-dir", "/tmp/bundle", "--host", "127.0.0.1", "--port", "9001"]
    )
    assert args.underlying == "QQQ"
    assert args.store_dir == "/tmp/bundle"
    assert args.host == "127.0.0.1"
    assert args.port == 9001


def test_main_serves_loaded_bundle(monkeypatch, capsys, tmp_path):
    seen = {}

    def fake_live_brief(underlying, store, journal, now):
        seen["underlying"] = underlying
        seen["store"] = store
        seen["journal"] = journal
        return "brief text"

    def fake_serve(store, journal, config, host, port):
        seen["serve_store"] = store
        seen["serve_journal"] = journal
        seen["config"] = config
        seen["host"] = host
        seen["port"] = port

    monkeypatch.setattr(run_app, "live_brief", fake_live_brief)
    monkeypatch.setattr(run_app, "serve", fake_serve)
    result = run_app.main(
        ["--underlying", "QQQ", "--store-dir", str(tmp_path), "--host", "127.0.0.1", "--port", "9001"]
    )
    assert result == "brief text"
    assert seen["underlying"] == "QQQ"
    assert seen["serve_store"] is seen["store"]
    assert seen["serve_journal"] is seen["journal"]
    assert seen["config"].dashboard_base_url == "https://ops.example.internal"
    assert seen["config"].dashboard_link_expiry_seconds == 3600
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
    seen = {}

    def failing_live_brief(underlying, store, journal, now):
        raise FeedUnavailableError("all chain feeds failed for QQQ")

    def fake_serve(store, journal, config, host, port):
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
