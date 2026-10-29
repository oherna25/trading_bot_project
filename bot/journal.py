from __future__ import annotations

from datetime import datetime
from typing import Any

from bot.shapes import JournalEntry


class Journal:
    def __init__(self) -> None:
        self._entries: list[JournalEntry] = []

    def append(self, timestamp_utc: datetime, kind: str, payload: dict[str, Any]) -> JournalEntry:
        entry = JournalEntry(timestamp_utc=timestamp_utc, kind=kind, payload=payload)
        self._entries.append(entry)
        return entry

    def entries(self) -> list[JournalEntry]:
        return list(self._entries)

    def restore(self, other: Journal) -> None:
        self._entries = list(other._entries)

    def merge(self, other: Journal) -> int:
        added = 0
        for entry in other.entries():
            if entry not in self._entries:
                self._entries.append(entry)
                added += 1
        self._entries.sort(key=lambda e: e.timestamp_utc)
        return added
