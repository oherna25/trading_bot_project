from datetime import datetime, timezone

import pytest

from bot.journal import Journal
from bot.research import advisory_review
from bot.shapes import PaperEvidence

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
FULL_ENV = {"META_MODEL_API_KEY": "test-key", "MODEL_DAY_BUDGET_USD": "5"}
EVIDENCE = PaperEvidence(clean_days=8, clean_days_target=10, max_drawdown=0.02,
                         drawdown_bound=0.05, halt_drills_passed=True)


def _strict_post(bodies):
    log = []

    def post(url, headers, payload, timeout):
        log.append(payload)
        if len(log) > len(bodies):
            raise AssertionError("unexpected extra http call")
        item = bodies[len(log) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    post.log = log
    return post


def _review_body(content):
    return {
        "choices": [{"message": {"content": content, "tool_calls": []}}],
        "usage": {"prompt_tokens": 400, "completion_tokens": 100},
    }


def _journal_with_signals():
    journal = Journal()
    journal.append(NOW, "signal", {"snapshot_id": "s1", "direction": "short", "thesis": "t1"})
    journal.append(NOW, "signal", {"snapshot_id": "s2", "direction": "long", "thesis": "t2"})
    return journal


def test_review_returns_note_and_journals_receipt():
    http_post = _strict_post([_review_body("Concern one. Concern two. Concern three. VERDICT: not-ready")])
    journal = _journal_with_signals()
    note = advisory_review(EVIDENCE, journal, NOW, live_models=True, env=FULL_ENV, http_post=http_post)
    assert "VERDICT: not-ready" in note
    assert http_post.log[0]["reasoning_effort"] == "high"
    assert http_post.log[0]["model"] == "muse-wonder-1.0"
    assert '"clean_days": 8' in http_post.log[0]["messages"][1]["content"]
    receipts = [e for e in journal.entries() if e.kind == "model_receipt"]
    assert len(receipts) == 1
    assert receipts[0].payload["model_id"] == "muse-wonder-1.0"
    assert receipts[0].payload["cost_usd"] == pytest.approx(400 / 1e6 * 4.00 + 100 / 1e6 * 20.00)
    stored = next(e for e in journal.entries() if e.kind == "review_note")
    assert stored.payload["note"] == note


def test_review_defaults_to_skipped_offline():
    def explode(*args, **kwargs):
        raise AssertionError("offline path must not call http")

    journal = Journal()
    assert advisory_review(EVIDENCE, journal, NOW, env=FULL_ENV, http_post=explode) == ""
    assert journal.entries()[-1].payload["reason"] == "live models off"


def test_review_missing_key_skips_without_network():
    def explode(*args, **kwargs):
        raise AssertionError("no key means no http")

    journal = Journal()
    assert advisory_review(EVIDENCE, journal, NOW, live_models=True, env={}, http_post=explode) == ""
    assert journal.entries()[-1].payload["reason"] == "missing META_MODEL_API_KEY"


def test_review_http_error_skips():
    journal = Journal()
    assert advisory_review(EVIDENCE, journal, NOW, live_models=True, env=FULL_ENV,
                            http_post=_strict_post([RuntimeError("down")])) == ""
    assert journal.entries()[-1].payload["reason"] == "RuntimeError"


def test_review_empty_note_skips():
    journal = Journal()
    assert advisory_review(EVIDENCE, journal, NOW, live_models=True, env=FULL_ENV,
                            http_post=_strict_post([_review_body("   ")])) == ""
    assert journal.entries()[-1].payload["reason"] == "empty review"
