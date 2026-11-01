import pytest


@pytest.fixture(autouse=True)
def _clear_response_and_card_caches():
    from bot.muse_http import clear_response_cache
    from bot.tradecards import clear_reason_queue, clear_trade_cards_cache

    clear_response_cache()
    clear_trade_cards_cache()
    clear_reason_queue()
    yield
    clear_response_cache()
    clear_trade_cards_cache()
    clear_reason_queue()
