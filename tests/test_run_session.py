import run_session


def test_runner_defaults_match_deploy_schedule():
    assert run_session.STORE_DIR == "data/trading-bot"
    assert run_session.SCHEDULE == "30 7 * * 1-5"


def test_parse_args_defaults():
    args = run_session.parse_args([])
    assert args.underlying == "SPY"
    assert args.store_dir == "data/trading-bot"
    assert args.paper_trade is True


def test_parse_args_no_paper_trade_disables():
    assert run_session.parse_args(["--no-paper-trade"]).paper_trade is False
    assert run_session.parse_args(["--paper-trade"]).paper_trade is True


def test_parse_args_overrides():
    args = run_session.parse_args(["--underlying", "QQQ", "--store-dir", "/tmp/bundle"])
    assert args.underlying == "QQQ"
    assert args.store_dir == "/tmp/bundle"


def test_main_wires_args_into_run(monkeypatch, capsys):
    seen = {}

    def fake_run(underlying, store_dir, paper_trade):
        seen["underlying"] = underlying
        seen["store_dir"] = store_dir
        seen["paper_trade"] = paper_trade
        return "brief text"

    monkeypatch.setattr(run_session, "run", fake_run)
    assert run_session.main(["--underlying", "QQQ", "--store-dir", "/tmp/bundle"]) == "brief text"
    assert seen == {"underlying": "QQQ", "store_dir": "/tmp/bundle", "paper_trade": True}
    assert capsys.readouterr().out.strip() == "brief text"
