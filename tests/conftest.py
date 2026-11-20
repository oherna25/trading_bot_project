from datetime import datetime, timezone

import pytest

FEED_CLOCK = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear_response_and_card_caches():
    from bot.muse_http import clear_response_cache
    from bot.tradecards import clear_reason_cache, clear_reason_queue, clear_trade_cards_cache

    clear_response_cache()
    clear_trade_cards_cache()
    clear_reason_cache()
    clear_reason_queue()
    yield
    clear_response_cache()
    clear_trade_cards_cache()
    clear_reason_cache()
    clear_reason_queue()


@pytest.fixture(autouse=True)
def _pin_feed_clock(monkeypatch):
    # Feed fixtures carry fixed expiry dates, so the feed clock must not follow the calendar.
    monkeypatch.setattr("bot.feeds._now_utc", lambda: FEED_CLOCK)
