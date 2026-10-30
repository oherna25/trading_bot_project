import json
from datetime import datetime, timezone

from bot.commands import (
    PORTFOLIO_USAGE,
    execute_portfolio_action,
    open_book,
    parse_portfolio_command,
    portfolio_response,
)
from bot.journal import Journal
from bot.shapes import Bar
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
ENV = {"META_MODEL_API_KEY": "key-1", "MODEL_DAY_BUDGET_USD": "5.0"}


def _bar(close):
    return Bar(interval="1d", timestamp_utc=NOW, open=close, high=close,
               low=close, close=close, volume=100.0)


def _seeded_book():
    store, journal = SnapshotStore(), Journal()
    store.save_bar("TEST", _bar(102.0))
    store.save_bar("WIDE", _bar(80.0))
    journal.append(NOW, "fill", {"symbol": "TEST", "side": "buy", "price": 100.0,
                                "size": 10.0, "venue": "paper"})
    journal.append(NOW, "fill", {"symbol": "WIDE", "side": "buy", "price": 100.0,
                                "size": 10.0, "venue": "paper"})
    journal.append(NOW, "fill", {"symbol": "GHOST", "side": "buy", "price": 50.0,
                                "size": 10.0, "venue": "replay"})
    return store, journal


def test_parse_close_losers_variants():
    assert parse_portfolio_command("close my losers", NOW) == {
        "action": "close_losers", "threshold_pct": 0.0}
    parsed = parse_portfolio_command("close my losers past 20%", NOW)
    assert parsed == {"action": "close_losers", "threshold_pct": 20.0}
    parsed = parse_portfolio_command("sell losers under -15%", NOW)
    assert parsed == {"action": "close_losers", "threshold_pct": 15.0}


def test_parse_close_winners_and_symbol():
    assert parse_portfolio_command("close my winners", NOW) == {
        "action": "close_winners", "threshold_pct": 0.0}
    assert parse_portfolio_command("close SPY", NOW) == {
        "action": "close_symbol", "symbol": "SPY"}
    assert parse_portfolio_command("close my test position", NOW) == {
        "action": "close_symbol", "symbol": "TEST"}


def test_parse_flatten_needs_confirm_word():
    assert parse_portfolio_command("flatten everything", NOW) == {
        "action": "flatten", "confirm": False}
    assert parse_portfolio_command("flatten everything confirm", NOW)["confirm"] is True


def test_parse_roll_variants():
    parsed = parse_portfolio_command("roll SPY put 760 to 2026-12-18", NOW)
    assert parsed == {"action": "roll", "symbol": "SPY", "kind": "put",
                      "strike": 760.0, "to_expiry": "2026-12-18"}
    parsed = parse_portfolio_command("roll my 760 puts to next friday", NOW)
    assert parsed["to_expiry"] == "2026-10-30"
    assert parsed["strike"] == 760.0 and parsed["symbol"] is None
    assert parse_portfolio_command("roll my puts to someday", NOW)["to_expiry"] is None


def test_parse_unknown():
    assert parse_portfolio_command("what is the weather", NOW)["action"] == "unknown"


def test_open_book_skips_replay():
    _, journal = _seeded_book()
    book = open_book(journal)
    assert sorted(entry["symbol"] for entry in book) == ["TEST", "WIDE"]
    wide = next(entry for entry in book if entry["symbol"] == "WIDE")
    assert wide["net"] == 10.0 and wide["avg"] == 100.0


def test_close_losers_closes_only_loser():
    store, journal = _seeded_book()
    reply = execute_portfolio_action({"action": "close_losers", "threshold_pct": 0.0},
                                     "TEST", store, journal, NOW)
    assert "WIDE" in reply and "closed" in reply
    assert "TEST" not in reply.split("closed")[0]
    sells = [e for e in journal.entries()
             if e.kind == "fill" and e.payload.get("side") == "sell"]
    assert len(sells) == 1 and sells[0].payload["symbol"] == "WIDE"


def test_close_losers_threshold_skips_small_loss():
    store, journal = _seeded_book()
    reply = execute_portfolio_action({"action": "close_losers", "threshold_pct": 50.0},
                                     "TEST", store, journal, NOW)
    assert "no losers past 50" in reply


