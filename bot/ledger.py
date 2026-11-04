"""Reservation ledger: cross-process quota and budget holds without locks.

Lock-free protocol with one file per fact inside a ledger directory:

- Quota claims: ``quota-<feed>-<underlying>-<YYYYMMDD>.json`` created with O_EXCL.
  Presence consumes the daily slot. Release deletes the file on failure.
- Budget holds: ``rsrv-<uuid>.json`` with day, amount, timestamp, pid, and role.
- Settled spend: ``spent-<day>-<id>.json`` created with O_EXCL, so settling the
  same receipt twice is a no-op.

Budget decisions serialize on an advisory lock file so the read of settled
spend plus open holds and the write of a new hold are one atomic step. Quota
claims need no lock: O_EXCL creation is already atomic. Stale holds expire by
TTL, which bounds crash leaks, and the kernel releases the lock if a holder
dies mid-decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterator, Optional

if sys.platform == "win32":  # pragma: no cover - Windows only
    import msvcrt

    fcntl = None  # type: ignore[assignment]
else:
    import fcntl

    msvcrt = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from bot.journal import Journal

RESERVATION_TTL_SECONDS = 600.0
_MONEY_EPSILON = 1e-9


@contextmanager
def _locked(ledger_dir: str) -> Iterator[None]:
    _ensure_dir(ledger_dir)
    if fcntl is None and msvcrt is None:
        raise RuntimeError("reservation ledger needs fcntl or msvcrt file locking")
    with open(os.path.join(ledger_dir, ".lock"), "a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@dataclass(frozen=True)
class BudgetHold:
    token: str
    day: str
    amount: float


def _ensure_dir(ledger_dir: str) -> None:
    os.makedirs(ledger_dir, exist_ok=True)


def _read_record(path: str) -> Optional[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _create_exclusive(path: str, payload: dict[str, Any]) -> bool:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        return False
    return True


def _write_new(path: str, payload: dict[str, Any]) -> None:
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp_path, path)


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _day_files(ledger_dir: str, day: str) -> tuple[float, list[tuple[str, float, float, int]]]:
    """Settled total plus fresh holds as (name, amount, ts, pid) for one day."""
    try:
        names = os.listdir(ledger_dir)
    except OSError:
        return 0.0, []
    stamp = day.replace("-", "")
    settled = 0.0
    holds: list[tuple[str, float, float, int]] = []
    fresh_edge = time.time() - RESERVATION_TTL_SECONDS
    for name in names:
        path = os.path.join(ledger_dir, name)
        if name.startswith("spent-"):
            record = _read_record(path)
            if record is None:
                _unlink_quietly(path)
                continue
            if str(record.get("day", "")) != day:
                _unlink_quietly(path)
                continue
            try:
                settled += float(record.get("amount", 0.0))
            except (TypeError, ValueError):
                continue
        elif name.startswith("rsrv-"):
            record = _read_record(path)
            if record is None:
                _unlink_quietly(path)
                continue
            if str(record.get("day", "")) != day:
                _unlink_quietly(path)
                continue
            try:
                ts = float(record.get("ts", 0.0))
                amount = float(record.get("amount", 0.0))
                pid = int(record.get("pid", 0))
            except (TypeError, ValueError):
                _unlink_quietly(path)
                continue
            if ts < fresh_edge:
                _unlink_quietly(path)
                continue
            holds.append((name, amount, ts, pid))
        elif name.startswith("quota-") and not name.endswith(f"-{stamp}.json"):
            _unlink_quietly(path)
    return settled, holds


def ledger_spent(ledger_dir: str, day: str) -> float:
    settled, holds = _day_files(ledger_dir, day)
    return settled + sum(amount for _, amount, _, _ in holds)


def reserve_quota(ledger_dir: str, feed: str, underlying: str, day: str) -> bool:
    _ensure_dir(ledger_dir)
    name = f"quota-{feed}-{underlying.upper()}-{day.replace('-', '')}.json"
    return _create_exclusive(os.path.join(ledger_dir, name), {"ts": time.time()})


def release_quota(ledger_dir: str, feed: str, underlying: str, day: str) -> None:
    name = f"quota-{feed}-{underlying.upper()}-{day.replace('-', '')}.json"
    _unlink_quietly(os.path.join(ledger_dir, name))


def quota_claimed(ledger_dir: str, feed: str, underlying: str, day: str) -> bool:
    name = f"quota-{feed}-{underlying.upper()}-{day.replace('-', '')}.json"
    return os.path.exists(os.path.join(ledger_dir, name))


def reserve_budget(
    ledger_dir: str,
    day: str,
    amount: float,
    budget: float,
    role: str = "",
    now: Optional[datetime] = None,
) -> Optional[BudgetHold]:
    if amount <= 0 or budget <= 0:
        return None
    with _locked(ledger_dir):
        settled, holds = _day_files(ledger_dir, day)
        committed = settled + sum(hold_amount for _, hold_amount, _, _ in holds)
        if committed + amount > budget + _MONEY_EPSILON:
            return None
        token = uuid.uuid4().hex
        ts = now.timestamp() if now is not None else time.time()
        _write_new(
            os.path.join(ledger_dir, f"rsrv-{token}.json"),
            {"day": day, "amount": amount, "ts": ts, "pid": os.getpid(), "role": role},
        )
        return BudgetHold(token=token, day=day, amount=amount)


def release_budget(ledger_dir: str, token: str) -> None:
    _unlink_quietly(os.path.join(ledger_dir, f"rsrv-{token}.json"))


def receipt_settle_id(prompt_hash: str, stamp: datetime, cost_usd: float) -> str:
    raw = f"{prompt_hash}|{stamp.isoformat()}|{cost_usd}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def settle_spend(
    ledger_dir: str, day: str, amount: float, settle_id: str, pid: Optional[int] = None
) -> bool:
    with _locked(ledger_dir):
        settled = _create_exclusive(
            os.path.join(ledger_dir, f"spent-{day}-{settle_id}.json"),
            {"day": day, "amount": amount, "ts": time.time()},
        )
        if settled and pid is not None:
            _, holds = _day_files(ledger_dir, day)
            own = sorted(name for name, _, _, hold_pid in holds if hold_pid == pid)
            if own:
                _unlink_quietly(os.path.join(ledger_dir, own[0]))
        return settled


def settle_journal_receipts(ledger_dir: str, journal: Journal, day: str) -> int:
    count = 0
    pid = os.getpid()
    for entry in journal.entries():
        if entry.kind != "model_receipt" or entry.timestamp_utc.date().isoformat() != day:
            continue
        if entry.payload.get("ledger_settled"):
            continue
        try:
            cost = float(entry.payload.get("cost_usd", 0.0))
        except (TypeError, ValueError):
            cost = 0.0
        prompt_hash = str(entry.payload.get("prompt_hash", ""))
        settle_id = receipt_settle_id(prompt_hash, entry.timestamp_utc, cost)
        settle_spend(ledger_dir, day, cost, settle_id, pid)
        entry.payload["ledger_settled"] = True
        count += 1
    return count
