import json
import os
import threading
from datetime import datetime, timezone

import pytest

from bot.journal import Journal
from bot.ledger import (
    ledger_spent,
    quota_claimed,
    release_budget,
    release_quota,
    reserve_budget,
    reserve_quota,
    settle_journal_receipts,
    settle_spend,
)

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
DAY = "2026-10-27"


def test_quota_claim_is_exclusive_until_released(tmp_path):
    ledger = str(tmp_path)
    assert reserve_quota(ledger, "alpha_vantage", "SPY", DAY) is True
    assert reserve_quota(ledger, "alpha_vantage", "SPY", DAY) is False
    assert quota_claimed(ledger, "alpha_vantage", "SPY", DAY) is True
    assert reserve_quota(ledger, "alpha_vantage", "QQQ", DAY) is True
    release_quota(ledger, "alpha_vantage", "SPY", DAY)
    assert quota_claimed(ledger, "alpha_vantage", "SPY", DAY) is False
    assert reserve_quota(ledger, "alpha_vantage", "SPY", DAY) is True


def test_quota_claim_race_allows_exactly_one_winner(tmp_path):
    ledger = str(tmp_path)
    barrier = threading.Barrier(8)
    wins: list[bool] = []

    def _racer():
        barrier.wait()
        wins.append(reserve_quota(ledger, "alpha_vantage", "SPY", DAY))

    threads = [threading.Thread(target=_racer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(wins) == 1


def test_budget_reserve_fits_and_rejects_over_budget(tmp_path):
    ledger = str(tmp_path)
    first = reserve_budget(ledger, DAY, 0.60, 1.00, "analyst")
    assert first is not None
    assert reserve_budget(ledger, DAY, 0.50, 1.00, "analyst") is None
    release_budget(ledger, first.token)
    assert reserve_budget(ledger, DAY, 0.50, 1.00, "analyst") is not None
    assert reserve_budget(ledger, DAY, 0.0, 1.00, "analyst") is None
    assert reserve_budget(ledger, DAY, 0.10, 0.0, "analyst") is None


def test_budget_hold_race_never_exceeds_budget(tmp_path):
    ledger = str(tmp_path)
    barrier = threading.Barrier(8)
    wins: list[bool] = []

    def _racer():
        barrier.wait()
        wins.append(reserve_budget(ledger, DAY, 0.30, 1.00, "analyst") is not None)

    threads = [threading.Thread(target=_racer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(wins) == 3
    assert ledger_spent(ledger, DAY) == pytest.approx(0.90)


def test_settle_spend_is_idempotent_and_drops_hold(tmp_path):
    ledger = str(tmp_path)
    hold = reserve_budget(ledger, DAY, 0.50, 1.00, "analyst")
    assert hold is not None
    assert settle_spend(ledger, DAY, 0.10, "abc123", os.getpid()) is True
    assert settle_spend(ledger, DAY, 0.10, "abc123", os.getpid()) is False
    assert ledger_spent(ledger, DAY) == pytest.approx(0.10)


def test_stale_hold_expires_and_other_days_prune(tmp_path):
    ledger = str(tmp_path)
    with open(f"{ledger}/rsrv-stale.json", "w", encoding="utf-8") as handle:
        json.dump({"day": DAY, "amount": 0.90, "ts": 1.0, "pid": 999999, "role": "analyst"}, handle)
    with open(f"{ledger}/spent-2026-10-26-deadbeef.json", "w", encoding="utf-8") as handle:
        json.dump({"day": "2026-10-26", "amount": 5.0, "ts": 1.0}, handle)
    assert reserve_budget(ledger, DAY, 0.50, 1.00, "analyst") is not None
    assert ledger_spent(ledger, DAY) == pytest.approx(0.50)


def test_settle_journal_receipts_marks_and_counts_once(tmp_path):
    from datetime import timedelta

    ledger = str(tmp_path)
    journal = Journal()
    journal.append(NOW, "model_receipt", {"prompt_hash": "h1", "cost_usd": 0.04})
    journal.append(NOW, "model_receipt", {"prompt_hash": "h2", "cost_usd": 0.02})
    journal.append(NOW - timedelta(days=1), "model_receipt", {"prompt_hash": "h0", "cost_usd": 9.0})
    assert settle_journal_receipts(ledger, journal, DAY) == 2
    assert settle_journal_receipts(ledger, journal, DAY) == 0
    assert ledger_spent(ledger, DAY) == pytest.approx(0.06)


def test_select_backend_reserves_across_journals(tmp_path):
    from bot.models import select_backend

    ledger = str(tmp_path)
    env = {"META_MODEL_API_KEY": "key-1", "MODEL_DAY_BUDGET_USD": "0.06", "LEDGER_DIR": ledger}
    first, second = Journal(), Journal()
    assert select_backend("analyst", first, NOW, env) == ("model", "")
    assert first.entries()[-1].kind == "budget_reserved"
    assert select_backend("analyst", second, NOW, env) == ("fallback", "day budget reserved")
    first.append(NOW, "model_receipt", {"prompt_hash": "h1", "cost_usd": 0.01})
    assert select_backend("analyst", first, NOW, env) == ("model", "")
    assert ledger_spent(ledger, DAY) == pytest.approx(0.06)


def test_quota_wiring_reserves_before_call_and_releases_on_failure(tmp_path, monkeypatch):
    from datetime import timedelta

    import pytest

    from bot.feeds import FeedContext, FeedUnavailableError, ingest_bars
    from bot.store import SnapshotStore

    bars_payload = {
        "underlying": "SPY",
        "bars": [
            {"timestamp": NOW - timedelta(days=1), "open": 1.0, "high": 1.0,
             "low": 1.0, "close": 1.0, "volume": 1.0},
        ],
    }
    from bot import feeds

    monkeypatch.setattr(feeds, "_now_utc", lambda: NOW)
    monkeypatch.setitem(feeds.FEED_REGISTRY, "alpha_vantage", lambda ctx: bars_payload)
    ledger = str(tmp_path)
    store, journal = SnapshotStore(), Journal()
    ctx = FeedContext(underlying="SPY", credentials={}, http_get=lambda *a, **k: {}, ledger_dir=ledger)
    assert len(ingest_bars("alpha_vantage", store, journal, ctx)) == 1
    assert journal.entries()[0].kind == "quota_reserved"
    with pytest.raises(FeedUnavailableError, match="already reserved"):
        ingest_bars("alpha_vantage", store, Journal(), ctx)

    def _boom(ctx):
        raise FeedUnavailableError("vendor down")

    monkeypatch.setitem(feeds.FEED_REGISTRY, "alpha_vantage", _boom)
    failing = FeedContext(underlying="QQQ", credentials={}, http_get=lambda *a, **k: {},
                           ledger_dir=ledger)
    with pytest.raises(FeedUnavailableError, match="vendor down"):
        ingest_bars("alpha_vantage", store, Journal(), failing)
    assert quota_claimed(ledger, "alpha_vantage", "QQQ", DAY) is False
