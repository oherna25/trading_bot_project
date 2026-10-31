from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from bot.shapes import JournalEntry


def _entry_key(entry: JournalEntry) -> tuple:
    return (entry.timestamp_utc, entry.kind, json.dumps(entry.payload, sort_keys=True, default=str))


class Journal:
    def __init__(self) -> None:
        self._entries: list[JournalEntry] = []

    def append(self, timestamp_utc: datetime, kind: str, payload: dict[str, Any]) -> JournalEntry:
        entry = JournalEntry(timestamp_utc=timestamp_utc, kind=kind, payload=payload)
        self._entries.append(entry)
        return entry

    def entries(self) -> list[JournalEntry]:
        return list(self._entries)

    def prune(self, keep: int = 20000) -> int:
        keep = max(0, keep)
        dropped = max(0, len(self._entries) - keep)
        if dropped:
            del self._entries[:dropped]
        return dropped

    def restore(self, other: Journal) -> None:
        self._entries = list(other._entries)

    def merge(self, other: Journal) -> int:
        seen = {_entry_key(entry) for entry in self._entries}
        added = 0
        for entry in other.entries():
            key = _entry_key(entry)
            if key not in seen:
                seen.add(key)
                self._entries.append(entry)
                added += 1
        self._entries.sort(key=lambda e: e.timestamp_utc)
        return added
