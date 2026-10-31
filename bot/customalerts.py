from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from bot.exposure import compute_gex
from bot.journal import Journal
from bot.store import SnapshotStore

RULE_USAGE = (
    "alert rules sound like alert me if SPY breaks 750 or alert me if RKLB crosses its flip "
    "-- next: chat alert me if TEST breaks 750"
)

_BREAK_RE = re.compile(
    r"\b([A-Za-z]{1,6})\s+"
    r"(breaks?|crosses?|closes?|rises?|falls?|drops?|pops?|taps?|hits?|touches?)"
    r"(?:\s+(above|below|over|under|through))?"
    r"\s+\$?(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_ABOVE_BELOW_RE = re.compile(
    r"\b([A-Za-z]{1,6})\s+(above|below)\s+\$?(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_FLIP_RE = re.compile(
    r"\b([A-Za-z]{1,6})\s+(?:crosses?|breaks?|closes?|holds?|loses?|reclaims?)\s+"
    r"(?:its|the)\s+flip\b",
    re.IGNORECASE,
)


def parse_alert_rule(text: str) -> dict[str, Any]:
    match = _FLIP_RE.search(text or "")
    if match:
        return {"symbol": match.group(1).upper(), "kind": "flip_cross", "level": None,
                "direction": "either"}
    match = _BREAK_RE.search(text or "")
    if match:
        symbol, _verb, side, level = match.groups()
        direction = "either"
        if side is not None:
            direction = "above" if side.lower() in ("above", "over") else "below"
        return {"symbol": symbol.upper(), "kind": "break", "level": float(level),
                "direction": direction}
    match = _ABOVE_BELOW_RE.search(text or "")
    if match:
        symbol, side, level = match.groups()
        direction = "above" if side.lower() == "above" else "below"
        return {"symbol": symbol.upper(), "kind": "break", "level": float(level),
                "direction": direction}
    raise ValueError(f"could not read that alert rule -- {RULE_USAGE}")


def describe_rule(rule: dict[str, Any]) -> str:
    symbol = str(rule.get("symbol", "")).upper()
    if rule.get("kind") == "flip_cross":
        return f"{symbol} crosses its flip"
    level = rule.get("level")
    level_text = f"{float(level):g}" if level is not None else "?"
    direction = str(rule.get("direction", "either"))
    if direction == "above":
        return f"{symbol} breaks above {level_text}"
    if direction == "below":
        return f"{symbol} breaks below {level_text}"
    return f"{symbol} breaks {level_text}"


def set_rule_response(text: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    try:
        parsed = parse_alert_rule(text)
    except ValueError as exc:
        return str(exc)
    saved = store.save_alert_rule(parsed)
    journal.append(now, "alert_rule_set", {"rule_id": saved["id"], "rule": describe_rule(saved)})
    return (
        f"rule {saved['id']}: alert me if {describe_rule(saved)} -- "
        f"next: chat list alert rules to see it fire on ingest and session"
    )


def list_rules_text(store: SnapshotStore) -> str:
    rules = store.list_alert_rules()
    if not rules:
        return f"no alert rules saved -- {RULE_USAGE}"
    lines = [f"alert rules ({len(rules)}):"]
    for rule in rules:
        lines.append(f"rule {rule.get('id')}: alert me if {describe_rule(rule)}")
    return "\n".join(lines)


def cancel_rule_response(
    rule_id: str, store: SnapshotStore, journal: Journal, now: datetime
) -> str:
    clean = str(rule_id or "").strip()
    if store.cancel_alert_rule(clean):
        journal.append(now, "alert_rule_cancelled", {"rule_id": clean})
        return f"cancelled rule {clean} -- next: chat list alert rules to confirm"
    return f"no rule {clean or '?'} saved -- next: chat list alert rules to see ids"


def _crossed(before: float, after: float, level: float, direction: str) -> bool:
    if direction == "above":
        return before <= level < after
    if direction == "below":
        return before >= level > after
    return (before - level) * (after - level) < 0


def _break_alert(rule: dict[str, Any], store: SnapshotStore) -> str | None:
    symbol = str(rule.get("symbol", "")).upper()
    level = rule.get("level")
    if level is None:
        return None
    bars = store.read_bars(symbol)
    if len(bars) < 2:
        return None
    before, after = bars[-2].close, bars[-1].close
    if not _crossed(before, after, float(level), str(rule.get("direction", "either"))):
        return None
    verb = "broke above" if after > float(level) else "broke below"
    return (
        f"ALERT: {symbol} {verb} {float(level):g} "
        f"(close {after:g}, rule {rule.get('id')})"
    )


def _flip_alert(rule: dict[str, Any], store: SnapshotStore) -> str | None:
    symbol = str(rule.get("symbol", "")).upper()
    found = store.read_latest(symbol)
    if found is None:
        return None
    snapshot_id, snapshot = found
    bars = store.read_bars(symbol)
    if len(bars) < 2:
        return None
    profile = compute_gex(snapshot, snapshot_id, spot=bars[-1].close)
    if profile.flip_point is None:
        return None
    before, after = bars[-2].close, bars[-1].close
    if not _crossed(before, after, profile.flip_point, "either"):
        return None
    side = "above" if after >= profile.flip_point else "below"
    return (
        f"ALERT: {symbol} crossed its flip {profile.flip_point:g} "
        f"(close {after:g} {side}, rule {rule.get('id')})"
    )


def evaluate_alert_rules(
    store: SnapshotStore, journal: Journal, now: datetime, underlying: str | None = None
) -> list[str]:
    rules = store.list_alert_rules(underlying) if underlying else store.list_alert_rules()
    fired = []
    for rule in rules:
        if rule.get("kind") == "flip_cross":
            text = _flip_alert(rule, store)
        else:
            text = _break_alert(rule, store)
        if text is None:
            continue
        journal.append(now, "alert", {"underlying": rule.get("symbol"), "text": text,
                                     "rule_id": rule.get("id")})
        fired.append(text)
    return fired
