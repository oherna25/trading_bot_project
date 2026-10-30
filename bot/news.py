from __future__ import annotations

from datetime import datetime
from typing import Mapping

from bot.journal import Journal
from bot.models import HttpPost
from bot.research import grounded_note
from bot.shock import shock_text
from bot.store import SnapshotStore


def shock_news_response(
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    shock_pct: float,
    live_models: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> str:
    facts = shock_text(underlying, store, journal, now, shock_pct)
    found = store.read_latest(underlying)
    if found is None or store.read_latest_bar(underlying) is None:
        return facts
    snapshot_id, _ = found
    if not live_models:
        journal.append(now, "shock_news_skipped", {"reason": "live models off"})
        return facts + "\nnews: enable live models for the cited explanation"
    question = (
        f"What current market news explains a {shock_pct:+.1f}% move in {underlying}? "
        f"One cited sentence. Shock facts: {' | '.join(facts.splitlines()[:3])}"
    )
    note = grounded_note(question, journal, now, snapshot_id, env, http_post)
    if not note:
        journal.append(now, "shock_news_skipped", {"reason": "no grounded note"})
        return facts + "\nnews: no fresh driver found"
    journal.append(now, "shock_news", {"underlying": underlying, "shock_pct": shock_pct, "note": note})
    return facts + f"\nlikely driver: {note}"
