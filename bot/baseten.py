from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime
from typing import Any, Mapping

from bot.config import read_dotenv as _read_dotenv
from bot.journal import Journal
from bot.models import HttpPost

API_KEY_ENV = "BASETEN_API_KEY"
MODEL_ID_ENV = "BASETEN_REASON_MODEL_ID"
REASON_TIMEOUT_SECONDS = 60.0


def _read_baseten_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    if env is not None:
        return dict(env)
    file_values = _read_dotenv()
    out: dict[str, str] = {}
    for name in (API_KEY_ENV, MODEL_ID_ENV):
        value = os.environ.get(name) or file_values.get(name)
        if value:
            out[name] = value
    return out


def predict_url(model_id: str) -> str:
    if model_id.startswith("http"):
        return model_id
    return f"https://model-{model_id}.api.baseten.co/environments/production/predict"


def select_reason_backend(
    journal: Journal, now: datetime, env: Mapping[str, str] | None = None
) -> tuple[str, str]:
    _ = (journal, now)
    values = _read_baseten_env(env)
    if not values.get(API_KEY_ENV):
        return "fallback", f"missing {API_KEY_ENV}"
    if not values.get(MODEL_ID_ENV):
        return "fallback", f"missing {MODEL_ID_ENV}"
    return "baseten", ""


def _urllib_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> Any:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def generate_reason(
    stats: dict[str, Any],
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    timeout: float = REASON_TIMEOUT_SECONDS,
) -> tuple[str, float]:
    values = _read_baseten_env(env)
    key = values.get(API_KEY_ENV, "")
    model_id = values.get(MODEL_ID_ENV, "")
    if not key:
        raise ValueError(f"missing {API_KEY_ENV}")
    if not model_id:
        raise ValueError(f"missing {MODEL_ID_ENV}")
    post = http_post or _urllib_post
    started = time.monotonic()
    body = post(
        predict_url(model_id),
        {"Authorization": f"Api-Key {key}", "Content-Type": "application/json"},
        {"stats": stats},
        timeout,
    )
    latency_ms = (time.monotonic() - started) * 1000.0
    reason = body.get("reason", "") if isinstance(body, dict) else ""
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        raise ValueError("empty reason")
    return reason, latency_ms
