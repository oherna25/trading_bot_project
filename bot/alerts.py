from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime
from typing import Any, Mapping

from bot.config import read_dotenv as _read_dotenv
from bot.exposure import compute_gex
from bot.journal import Journal
from bot.localmodels import LocalRunner, local_available, run_local
from bot.models import MODEL_REGISTRY, HttpPost, model_api_key, receipt_payload, run_analyst
from bot.research import change_alerts
from bot.store import SnapshotStore
from bot.tiers import select_model_tier

DISCORD_ENV = "DISCORD_WEBHOOK_URL"
TELEGRAM_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ENV = "TELEGRAM_CHAT_ID"
WALL_PROXIMITY_PCT = 0.5
WALL_GROWTH_PCT_PER_HOUR = 25.0
WALL_GROWTH_TOP_WALLS = 3
ALERT_TIMEOUT_SECONDS = 15.0

NARRATE_SYSTEM = (
    "Explain this trading alert in one short sentence for the operator. "
    "Reply with that sentence only."
)
JUDGE_SYSTEM = (
    "Decide if this trading alert is worth waking the operator. "
    "Reply exactly: YES plus five words, or NO plus five words."
)


def _read_alert_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    if env is not None:
        return dict(env)
    file_values = _read_dotenv()
    out: dict[str, str] = {}
    for name in (DISCORD_ENV, TELEGRAM_TOKEN_ENV, TELEGRAM_CHAT_ENV):
        value = os.environ.get(name) or file_values.get(name)
        if value:
            out[name] = value
    return out


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> Any:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    try:
        return json.loads(body)
    except ValueError:
        return {}


def wall_proximity_alerts(
    underlying: str, store: SnapshotStore, journal: Journal, now: datetime
) -> list[str]:
    found = store.read_latest(underlying)
    if found is None:
        return []
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    spot = bar.close if bar is not None else None
    if not spot:
        return []
    profile = compute_gex(snapshot, snapshot_id, spot=spot)
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))
    if not walls:
        return []
    strike, wall = walls[0]
    distance_pct = abs(spot - strike) / spot * 100.0
    if distance_pct > WALL_PROXIMITY_PCT:
        return []
    text = f"ALERT: {underlying} is {distance_pct:.1f}% from the {strike:g} wall (${wall:,.0f})"
    journal.append(now, "alert", {"underlying": underlying, "text": text})
    return [text]


def _format_span(span_hours: float) -> str:
    minutes = span_hours * 60.0
    if minutes < 120.0:
        return f"{minutes:.0f}m"
    return f"{span_hours:.1f}h"


def wall_growth_alerts(
    underlying: str, store: SnapshotStore, journal: Journal, now: datetime
) -> list[str]:
    snaps = [
        (snapshot_id, snapshot)
        for snapshot_id, snapshot in store.snapshots_in_order()
        if snapshot.underlying == underlying
    ]
    if len(snaps) < 2:
        return []
    (prev_id, prev), (latest_id, latest) = snaps[-2], snaps[-1]
    span_hours = (latest.timestamp_utc - prev.timestamp_utc).total_seconds() / 3600.0
    if span_hours <= 0:
        return []
    bar = store.read_latest_bar(underlying)
    spot = bar.close if bar is not None else None
    before = compute_gex(prev, prev_id, spot=spot)
    after = compute_gex(latest, latest_id, spot=spot)
    walls = sorted(after.per_strike_exposure.items(), key=lambda item: -abs(item[1]))
    alerts = []
    for strike, value in walls[:WALL_GROWTH_TOP_WALLS]:
        old = before.per_strike_exposure.get(strike, 0.0)
        if old == 0.0:
            continue
        growth_pct = (abs(value) - abs(old)) / abs(old) * 100.0
        pace = growth_pct / span_hours
        if pace < WALL_GROWTH_PCT_PER_HOUR:
            continue
        text = (
            f"ALERT: {underlying} {strike:g} wall grew {growth_pct:+.0f}% "
            f"in {_format_span(span_hours)} ({pace:+.0f}%/h pace)"
        )
        journal.append(now, "alert", {"underlying": underlying, "text": text})
        alerts.append(text)
    return alerts


def collect_alerts(underlying: str, store: SnapshotStore, journal: Journal, now: datetime) -> list[str]:
    return (
        change_alerts(underlying, store, journal, now)
        + wall_proximity_alerts(underlying, store, journal, now)
        + wall_growth_alerts(underlying, store, journal, now)
    )


def fallback_narration(text: str) -> str:
    lowered = text.lower()
    if "regime flipped" in lowered:
        return "positioning flipped sides; check whether open trades still match the regime."
    if "spot crossed the flip" in lowered:
        return "spot crossed the flip; momentum may now work against old levels."
    if "stale" in lowered:
        return "the snapshot is old; refresh before trusting any level."
    if "wall grew" in lowered:
        return "a wall is growing fast; positioning is concentrating there."
    if "wall" in lowered:
        return "price is pressing a wall; expect chop or a break."
    return "review this alert against open positions before acting."


