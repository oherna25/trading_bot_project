from datetime import datetime, timezone

from bot.coach import coach_review, fallback_coach, journal_stats
from bot.journal import Journal
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
ENV = {"META_MODEL_API_KEY": "key-1", "MODEL_DAY_BUDGET_USD": "5.0"}


def _storied_journal() -> Journal:
    journal = Journal()
    journal.append(NOW, "fill", {"symbol": "TEST", "side": "buy", "price": 100.0, "size": 10})
    journal.append(NOW, "fill", {"symbol": "TEST", "side": "sell", "price": 101.0, "size": 10})
    journal.append(NOW, "intent_vetoed", {"reason": "outside trade targets"})
    journal.append(NOW, "intent_approved", {"signal_id": "s-1", "size": 100})
    journal.append(NOW, "halt", {"day_pnl": -1200.0})
    journal.append(NOW, "signal", {"direction": "long", "thesis": "t"})
    journal.append(NOW, "signal", {"direction": "short", "thesis": "t"})
    return journal


def _wonder_stub(seen, note="Keep: journaling. Fix: size down."):
    def post(url, headers, payload, timeout):
        seen.append(payload)
        assert payload["model"] == "muse-wonder-1.0"
        return {
            "choices": [{"message": {"content": note}}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 40},
        }

    return post


def test_journal_stats_counts_kinds():
    stats = journal_stats(_storied_journal())
    assert stats["fills"] == 2
    assert stats["symbols"] == ["TEST"]
    assert stats["halts"] == 1 and stats["kills"] == 0
    assert stats["vetoed"] == 1 and stats["approved"] == 1
    assert stats["signals"] == {"long": 1, "short": 1, "flat": 0}


def test_fallback_coach_cites_facts():
    text = fallback_coach(journal_stats(_storied_journal()))
    assert "[rules]" in text
    assert "2 fills" in text
    assert "outside trade targets" in text
    assert "halt" in text.lower()


def test_fallback_coach_empty_journal():
    text = fallback_coach(journal_stats(Journal()))
    assert "no fills yet" in text


def test_coach_offline_returns_rules():
    journal = _storied_journal()
    text = coach_review(SnapshotStore(), journal, NOW)
    assert "[rules]" in text
    assert journal.entries()[-1].kind == "coach_skipped"


def test_coach_live_returns_wonder_note():
    seen = []
    journal = _storied_journal()
    text = coach_review(SnapshotStore(), journal, NOW, live_models=True,
                        env=dict(ENV), http_post=_wonder_stub(seen))
    assert "size down" in text
    assert "muse-wonder-1.0" in text
    assert "fills" in seen[0]["messages"][1]["content"]
    kinds = [entry.kind for entry in journal.entries()]
    assert "coach_note" in kinds and "model_receipt" in kinds


def test_coach_live_error_falls_back_to_rules():
    def down(*args, **kwargs):
        raise RuntimeError("wonder down")

    journal = _storied_journal()
    text = coach_review(SnapshotStore(), journal, NOW, live_models=True,
                        env=dict(ENV), http_post=down)
    assert "[rules]" in text
    assert journal.entries()[-1].kind == "coach_skipped"


def test_coach_uses_local_tier(tmp_path):
    weights = tmp_path / "glimmer.gguf"
    weights.write_bytes(b"gguf-stub")
    journal = _storied_journal()
    text = coach_review(
        SnapshotStore(), journal, NOW, live_models=True,
        env={"GLIMMER_GGUF_PATH": str(weights)},
        local_runner=lambda *a: {"text": "Keep: calm. Fix: halts."},
    )
    assert "Keep: calm" in text
    assert "muse-glimmer-3b" in text
