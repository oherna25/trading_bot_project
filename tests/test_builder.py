from datetime import datetime, timezone

from bot.builder import builder_html, builder_preview_text
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore
from bot.theme import stylesheet

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)


def _seeded() -> SnapshotStore:
    store = SnapshotStore()
    store.save(ChainSnapshot(
        underlying="SPY", timestamp_utc=NOW, source_name="tradier_sandbox", latency_ms=5.0,
        contracts_list=(OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                                       open_interest=2000.0, gamma=0.05, delta=0.6,
                                       bid=2.4, ask=2.6, price=2.5),),
    ))
    store.save_bar("SPY", Bar(interval="1d", timestamp_utc=NOW, open=100.0, high=103.0,
                              low=99.0, close=102.0, volume=1200.0))
    return store


def test_builder_preview_lists_ideas():
    store = _seeded()
    text = builder_preview_text(store, Journal(), NOW, {"underlying": "SPY", "direction": "long"})
    assert "builder SPY" in text


def test_builder_html_has_form_and_viewport():
    store = _seeded()
    html = builder_html(store, Journal(), NOW, {"underlying": "SPY"})
    assert "<form" in html and "/builder" in html
    assert 'name="viewport"' in html


def test_stylesheet_is_responsive():
    css = stylesheet()
    assert "@media" in css
    assert "table-scroll" in css
    assert "auto-fit" in css
