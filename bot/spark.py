"""Muse Spark audit block: model proposes, engine disposes.

Spark is the hero for the Meta hackathon. The analyst model proposes a
direction and confidence. This module checks that claim against the
computed GEX profile and journals both sides. Deterministic math always
has the last word.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional

from bot.exposure import compute_gex
from bot.ideas import resolve_direction
from bot.journal import Journal
from bot.models import HttpPost
from bot.store import SnapshotStore, need_snapshot_text


MATCH = "MATCH"
CONTRADICTION = "CONTRADICTION"
UNVERIFIED = "UNVERIFIED"
OFF = "OFF"


@dataclass(frozen=True)
class SparkClaim:
    direction: str
    confidence: float
    thesis: str
    model_id: str


@dataclass(frozen=True)
class SparkVerdict:
    status: str
    detail: str


def verify_spark_claim(claimed: str, expected: str) -> SparkVerdict:
    claimed = (claimed or "").strip().lower()
    expected = (expected or "").strip().lower()
    if not claimed or not expected:
        return SparkVerdict(UNVERIFIED, "missing direction on one side")
    if claimed == expected:
        return SparkVerdict(MATCH, f"model {claimed} matches engine {expected}")
    return SparkVerdict(CONTRADICTION, f"model {claimed} vs engine {expected}")


def expected_direction(
    bar_close: Optional[float],
    flip_point: Optional[float],
    total_exposure: float,
) -> str:
    if flip_point is not None and bar_close is not None:
        return "long" if bar_close > flip_point else "short"
    if total_exposure > 0:
        return "long"
    if total_exposure < 0:
        return "short"
    return "flat"


def format_spark_block(
    claim: Optional[SparkClaim], verdict: SparkVerdict, expected: str
) -> str:
    if claim is None:
        return f"spark audit: {verdict.status} -- {verdict.detail}"
    lines = [
        f"spark audit: {verdict.status} (model {claim.direction} vs engine {expected})",
        f"model: {claim.model_id} confidence {claim.confidence:.2f}",
        f"thesis: {claim.thesis}",
        f"check: {verdict.detail}",
        "note: free prose is unverified, only direction vs engine counts",
    ]
    return "\n".join(lines)


def spark_audit_response(
    text: str,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    grounding: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    local_runner=None,
) -> str:
    from bot.research_analyst import analyst_backend

    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(need_snapshot_text(underlying, store))
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    spot = bar.close if bar is not None else None
    profile = compute_gex(snapshot, snapshot_id, spot=spot)
    expected = resolve_direction(bar, profile.flip_point, profile.total_exposure)

    signal = analyst_backend(
        text or f"spark audit on {underlying}",
        snapshot_id,
        snapshot,
        profile,
        bar,
        journal,
        now,
        live_models,
        grounding,
        env,
        http_post,
        local_runner,
    )
    if not live_models:
        verdict = SparkVerdict(OFF, "live models off, deterministic engine only")
        journal.append(
            now, "spark_verdict", {"status": verdict.status, "expected": expected}
        )
        return format_spark_block(None, verdict, expected)

    claim = SparkClaim(
        direction=signal.direction,
        confidence=signal.confidence,
        thesis=signal.thesis,
        model_id="muse-spark-1.3",
    )
    journal.append(
        now,
        "spark_claim",
        {
            "direction": claim.direction,
            "confidence": claim.confidence,
            "model_id": claim.model_id,
            "snapshot_id": snapshot_id,
        },
    )
    verdict = verify_spark_claim(claim.direction, expected)
    journal.append(
        now,
        "spark_verdict",
        {"status": verdict.status, "expected": expected, "detail": verdict.detail},
    )
    return format_spark_block(claim, verdict, expected)
