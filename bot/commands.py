from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Mapping

from bot.journal import Journal
from bot.models import (
    MODEL_REGISTRY,
    HttpPost,
    model_api_key,
    receipt_payload,
    run_analyst,
)
from bot.risk import review_intent, submit_paper
from bot.shapes import OrderIntent, OrderLeg, RiskLimits, RiskState
from bot.store import SnapshotStore
from bot.tiers import select_model_tier

COMMAND_STATE = RiskState(
    account_size=100000.0, risk_per_trade=0.01, day_pnl=0.0, halt_active=False, kill_active=False
)
COMMAND_LIMITS = RiskLimits(max_trade_size=5000.0, day_profit_target=500.0, max_day_loss=1000.0)

PORTFOLIO_USAGE = (
    'portfolio commands: "close my losers past 20%", "close my winners", '
    '"close SPY", "roll SPY put 760 to next friday", "flatten everything confirm". '
    "Paper venue only."
)

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

LOSERS_RE = re.compile(
    r"\b(close|sell)\s+(?:my\s+)?losers?(?:\s+(?:past|under|over|beyond|down|at)\s+(-?\d+(?:\.\d+)?)\s*%?)?",
    re.IGNORECASE,
)
WINNERS_RE = re.compile(
    r"\b(close|sell)\s+(?:my\s+)?(?:winners?|gainers?)(?:\s+(?:past|under|over|beyond|at)\s+(-?\d+(?:\.\d+)?)\s*%?)?",
    re.IGNORECASE,
)
SYMBOL_RE = re.compile(
    r"\b(?:close|sell)\s+(?:my\s+)?(?!(?:losers?|winners?|gainers?|everything|all|positions?)\b)"
    r"([A-Za-z]{1,6})(?:\s+(?:position|stock|shares?))?\b",
    re.IGNORECASE,
)
FLATTEN_RE = re.compile(r"\bflatten\b|\bclose\s+everything\b|\bclose\s+all\b", re.IGNORECASE)
ROLL_RE = re.compile(
    r"\broll\s+(?:my\s+)?(?:(?!(?:call|put|calls|puts|my)\b)([A-Za-z]{1,6})\s+)?"
    r"(call|put)?s?\s*(\d+(?:\.\d+)?)?\s*(call|put)?s?\s+to\s+(.+)",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
NEXT_WEEKDAY_RE = re.compile(
    r"next\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", re.IGNORECASE
)

PORTFOLIO_SYSTEM = (
    "Translate the operator request into one JSON portfolio action and nothing else. "
    "Actions: {\"action\": \"close_losers\", \"threshold_pct\": number}, "
    "{\"action\": \"close_winners\", \"threshold_pct\": number}, "
    "{\"action\": \"close_symbol\", \"symbol\": \"SYM\"}, "
    "{\"action\": \"flatten\", \"confirm\": true|false}, "
    "{\"action\": \"roll\", \"symbol\": \"SYM\"|null, \"kind\": \"call\"|\"put\"|null, "
    "\"strike\": number|null, \"to_expiry\": \"YYYY-MM-DD\"}. "
    "You may call get_positions once to see the open book. Reply with JSON only."
)

POSITIONS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_positions",
        "description": "Read open paper positions with marks",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _next_weekday(now: datetime, name: str) -> str:
    days = (WEEKDAYS[name.lower()] - now.weekday()) % 7 or 7
    return (now + timedelta(days=days)).date().isoformat()


def _roll_target(raw: str, now: datetime) -> str | None:
    match = DATE_RE.search(raw)
    if match is not None:
        try:
            datetime.fromisoformat(match.group(1)).date()
        except ValueError:
            return None
        return match.group(1)
    match = NEXT_WEEKDAY_RE.search(raw)
    if match is not None:
        return _next_weekday(now, match.group(1))
    return None


def parse_portfolio_command(text: str, now: datetime) -> dict[str, Any]:
    lowered = text.lower()
    if FLATTEN_RE.search(text) is not None:
        return {"action": "flatten", "confirm": "confirm" in lowered}
    match = LOSERS_RE.search(text)
    if match is not None:
        threshold = abs(float(match.group(2))) if match.group(2) else 0.0
        return {"action": "close_losers", "threshold_pct": threshold}
    match = WINNERS_RE.search(text)
    if match is not None:
        threshold = abs(float(match.group(2))) if match.group(2) else 0.0
        return {"action": "close_winners", "threshold_pct": threshold}
    match = ROLL_RE.search(text)
    if match is not None:
        symbol, kind_a, strike, kind_b, target = match.groups()
        kind = (kind_a or kind_b or "").lower() or None
        return {
            "action": "roll",
            "symbol": symbol.upper() if symbol else None,
            "kind": kind,
            "strike": float(strike) if strike else None,
            "to_expiry": _roll_target(target, now),
        }
    match = SYMBOL_RE.search(text)
    if match is not None:
        return {"action": "close_symbol", "symbol": match.group(1).upper()}
    return {"action": "unknown", "text": text.strip()}


def open_book(journal: Journal) -> list[dict[str, Any]]:
    books: dict[tuple, dict[str, Any]] = {}
    for entry in journal.entries():
        if entry.kind != "fill" or entry.payload.get("venue", "paper") == "replay":
            continue
        symbol = entry.payload.get("symbol")
        price = entry.payload.get("price")
        size = entry.payload.get("size")
        if not symbol or price is None or size is None:
            continue
        side = entry.payload.get("side", "buy")
        legs = entry.payload.get("legs") or []
        if legs:
            for leg in legs:
                if not isinstance(leg, dict):
                    continue
                key = (
                    str(leg.get("symbol") or symbol),
                    str(leg.get("call_or_put") or ""),
                    leg.get("strike"),
                    str(leg.get("expiry") or ""),
                )
                leg_size = leg.get("size") or 0.0
                signed = float(leg_size) if leg.get("side", side) == "buy" else -float(leg_size)
                slot = books.setdefault(key, _new_slot(key))
                slot["net"] += signed
                slot["cost"] += signed * float(price)
        else:
            key = (str(symbol), "", None, "")
            signed = float(size) if side == "buy" else -float(size)
            slot = books.setdefault(key, _new_slot(key))
            slot["net"] += signed
            slot["cost"] += signed * float(price)
    entries = []
    for slot in books.values():
        if slot["net"] == 0:
            continue
        slot["avg"] = slot["cost"] / slot["net"]
        entries.append(slot)
    return sorted(entries, key=lambda slot: (slot["symbol"], str(slot["strike"])))


def _new_slot(key: tuple) -> dict[str, Any]:
    symbol, kind, strike, expiry = key
    return {
        "symbol": symbol,
        "kind": kind or "stock",
        "strike": strike,
        "expiry": expiry,
        "net": 0.0,
        "cost": 0.0,
        "avg": 0.0,
    }


def _describe(entry: Mapping[str, Any]) -> str:
    if entry["kind"] == "stock":
        return f"{entry['symbol']} stock x{abs(entry['net']):g}"
    return f"{entry['symbol']} {entry['kind']} {entry['strike']} {entry['expiry']}".strip()


def _mark(entry: Mapping[str, Any], store: SnapshotStore) -> float | None:
    if entry["kind"] != "stock":
        return None
    bar = store.read_latest_bar(str(entry["symbol"]))
    return bar.close if bar is not None else None


def _close_entry(
    entry: Mapping[str, Any],
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    signal_id: str,
) -> str:
    label = _describe(entry)
    side = "sell" if float(entry["net"]) > 0 else "buy"
    size = abs(float(entry["net"]))
    legs: tuple[OrderLeg, ...] = ()
    if entry["kind"] == "stock":
        mark = _mark(entry, store)
        if mark is None:
            return f"{label}: skipped (no stored bar for mark)"
    else:
        if not entry.get("expiry"):
            return f"{label}: skipped (option leg missing expiry)"
        legs = (
            OrderLeg(
                symbol=str(entry["symbol"]), side=side, size=size,
                strike=entry["strike"], expiry=str(entry["expiry"]),
                call_or_put=str(entry["kind"]),
            ),
        )
        mark = float(entry["avg"])
    intent = OrderIntent(
        symbol=str(entry["symbol"]), side=side, size=size,
        risk_numbers={"close": 1.0, "size": size}, signal_id=signal_id, legs=legs,
    )
    journal.append(now, "intent_proposed", {"signal_id": signal_id, "size": size, "close": True})
    verdict = review_intent(intent, COMMAND_STATE, COMMAND_LIMITS, journal, now,
                            profile=store.read_profile())
    if verdict != "approved":
        reason = journal.entries()[-1].payload.get("reason", "vetoed")
        return f"{label}: rejected ({reason})"
    fill, _, _ = submit_paper(intent, mark, COMMAND_STATE, None, COMMAND_LIMITS, journal, now)
    note = " at cost, no live option quote" if entry["kind"] != "stock" else ""
    return f"{label}: closed {size:g} @ {fill.price:.2f}{note}"


def _pnl_pct(entry: Mapping[str, Any], mark: float) -> float:
    cost = abs(float(entry["avg"]) * float(entry["net"]))
    if cost == 0:
        return 0.0
    return (mark - float(entry["avg"])) * float(entry["net"]) / cost * 100.0


def _close_losers(action: Mapping[str, Any], store: SnapshotStore,
                 journal: Journal, now: datetime) -> list[str]:
    threshold = float(action.get("threshold_pct", 0.0))
    lines = []
    for entry in open_book(journal):
        mark = _mark(entry, store)
        if mark is None:
            continue
        pnl = (mark - float(entry["avg"])) * float(entry["net"])
        if pnl < 0 and abs(_pnl_pct(entry, mark)) >= threshold:
            lines.append(_close_entry(entry, store, journal, now, "portfolio-close-losers"))
    if not lines:
        label = f" past {threshold:g}%" if threshold else ""
        return [f"no losers{label} to close"]
    return [f"closed {len(lines)} loser{'s' if len(lines) != 1 else ''}:"] + lines


def _close_winners(action: Mapping[str, Any], store: SnapshotStore,
                  journal: Journal, now: datetime) -> list[str]:
    threshold = float(action.get("threshold_pct", 0.0))
    lines = []
    for entry in open_book(journal):
        mark = _mark(entry, store)
        if mark is None:
            continue
        pnl = (mark - float(entry["avg"])) * float(entry["net"])
        if pnl > 0 and _pnl_pct(entry, mark) >= threshold:
            lines.append(_close_entry(entry, store, journal, now, "portfolio-close-winners"))
    if not lines:
        label = f" past {threshold:g}%" if threshold else ""
        return [f"no winners{label} to close"]
    return [f"closed {len(lines)} winner{'s' if len(lines) != 1 else ''}:"] + lines


def _close_symbol(action: Mapping[str, Any], store: SnapshotStore,
                 journal: Journal, now: datetime) -> list[str]:
    symbol = str(action.get("symbol", "")).upper()
    entries = [entry for entry in open_book(journal) if entry["symbol"] == symbol]
    if not entries:
        return [f"no open {symbol} position to close"]
    return [
        _close_entry(entry, store, journal, now, f"portfolio-close-{symbol}")
        for entry in entries
    ]


def _flatten(action: Mapping[str, Any], store: SnapshotStore,
            journal: Journal, now: datetime) -> list[str]:
    entries = open_book(journal)
    if not entries:
        return ["no open positions to flatten"]
    if not action.get("confirm"):
        journal.append(now, "command_armed", {"action": "flatten", "count": len(entries)})
        return [f"say 'flatten everything confirm' to close {len(entries)} open positions"]
    lines = [
        _close_entry(entry, store, journal, now, "portfolio-flatten") for entry in entries
    ]
    closed = sum(1 for line in lines if ": closed" in line)
    return [f"closed {closed} positions:"] + lines


def _roll(action: Mapping[str, Any], store: SnapshotStore,
         journal: Journal, now: datetime) -> list[str]:
    to_expiry = action.get("to_expiry")
    if not to_expiry:
        return ["roll needs a target expiry like 2026-12-18 or next friday"]
    want_symbol = action.get("symbol")
    want_kind = (action.get("kind") or "").lower() or None
    want_strike = action.get("strike")
    candidates = [
        entry for entry in open_book(journal)
        if entry["kind"] != "stock"
        and (want_symbol is None or entry["symbol"] == want_symbol)
        and (want_kind is None or entry["kind"] == want_kind)
        and (want_strike is None or entry["strike"] == want_strike)
    ]
    if not candidates:
        bits = " ".join(
            str(part) for part in (want_symbol, want_kind, want_strike) if part is not None
        )
        return [f"no open {bits or 'option'} position to roll".strip()]
    entry = candidates[0]
    closed = _close_entry(entry, store, journal, now, "portfolio-roll-close")
    if ": closed" not in closed:
        return [closed]
    side = "buy" if float(entry["net"]) > 0 else "sell"
    size = abs(float(entry["net"]))
    intent = OrderIntent(
        symbol=str(entry["symbol"]), side=side, size=size,
        risk_numbers={"roll": 1.0, "size": size}, signal_id="portfolio-roll-open",
        legs=(
            OrderLeg(
                symbol=str(entry["symbol"]), side=side, size=size,
                strike=entry["strike"], expiry=str(to_expiry), call_or_put=str(entry["kind"]),
            ),
        ),
    )
    journal.append(now, "intent_proposed", {"signal_id": "portfolio-roll-open", "size": size})
    verdict = review_intent(intent, COMMAND_STATE, COMMAND_LIMITS, journal, now,
                            profile=store.read_profile())
    if verdict != "approved":
        reason = journal.entries()[-1].payload.get("reason", "vetoed")
        return [closed, f"reopen rejected ({reason})"]
    submit_paper(intent, float(entry["avg"]), COMMAND_STATE, None, COMMAND_LIMITS, journal, now)
    label = _describe(entry)
    return [closed, f"rolled {label} to {to_expiry} at cost"]


def execute_portfolio_action(
    action: Mapping[str, Any],
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
) -> str:
    del underlying
    name = action.get("action")
    if name == "close_losers":
        lines = _close_losers(action, store, journal, now)
    elif name == "close_winners":
        lines = _close_winners(action, store, journal, now)
    elif name == "close_symbol":
        lines = _close_symbol(action, store, journal, now)
    elif name == "flatten":
        lines = _flatten(action, store, journal, now)
    elif name == "roll":
        lines = _roll(action, store, journal, now)
    else:
        journal.append(now, "command_rejected", {"reason": "unknown action"})
        return PORTFOLIO_USAGE
    journal.append(now, "portfolio_command", {"action": dict(action), "lines": len(lines)})
    return "\n".join(lines)


def _validate_spark_action(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"action": "unknown"}
    name = body.get("action")
    if name in ("close_losers", "close_winners"):
        try:
            threshold = abs(float(body.get("threshold_pct", 0.0)))
        except (TypeError, ValueError):
            threshold = 0.0
        return {"action": name, "threshold_pct": threshold}
    if name == "close_symbol":
        symbol = body.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            return {"action": "unknown"}
        return {"action": name, "symbol": symbol.strip().upper()}
    if name == "flatten":
        return {"action": name, "confirm": bool(body.get("confirm", False))}
    if name == "roll":
        to_expiry = body.get("to_expiry")
        if not isinstance(to_expiry, str) or not DATE_RE.search(to_expiry):
            return {"action": "unknown"}
        strike = body.get("strike")
        try:
            strike_value = float(strike) if strike is not None else None
        except (TypeError, ValueError):
            return {"action": "unknown"}
        kind = body.get("kind")
        symbol = body.get("symbol")
        return {
            "action": name,
            "symbol": symbol.strip().upper() if isinstance(symbol, str) and symbol.strip() else None,
            "kind": kind.lower() if kind in ("call", "put") else None,
            "strike": strike_value,
            "to_expiry": DATE_RE.search(to_expiry).group(1),  # type: ignore[union-attr]
        }
    return {"action": "unknown"}


def spark_translate(
    text: str,
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> dict[str, Any]:
    decision = select_model_tier("portfolio", journal, now, env)
    if decision.tier != "model":
        return {"action": "unknown"}
    book_json = json.dumps(open_book(journal), default=str)
    prompt = f"Request: {text}\nOpen book: {book_json}"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": PORTFOLIO_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    spec = MODEL_REGISTRY[decision.role]
    try:
        reply, receipts = run_analyst(
            spec,
            model_api_key(env),
            messages,
            [POSITIONS_TOOL],
            {"get_positions": lambda args: book_json},
            prompt,
            http_post,
        )
    except Exception:
        return {"action": "unknown"}
    for receipt in receipts:
        journal.append(now, "model_receipt", receipt_payload(receipt, ""))
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        return {"action": "unknown"}
    try:
        body = json.loads(reply[start : end + 1])
    except ValueError:
        return {"action": "unknown"}
    return _validate_spark_action(body)


def portfolio_response(
    text: str,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> str:
    parsed = parse_portfolio_command(text, now)
    if parsed["action"] == "unknown" and live_models:
        parsed = spark_translate(text, journal, now, env, http_post)
    if parsed["action"] == "unknown":
        journal.append(now, "command_rejected", {"reason": "unrecognized command"})
        return PORTFOLIO_USAGE
    result = execute_portfolio_action(parsed, underlying, store, journal, now)
    if result == PORTFOLIO_USAGE:
        return result
    return "paper venue only:\n" + result