def test_close_symbol():
    store, journal = _seeded_book()
    reply = execute_portfolio_action({"action": "close_symbol", "symbol": "TEST"},
                                     "TEST", store, journal, NOW)
    assert "TEST" in reply and "closed" in reply


def test_close_symbol_missing():
    store, journal = _seeded_book()
    reply = execute_portfolio_action({"action": "close_symbol", "symbol": "QQQ"},
                                     "TEST", store, journal, NOW)
    assert "no open QQQ" in reply


def test_flatten_arms_then_confirms():
    store, journal = _seeded_book()
    armed = execute_portfolio_action({"action": "flatten", "confirm": False},
                                     "TEST", store, journal, NOW)
    assert "confirm" in armed
    assert not [e for e in journal.entries() if e.payload.get("side") == "sell"]
    done = execute_portfolio_action({"action": "flatten", "confirm": True},
                                    "TEST", store, journal, NOW)
    assert "closed 2 positions" in done


def test_close_vetoed_by_size_gate():
    store = SnapshotStore()
    store.save_bar("BIG", _bar(10.0))
    journal = Journal()
    journal.append(NOW, "fill", {"symbol": "BIG", "side": "buy", "price": 10.0,
                                "size": 100000.0, "venue": "paper"})
    reply = execute_portfolio_action({"action": "close_symbol", "symbol": "BIG"},
                                     "BIG", store, journal, NOW)
    assert "rejected" in reply and "outside trade targets" in reply


def test_roll_closes_and_reopens():
    from bot.shapes import OperatorProfile

    store = SnapshotStore()
    store.save_profile(OperatorProfile(risk_tolerance="moderate", portfolio_size=50000.0,
                                       knowledge="intermediate", options_ok=True))
    journal = Journal()
    journal.append(NOW, "fill", {"symbol": "TEST", "side": "buy", "price": 2.5,
                                "size": 250.0, "venue": "paper",
                                "legs": [{"symbol": "TEST", "side": "buy", "size": 250.0,
                                          "strike": 760.0, "expiry": "2026-11-20",
                                          "call_or_put": "put"}]})
    reply = execute_portfolio_action(
        {"action": "roll", "symbol": "TEST", "kind": "put", "strike": 760.0,
         "to_expiry": "2026-12-18"},
        "TEST", store, journal, NOW)
    assert "rolled" in reply and "2026-12-18" in reply
    fills = [e for e in journal.entries() if e.kind == "fill"]
    assert len(fills) == 3
    assert fills[-1].payload["legs"][0]["expiry"] == "2026-12-18"


def test_roll_without_match():
    store, journal = _seeded_book()
    reply = execute_portfolio_action(
        {"action": "roll", "symbol": "TEST", "kind": "put", "strike": 1.0,
         "to_expiry": "2026-12-18"},
        "TEST", store, journal, NOW)
    assert "no open" in reply


def test_portfolio_response_unknown_shows_usage():
    store, journal = _seeded_book()
    assert portfolio_response("what is the weather", "TEST", store, journal, NOW) == PORTFOLIO_USAGE


def test_portfolio_response_runs_parser_match():
    store, journal = _seeded_book()
    reply = portfolio_response("close my losers", "TEST", store, journal, NOW)
    assert "paper venue only" in reply and "WIDE" in reply


def test_spark_assist_translates_unknown():
    bodies = [
        {"choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "call-1", "function": {
                "name": "get_positions", "arguments": "{}"}}]},
        }], "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
        {"choices": [{"message": {
            "content": '{"action": "close_losers", "threshold_pct": 5.0}'}}],
         "usage": {"prompt_tokens": 120, "completion_tokens": 15}},
    ]
    log = []

    def post(url, headers, payload, timeout):
        log.append(payload)
        assert payload["model"] == "muse-spark-1.3"
        return bodies[len(log) - 1]

    store, journal = _seeded_book()
    reply = portfolio_response("trim my red positions", "TEST", store, journal, NOW,
                               live_models=True, env=dict(ENV), http_post=post)
    assert "WIDE" in reply and "closed" in reply
    assert len(log) == 2


def test_spark_bad_json_shows_usage():
    def post(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": "no json here"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    store, journal = _seeded_book()
    reply = portfolio_response("trim my red positions", "TEST", store, journal, NOW,
                               live_models=True, env=dict(ENV), http_post=post)
    assert reply == PORTFOLIO_USAGE
