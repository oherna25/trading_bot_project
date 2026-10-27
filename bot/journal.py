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
