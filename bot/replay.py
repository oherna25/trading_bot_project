from __future__ import annotations

from bot.exposure import compute_gex
from bot.journal import Journal
from bot.shapes import ChainSnapshot
from bot.store import snapshot_id_for


def replay_week(snapshot_list: list[ChainSnapshot], journal: Journal) -> Journal:
    ordered = sorted(snapshot_list, key=lambda s: s.timestamp_utc)
    for snapshot in ordered:
        snapshot_id = snapshot_id_for(snapshot)
        profile = compute_gex(snapshot, snapshot_id)
        journal.append(
            snapshot.timestamp_utc,
            "replay_step",
            {"snapshot_id": snapshot_id, "total_exposure": profile.total_exposure},
        )
    return journal
