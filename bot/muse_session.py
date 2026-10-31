"""Session trigger path, schedule parsing, and controller."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from bot.feeds import FeedContext, live_context
from bot.journal import Journal
from bot.persist import save_bundle
from bot.schedule import normalize_underlyings, run_session_core, tick_symbols
from bot.store import SnapshotStore
from bot.video import video_response

SESSION_PATH = "/session"
SESSION_TICK_SECONDS = 30.0


@dataclass(frozen=True)
class SessionSchedule:
    """Typed parse of cron subset "M H * * DOW" in machine-local time.

    Weekdays are Python weekdays (Monday 0 through Sunday 6).
    """

    minute: int
    hour: int
    weekdays: frozenset[int]

    def matches(self, now: datetime) -> bool:
        return now.minute == self.minute and now.hour == self.hour and now.weekday() in self.weekdays


def _parse_dow_value(raw: str, text: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"invalid schedule {text!r}: bad weekday {raw!r}") from None
    if value == 7:
        value = 0
    if not 0 <= value <= 6:
        raise ValueError(f"invalid schedule {text!r}: weekday {raw!r} out of range 0-7")
    return value


def _parse_dow_field(field: str, text: str) -> frozenset[int]:
    if field == "*":
        return frozenset(range(7))
    days: set[int] = set()
    for part in field.split(","):
        if not part.strip():
            raise ValueError(f"invalid schedule {text!r}: empty weekday entry")
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2:
                raise ValueError(f"invalid schedule {text!r}: bad weekday range {part!r}")
            start = _parse_dow_value(bounds[0], text)
            end = _parse_dow_value(bounds[1], text)
            if end < start:
                raise ValueError(f"invalid schedule {text!r}: reversed weekday range {part!r}")
            days.update(range(start, end + 1))
        else:
            days.add(_parse_dow_value(part, text))
    return frozenset((day + 6) % 7 for day in days)


def parse_schedule(text: str) -> SessionSchedule:
    """Parse cron subset "M H * * DOW". Raises ValueError when malformed."""
    fields = (text or "").split()
    if len(fields) != 5:
        raise ValueError(f"invalid schedule {text!r}: want 'M H * * DOW'")
    minute_text, hour_text, day_of_month, month, dow = fields
    try:
        minute = int(minute_text)
        hour = int(hour_text)
    except ValueError:
        raise ValueError(f"invalid schedule {text!r}: minute and hour must be numbers") from None
    if not 0 <= minute <= 59:
        raise ValueError(f"invalid schedule {text!r}: minute out of range 0-59")
    if not 0 <= hour <= 23:
        raise ValueError(f"invalid schedule {text!r}: hour out of range 0-23")
    if day_of_month != "*" or month != "*":
        raise ValueError(f"invalid schedule {text!r}: day-of-month and month must be '*'")
    return SessionSchedule(minute, hour, _parse_dow_field(dow, text))


MinuteKey = tuple[int, int, int, int, int]


def session_due(schedule: SessionSchedule, now: datetime, last_fired: MinuteKey | None) -> bool:
    key = (now.year, now.month, now.day, now.hour, now.minute)
    return schedule.matches(now) and key != last_fired


@contextmanager
def _scratch_run(
    lock: threading.RLock, store: SnapshotStore, journal: Journal
) -> Iterator[tuple[SnapshotStore, Journal]]:
    """Yield a scratch copy of live state, merging it back on clean exit.

    The core runs with no lock held. A raised core skips the commit.
    Pruning and saving stay with the caller.
    """
    scratch_store = SnapshotStore()
    scratch_journal = Journal()
    with lock:
        scratch_store.restore(store)
        scratch_journal.restore(journal)
    yield scratch_store, scratch_journal
    with lock:
        store.merge_from(scratch_store)
        journal.merge(scratch_journal)


@dataclass
class SessionController:
    """Shared session primitive for the HTTP trigger and the timer.

    run is the only entry; both drivers call it. lock is the server
    lock, reentrant because the HTTP path already holds it.
    """

    store: SnapshotStore
    journal: Journal
    store_dir: str
    lock: threading.RLock
    context_factory: Callable[[str], FeedContext] = live_context
    after_save: Optional[Callable[[], None]] = None

    def run(
        self,
        underlying: str | Sequence[str] = "SPY",
        live_models: bool = False,
        grounding: bool = False,
        cards: bool = False,
        now: datetime | None = None,
        paper_trade: bool = True,
    ) -> tuple[str, list[str]]:
        with self.lock:
            symbols = tick_symbols(normalize_underlyings(underlying), self.store)
        with _scratch_run(self.lock, self.store, self.journal) as (scratch_store, scratch_journal):
            text = run_session_core(
                symbols,
                scratch_store,
                scratch_journal,
                self.store_dir,
                now,
                None,
                None,
                self.context_factory,
                live_models,
                grounding,
                cards,
                paper_trade,
            )
        with self.lock:
            moment = now or datetime.now(timezone.utc)
            try:
                requested = underlying if isinstance(underlying, str) else ",".join(underlying)
                video_response(
                    "", requested, self.store, self.journal, moment,
                    project_dir=os.path.join(self.store_dir, "video-brief"),
                )
            except Exception as exc:
                self.journal.append(moment, "video_brief_failed", {"error": type(exc).__name__})
            save_bundle(self.store_dir, self.store, self.journal)
            if self.after_save is not None:
                self.after_save()
        return text, symbols