def narrate_alert(
    text: str,
    underlying: str,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    local_runner: LocalRunner | None = None,
) -> tuple[str, str]:
    if not live_models:
        journal.append(now, "narrate_skipped", {"reason": "live models off"})
        return fallback_narration(text), "rules"
    decision = select_model_tier("narrate", journal, now, env)
    try:
        if decision.tier == "local":
            reply, receipt = run_local(
                "narrate", f"{NARRATE_SYSTEM}\nAlert: {text}", env, runner=local_runner
            )
            receipts = [receipt]
            model_id = receipt.model_id
        elif decision.tier == "model":
            spec = MODEL_REGISTRY[decision.role]
            reply, receipts = run_analyst(
                spec,
                model_api_key(env),
                [
                    {"role": "system", "content": NARRATE_SYSTEM},
                    {"role": "user", "content": text},
                ],
                [],
                {},
                text,
                http_post,
            )
            model_id = spec.model_id
        else:
            journal.append(now, "narrate_skipped", {"reason": decision.reason})
            return fallback_narration(text), "rules"
    except Exception as exc:
        journal.append(now, "narrate_skipped", {"reason": type(exc).__name__})
        return fallback_narration(text), "rules"
    narration = " ".join(reply.split())
    if not narration:
        journal.append(now, "narrate_skipped", {"reason": "empty narration"})
        return fallback_narration(text), "rules"
    for receipt in receipts:
        journal.append(now, "model_receipt", receipt_payload(receipt, ""))
    journal.append(now, "alert_narrated", {"underlying": underlying, "source": model_id})
    return narration, model_id


def alert_noise(text: str, journal: Journal, now: datetime) -> str | None:
    if "stale" not in text.lower():
        return None
    seen = [
        entry
        for entry in journal.entries()
        if entry.kind == "alert"
        and entry.payload.get("text") == text
        and entry.timestamp_utc.date() == now.date()
    ]
    if len(seen) > 1:
        return "repeat stale alert today"
    return None


def judge_alert(
    text: str,
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None = None,
    local_runner: LocalRunner | None = None,
) -> tuple[bool, str]:
    if not local_available(env):
        return True, "no local judge"
    try:
        reply, receipt = run_local(
            "classify", f"{JUDGE_SYSTEM}\nAlert: {text}", env, runner=local_runner
        )
    except Exception:
        return True, "judge failed"
    journal.append(now, "model_receipt", receipt_payload(receipt, ""))
    note = " ".join(reply.split())[:80]
    return reply.strip().upper().startswith("YES"), note


def filter_noise(
    alerts: list[str],
    underlying: str,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    env: Mapping[str, str] | None = None,
    local_runner: LocalRunner | None = None,
) -> list[str]:
    kept = []
    for text in alerts:
        reason = alert_noise(text, journal, now)
        if reason is not None:
            journal.append(
                now, "alert_dropped", {"underlying": underlying, "text": text, "reason": reason}
            )
            continue
        if live_models:
            worthy, note = judge_alert(text, journal, now, env, local_runner)
            if not worthy:
                journal.append(
                    now,
                    "alert_dropped",
                    {"underlying": underlying, "text": text, "reason": f"judge: {note}"},
                )
                continue
        kept.append(text)
    return kept


def narrated_alerts(
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    local_runner: LocalRunner | None = None,
) -> list[str]:
    alerts = filter_noise(
        collect_alerts(underlying, store, journal, now),
        underlying, journal, now, live_models, env, local_runner,
    )
    lines = []
    for text in alerts:
        narration, source = narrate_alert(
            text, underlying, journal, now, live_models, env, http_post, local_runner
        )
        lines.append(f"{text}\nwhy: {narration} [{source}]")
    return lines


def _providers(env: Mapping[str, str]) -> list[tuple[str, str, dict[str, str]]]:
    providers = []
    webhook = env.get(DISCORD_ENV, "")
    if webhook:
        providers.append(("discord", webhook, {}))
    token = env.get(TELEGRAM_TOKEN_ENV, "")
    chat_id = env.get(TELEGRAM_CHAT_ENV, "")
    if token and chat_id:
        providers.append(("telegram", f"https://api.telegram.org/bot{token}/sendMessage", {}))
    return providers


def _payload(provider: str, text: str, env: Mapping[str, str]) -> dict[str, Any]:
    if provider == "telegram":
        return {"chat_id": env.get(TELEGRAM_CHAT_ENV, ""), "text": text}
    return {"content": text}


def push_alerts(
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    narrate: bool = False,
    quiet: bool = False,
    live_models: bool = False,
    local_runner: LocalRunner | None = None,
) -> list[str]:
    alerts = collect_alerts(underlying, store, journal, now)
    if quiet or narrate:
        alerts = filter_noise(alerts, underlying, journal, now, live_models, env, local_runner)
    values = _read_alert_env(env)
    providers = _providers(values)
    if not providers:
        return []
    post = http_post or _post_json
    sent_before = {
        (entry.payload.get("underlying"), entry.payload.get("text"))
        for entry in journal.entries()
        if entry.kind == "alert_sent"
    }
    pushed = []
    for text in alerts:
        if (underlying, text) in sent_before:
            continue
        send_text = text
        if narrate:
            narration, source = narrate_alert(
                text, underlying, journal, now, live_models, env, http_post, local_runner
            )
            send_text = f"{text}\nwhy: {narration} [{source}]"
        for name, url, headers in providers:
            try:
                post(url, headers, _payload(name, send_text, values), ALERT_TIMEOUT_SECONDS)
            except Exception as exc:
                journal.append(
                    now, "alert_failed", {"underlying": underlying, "provider": name, "reason": type(exc).__name__}
                )
                continue
            journal.append(now, "alert_sent", {"underlying": underlying, "text": text, "provider": name})
        pushed.append(send_text)
    return pushed
