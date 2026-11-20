from __future__ import annotations

from datetime import datetime
from typing import Mapping

from bot.chartread import CHART_MAX_BYTES, image_kind, read_chart_image
from bot.journal import Journal
from bot.models import HttpPost
from bot.store import SnapshotStore

DOC_QUESTION_DEFAULT = "Extract the table: strikes, expiries, bids, asks, and open interest."

DOC_USAGE = (
    "send a document screenshot to POST /docread?underlying=SYM as raw png or jpeg bytes, "
    "or call the read_document tool with base64 image bytes"
)


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
    return read_chart_image(
        image, underlying, store, journal, now,
        live_models=True,
        question=question.strip() or DOC_QUESTION_DEFAULT,
        env=env,
        http_post=http_post,
    )
