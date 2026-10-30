from datetime import datetime, timezone

from bot.alerts import collect_alerts, push_alerts, wall_proximity_alerts
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
ENV = {
    "DISCORD_WEBHOOK_URL": "https://discord.example/hook",
    "TELEGRAM_BOT_TOKEN": "tok-1",
    "TELEGRAM_CHAT_ID": "chat-1",
}


def _seeded_store(close=100.0) -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=5000.0,
                    gamma=0.05, price=2.50, bid=2.40, ask=2.60, iv=0.30,
                ),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=close, high=close, low=close, close=close, volume=1000.0))
    return store


def test_wall_proximity_fires_near_and_silent_far():
    journal = Journal()
    alerts = wall_proximity_alerts("TEST", _seeded_store(close=100.2), journal, NOW)
    assert len(alerts) == 1
    assert "0.2% from the 100 wall" in alerts[0]
    assert journal.entries()[-1].kind == "alert"
    assert wall_proximity_alerts("TEST", _seeded_store(close=110.0), Journal(), NOW) == []


def test_push_alerts_sends_once_per_provider_then_dedups():
    calls = []

    def stub(url, headers, payload, timeout):
        calls.append((url, payload))
        return {}

    journal = Journal()
    store = _seeded_store(close=100.2)
    pushed = push_alerts("TEST", store, journal, NOW, dict(ENV), stub)
    assert pushed != []
    urls = [url for url, _ in calls]
    assert "https://discord.example/hook" in urls
    assert "https://api.telegram.org/bottok-1/sendMessage" in urls
    assert [entry.kind for entry in journal.entries()].count("alert_sent") == 2 * len(pushed)
    calls.clear()
    assert push_alerts("TEST", store, journal, NOW, dict(ENV), stub) == []
    assert calls == []


def test_push_alerts_silent_without_providers():
    journal = Journal()
    assert push_alerts("TEST", _seeded_store(close=100.2), journal, NOW, {}, None) == []
    assert [e.kind for e in journal.entries()].count("alert_sent") == 0


def test_push_alerts_failure_journals_without_raising():
    def failing(*args):
        raise RuntimeError("down")

    journal = Journal()
    pushed = push_alerts(
        "TEST", _seeded_store(close=100.2), journal, NOW,
        {"DISCORD_WEBHOOK_URL": "https://discord.example/hook"}, failing,
    )
    assert pushed != []
    assert journal.entries()[-1].kind == "alert_failed"


def test_collect_alerts_includes_regime_flip():
    from datetime import timedelta

    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW - timedelta(minutes=15),
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=5000.0,
                    gamma=0.05, price=2.50, bid=2.40, ask=2.60, iv=0.30,
                ),
            ),
        )
    )
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="put", open_interest=5000.0,
                    gamma=0.05, price=2.50, bid=2.40, ask=2.60, iv=0.30,
                ),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=110.0, high=110.0, low=110.0, close=110.0, volume=1000.0))
    alerts = collect_alerts("TEST", store, Journal(), NOW)
    assert any("regime flipped long-gamma -> short-gamma" in text for text in alerts)
