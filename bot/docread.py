from __future__ import annotations

import base64
import json
import urllib.request
from datetime import datetime
from typing import Any, Mapping

from bot.chartread import CHART_MAX_BYTES, image_kind, read_chart_image
from bot.journal import Journal
from bot.models import (
    MODEL_API_BASE_URL,
    MODEL_TIMEOUT_SECONDS,
    HttpPost,
    ModelSpec,
    model_api_key,
    prompt_hash,
    receipt_payload,
    select_backend,
)
from bot.shapes import ModelReceipt
from bot.store import SnapshotStore

SEGMENTER_SPEC = ModelSpec("sam-3.1", "/segmentations", "image", 0.01, 0.0, "read full image")

DOC_QUESTION_DEFAULT = "Extract the table: strikes, expiries, bids, asks, and open interest."

DOC_USAGE = (
    "send a document screenshot to POST /docread?underlying=SYM as raw png or jpeg bytes, "
    "or call the read_document tool with base64 image bytes"
)


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> Any:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _clean_box(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        box: dict[str, Any] = {
            "x": int(raw.get("x", 0)),
            "y": int(raw.get("y", 0)),
            "w": int(raw.get("w", 0)),
            "h": int(raw.get("h", 0)),
        }
    except (TypeError, ValueError):
        return None
    if box["w"] <= 0 or box["h"] <= 0:
        return None
    label = raw.get("label")
    box["label"] = str(label).strip() if label else "region"
    return box


def segment_image(
    image: bytes,
    journal: Journal,
    now: datetime,
    snapshot_id: str = "",
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    timeout: float = MODEL_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    # Same key plus day budget as every hosted role, so the artist gate doubles here.
    backend, reason = select_backend("artist", journal, now, env)
    if backend != "model":
        journal.append(now, "segment_skipped", {"reason": reason})
        return []
    post = http_post or _post_json
    body_b64 = base64.b64encode(image).decode("ascii")
    started = datetime.now().timestamp()
    try:
        response = post(
            MODEL_API_BASE_URL + SEGMENTER_SPEC.endpoint,
            {"Authorization": f"Bearer {model_api_key(env)}", "Content-Type": "application/json"},
            {"model": SEGMENTER_SPEC.model_id, "image_b64": body_b64, "task": "table"},
            timeout,
        )
    except Exception as exc:
        journal.append(now, "segment_skipped", {"reason": type(exc).__name__})
        return []
    latency_ms = (datetime.now().timestamp() - started) * 1000.0
    boxes = response.get("boxes", []) if isinstance(response, dict) else []
    regions = [box for raw in boxes if (box := _clean_box(raw)) is not None]
    receipt = ModelReceipt(
        model_id=SEGMENTER_SPEC.model_id,
        role_name="segmenter",
        prompt_hash=prompt_hash(body_b64[:64]),
        tokens_in=0,
        tokens_out=0,
        cost_usd=SEGMENTER_SPEC.price_in,
        latency_ms=latency_ms,
    )
    journal.append(now, "model_receipt", receipt_payload(receipt, snapshot_id))
    journal.append(now, "image_segmented", {"boxes": len(regions)})
    return regions


def read_document(
    image: bytes,
    question: str,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> str:
    if not live_models:
        journal.append(now, "doc_skipped", {"reason": "live models off"})
        return "document reading needs live models"
    if image_kind(image) is None:
        journal.append(now, "doc_skipped", {"reason": "not an image"})
        return "document rejected: not a png or jpeg"
    if len(image) > CHART_MAX_BYTES:
        journal.append(now, "doc_skipped", {"reason": "image too large"})
        return "document rejected: image too large"
    found = store.read_latest(underlying)
    snapshot_id = found[0] if found is not None else ""
    regions = segment_image(image, journal, now, snapshot_id, env, http_post)
    return read_chart_image(
        image, underlying, store, journal, now,
        live_models=True,
        question=question.strip() or DOC_QUESTION_DEFAULT,
        regions=regions,
        env=env,
        http_post=http_post,
    )
