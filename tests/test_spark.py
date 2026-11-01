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
