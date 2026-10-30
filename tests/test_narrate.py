from datetime import datetime, timedelta, timezone

from bot.alerts import (
    fallback_narration,
    filter_noise,
    narrate_alert,
    narrated_alerts,
    push_alerts,
)
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
ENV = {"META_MODEL_API_KEY": "key-1", "MODEL_DAY_BUDGET_USD": "5.0"}


def _snapshot(at):
    return ChainSnapshot(
        underlying="TEST", timestamp_utc=at, source_name="tradier_sandbox", latency_ms=9.0,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                           open_interest=1000.0, gamma=0.05),
        ),
    )


def _stale_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(_snapshot(NOW - timedelta(hours=7)))
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW - timedelta(hours=7),
                               open=90.0, high=91.0, low=89.0, close=90.0, volume=500.0))
    return store


def _narrate_stub(seen, content="walls are pressing price"):
    def post(url, headers, payload, timeout):
        seen.append(payload)
        assert payload["model"] == "muse-spark-1.3"
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 12},
        }

    return post


def test_fallback_narration_covers_kinds():
    assert "flipped" in fallback_narration("ALERT: regime flipped short-gamma -> long-gamma")
    assert "flip" in fallback_narration("ALERT: spot crossed the flip (99.0 -> 101.0)")
    assert "old" in fallback_narration("ALERT: snapshot is stale (age 7h00m)")
    assert "wall" in fallback_narration("ALERT: TEST is 0.1% from the 100 wall ($1,000)")
    assert "growing" in fallback_narration("ALERT: TEST 100 wall grew +40% in 1h (+40%/h pace)")


def test_narrate_offline_uses_rules():
    journal = Journal()
    narration, source = narrate_alert("ALERT: regime flipped a -> b", "TEST", journal, NOW)
    assert source == "rules" and narration
    assert journal.entries()[-1].kind == "narrate_skipped"


def test_narrate_live_uses_spark():
    seen = []
    journal = Journal()
    narration, source = narrate_alert(
        "ALERT: regime flipped a -> b", "TEST", journal, NOW,
        live_models=True, env=dict(ENV), http_post=_narrate_stub(seen),
    )
    assert narration == "walls are pressing price"
    assert source == "muse-spark-1.3"
    assert "model_receipt" in [entry.kind for entry in journal.entries()]


def test_narrate_live_error_falls_back():
    def down(*args, **kwargs):
        raise RuntimeError("spark down")

    journal = Journal()
    narration, source = narrate_alert(
        "ALERT: regime flipped a -> b", "TEST", journal, NOW,
        live_models=True, env=dict(ENV), http_post=down,
    )
    assert source == "rules" and narration


def test_filter_drops_repeat_stale_same_day():
    journal = Journal()
    text = "ALERT: snapshot is stale (age 7h00m)"
    journal.append(NOW - timedelta(hours=1), "alert", {"underlying": "TEST", "text": text})
    journal.append(NOW, "alert", {"underlying": "TEST", "text": text})
    kept = filter_noise([text], "TEST", journal, NOW)
    assert kept == []
    assert journal.entries()[-1].kind == "alert_dropped"


def test_filter_keeps_first_stale_and_flips():
    journal = Journal()
    stale = "ALERT: snapshot is stale (age 7h00m)"
    flip = "ALERT: regime flipped a -> b"
    assert filter_noise([stale, flip], "TEST", journal, NOW) == [stale, flip]


def test_judge_drops_unworthy_with_local(tmp_path):
    weights = tmp_path / "glimmer.gguf"
    weights.write_bytes(b"gguf-stub")
    journal = Journal()
    env = {"GLIMMER_GGUF_PATH": str(weights)}
    kept = filter_noise(
        ["ALERT: snapshot is stale (age 7h00m)"], "TEST", journal, NOW,
        live_models=True, env=env,
        local_runner=lambda *a: {"text": "NO already seen today ok"},
    )
    assert kept == []
    assert journal.entries()[-1].kind == "alert_dropped"


def test_judge_keeps_worthy_with_local(tmp_path):
    weights = tmp_path / "glimmer.gguf"
    weights.write_bytes(b"gguf-stub")
    journal = Journal()
    env = {"GLIMMER_GGUF_PATH": str(weights)}
    kept = filter_noise(
        ["ALERT: regime flipped a -> b"], "TEST", journal, NOW,
        live_models=True, env=env,
        local_runner=lambda *a: {"text": "YES regime changed sides now"},
    )
    assert kept == ["ALERT: regime flipped a -> b"]


def test_narrated_alerts_compose_lines():
    journal = Journal()
    lines = narrated_alerts("TEST", _stale_store(), journal, NOW)
    assert len(lines) == 1
    assert "why:" in lines[0] and "[rules]" in lines[0]


def test_push_alerts_narrate_sends_reason():
    sent = []

    def post(url, headers, payload, timeout):
        sent.append(payload)
        return {}

    journal = Journal()
    env = {"DISCORD_WEBHOOK_URL": "https://discord.example/hook"}
    pushed = push_alerts("TEST", _stale_store(), journal, NOW, env=env, http_post=post, narrate=True)
    assert len(pushed) == 1 and "why:" in pushed[0]
    assert "why:" in sent[0]["content"]
    recorded = [e for e in journal.entries() if e.kind == "alert_sent"]
    assert recorded and "why:" not in recorded[0].payload["text"]
