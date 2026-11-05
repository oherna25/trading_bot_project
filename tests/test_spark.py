from datetime import datetime, timezone

from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.spark import (
    CONTRADICTION,
    MATCH,
    UNVERIFIED,
    expected_direction,
    format_spark_block,
    spark_audit_response,
    verify_spark_claim,
)
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)


def _contract(strike, expiry, call_or_put, open_interest, gamma):
    return OptionContract(
        strike=strike, expiry=expiry, call_or_put=call_or_put,
        open_interest=open_interest, gamma=gamma,
    )


def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="SPY",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                _contract(100.0, "2026-11-20", "call", 1000.0, 0.05),
                _contract(100.0, "2026-11-20", "put", 2000.0, 0.03),
            ),
        )
    )
    store.save_bar("SPY", Bar(interval="1d", timestamp_utc=NOW, open=100.0,
                               high=103.0, low=99.0, close=102.0, volume=1200.0))
    return store


def test_verify_match_and_contradiction():
    assert verify_spark_claim("long", "long").status == MATCH
    assert verify_spark_claim("long", "short").status == CONTRADICTION
    assert verify_spark_claim("", "long").status == UNVERIFIED


def test_expected_direction_spot_vs_flip():
    assert expected_direction(105.0, 100.0, -5.0) == "long"
    assert expected_direction(95.0, 100.0, 5.0) == "short"
    assert expected_direction(None, None, 5.0) == "long"
    assert expected_direction(None, None, -5.0) == "short"
    assert expected_direction(None, None, 0.0) == "flat"


def test_spark_audit_offline_journals_verdict():
    store = _seeded_store()
    journal = Journal()
    text = spark_audit_response("audit", "SPY", store, journal, NOW, live_models=False)
    assert text.startswith("spark audit: OFF")
    kinds = [e.kind for e in journal.entries()]
    assert "spark_verdict" in kinds


def test_format_spark_block_marks_prose_unverified():
    verdict = verify_spark_claim("short", "long")
    assert verdict.status == CONTRADICTION
    block = format_spark_block(None, verdict, "long")
    assert "CONTRADICTION" in block


def _live_post(captured):
    def post(url, headers, payload, timeout):
        captured.append(payload)
        return {
            "choices": [{"message": {"content": "Thesis holds.\nDIRECTION: long\nCONFIDENCE: 0.6", "tool_calls": []}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    return post


def _claim_model_id(journal):
    claims = [e for e in journal.entries() if e.kind == "spark_claim"]
    assert claims, "expected a spark_claim journal entry"
    return claims[-1].payload.get("model_id")


def test_spark_audit_labels_wonder_when_meta_key_makes_the_call():
    store = _seeded_store()
    journal = Journal()
    captured: list = []
    env = {"META_MODEL_API_KEY": "test-key", "MODEL_DAY_BUDGET_USD": "5"}
    text = spark_audit_response(
        "audit", "SPY", store, journal, NOW,
        live_models=True, env=env, http_post=_live_post(captured),
    )
    assert captured and captured[0]["model"] == "muse-wonder-1.0"
    assert _claim_model_id(journal) == "muse-wonder-1.0"
    assert "model: muse-wonder-1.0" in text


def test_spark_audit_labels_spark_open_when_baseten_key_makes_the_call():
    store = _seeded_store()
    journal = Journal()
    captured: list = []
    env = {"BASETEN_API_KEY": "test-key"}
    text = spark_audit_response(
        "audit", "SPY", store, journal, NOW,
        live_models=True, env=env, http_post=_live_post(captured),
    )
    assert captured and captured[0]["model"] == "meta-models/muse-spark-open-1026"
    assert _claim_model_id(journal) == "meta-models/muse-spark-open-1026"
    assert "model: meta-models/muse-spark-open-1026" in text


def test_spark_audit_labels_local_model_when_local_tier_makes_the_call(tmp_path):
    weights = tmp_path / "glimmer.gguf"
    weights.write_bytes(b"fake")
    store = _seeded_store()
    journal = Journal()
    text = spark_audit_response(
        "audit", "SPY", store, journal, NOW,
        live_models=True, env={"GLIMMER_GGUF_PATH": str(weights)},
        local_runner=lambda *a: {"text": "local read\nDIRECTION: long\nCONFIDENCE: 0.8"},
    )
    assert _claim_model_id(journal) == "muse-glimmer-3b"
    assert "model: muse-glimmer-3b" in text


def test_spark_panel_shows_the_model_that_actually_claimed():
    from bot.spark import spark_panel_html

    store = _seeded_store()
    journal = Journal()
    env = {"META_MODEL_API_KEY": "test-key", "MODEL_DAY_BUDGET_USD": "5"}
    spark_audit_response(
        "audit", "SPY", store, journal, NOW,
        live_models=True, env=env, http_post=_live_post([]),
    )
    html = spark_panel_html(store, journal, "SPY")
    assert "muse-wonder-1.0" in html
    assert "muse-spark-1.3" not in html


def test_spark_panel_html_off_state_is_visual_and_readonly():
    from bot.spark import spark_panel_html

    journal = Journal()
    html = spark_panel_html(_seeded_store(), journal, "SPY")
    assert "spark-panel" in html
    assert "SPARK AUDIT" in html and "OFF" in html
    assert "Spark (live models)" in html and "Engine disposes" in html
    assert journal.entries() == []
