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
INFERENCE_CHAT_URL = "https://inference.baseten.co/v1/chat/completions"
REASON_MODEL_DEFAULT = "meta-models/muse-spark-open-1026"
REASON_MAX_TOKENS = 1000
REASON_REASONING = {"effort": "low"}
REASON_SYSTEM_PROMPT = (
    "Write one sentence stating why this options trade is a good trade. "
    "Reply with that sentence only."
)
AGENT_SYSTEM_PROMPT = (
    "You rank premium-selling candidates already filtered by deterministic rules. "
    "Pick one candidate index, or -1 to skip today. Prefer defined risk, nearer "
    "delta to 0.20, higher annualized return, and nearer expiry only as a tiebreak. "
    'Reply with JSON only: {"index": 0, "rationale": "one sentence"}.'
)


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


def is_model_slug(model_id: str) -> bool:
    return "/" in model_id and not model_id.startswith("http")


def predict_url(model_id: str) -> str:
    if model_id.startswith("http"):
        return model_id
    if is_model_slug(model_id):
        return INFERENCE_CHAT_URL
    return f"https://model-{model_id}.api.baseten.co/environments/production/predict"


def select_reason_backend(
    journal: Journal, now: datetime, env: Mapping[str, str] | None = None
) -> tuple[str, str]:
    _ = (journal, now)
    values = _read_baseten_env(env)
    if not values.get(API_KEY_ENV):
        return "fallback", f"missing {API_KEY_ENV}"
    return "baseten", ""


def _urllib_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> Any:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _chat_payload(model_id: str, stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": model_id,
        "messages": [
            {"role": "system", "content": REASON_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(stats)},
        ],
        "max_tokens": REASON_MAX_TOKENS,
        "reasoning": dict(REASON_REASONING),
    }


def _chat_reason(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content", "") if isinstance(message, dict) else ""
    return content.strip() if isinstance(content, str) else ""


def _predict_reason(body: Any) -> str:
    reason = body.get("reason", "") if isinstance(body, dict) else ""
    return reason.strip() if isinstance(reason, str) else ""


def reason_model_id(env: Mapping[str, str] | None = None) -> str:
    return _read_baseten_env(env).get(MODEL_ID_ENV, "") or REASON_MODEL_DEFAULT


def _parse_pick(body: Any) -> tuple[int, str]:
    if isinstance(body, dict) and "index" in body:
        rationale = body.get("rationale") or body.get("reason") or ""
        return int(body["index"]), str(rationale).strip()  # type: ignore[arg-type]
    content = _chat_reason(body)
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("agent returned no JSON")
    parsed = json.loads(content[start : end + 1])
    if not isinstance(parsed, dict) or "index" not in parsed:
        raise ValueError("agent returned bad JSON")
    return int(parsed["index"]), str(parsed.get("rationale") or "").strip()


def generate_pick(
    candidates: list[dict[str, Any]],
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    timeout: float = REASON_TIMEOUT_SECONDS,
) -> tuple[int, str, float, str]:
    values = _read_baseten_env(env)
    key = values.get(API_KEY_ENV, "")
    model_id = values.get(MODEL_ID_ENV, "") or REASON_MODEL_DEFAULT
    if not key:
        raise ValueError(f"missing {API_KEY_ENV}")
    if not candidates:
        raise ValueError("no candidates to pick from")
    post = http_post or _urllib_post
    started = time.monotonic()
    if is_model_slug(model_id):
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"candidates": candidates})},
            ],
            "max_tokens": REASON_MAX_TOKENS,
            "reasoning": dict(REASON_REASONING),
        }
        body = post(
            predict_url(model_id),
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            payload,
            timeout,
        )
    else:
        body = post(
            predict_url(model_id),
            {"Authorization": f"Api-Key {key}", "Content-Type": "application/json"},
            {"candidates": candidates},
            timeout,
        )
    latency_ms = (time.monotonic() - started) * 1000.0
    index, rationale = _parse_pick(body)
    if not rationale:
        raise ValueError("empty agent rationale")
    return index, rationale, latency_ms, model_id


def generate_reason(
    stats: dict[str, Any],
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    timeout: float = REASON_TIMEOUT_SECONDS,
) -> tuple[str, float]:
    values = _read_baseten_env(env)
    key = values.get(API_KEY_ENV, "")
    model_id = values.get(MODEL_ID_ENV, "") or REASON_MODEL_DEFAULT
    if not key:
        raise ValueError(f"missing {API_KEY_ENV}")
    post = http_post or _urllib_post
    started = time.monotonic()
    if is_model_slug(model_id):
        body = post(
            predict_url(model_id),
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            _chat_payload(model_id, stats),
            timeout,
        )
        reason = _chat_reason(body)
    else:
        body = post(
            predict_url(model_id),
            {"Authorization": f"Api-Key {key}", "Content-Type": "application/json"},
            {"stats": stats},
            timeout,
        )
        reason = _predict_reason(body)
    latency_ms = (time.monotonic() - started) * 1000.0
    if not reason:
        raise ValueError("empty reason")
    return reason, latency_ms
