import struct
from datetime import datetime, timedelta, timezone

import pytest

from bot import autonomy
from bot.holdings import parse_spoken_holdings, save_spoken_holdings
from bot.journal import Journal
from bot.store import SnapshotStore
from bot.voice import handle_voice_note

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
FULL_ENV = {"META_MODEL_API_KEY": "test-key", "MODEL_DAY_BUDGET_USD": "5"}


def _wav_bytes(rate=16000, channels=1, bits=16, nframes=16000):
    block = channels * bits // 8
    data = bytes(nframes * block)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(data), b"WAVE", b"fmt ", 16, 1, channels, rate,
        rate * block, block, bits, b"data", len(data),
    )
    return header + data


def _say(transcript):
    def post(url, headers, parts, timeout):
        return {"transcript": transcript, "audioDurationMs": 30000}

    return post


def test_parse_spoken_stock():
    holdings = parse_spoken_holdings("long 100 SPY stock")
    assert [(h.symbol, h.kind, h.side, h.qty, h.strike) for h in holdings] == [
        ("SPY", "stock", "long", 100.0, None)
    ]


def test_parse_spoken_option_with_expiry():
    holdings = parse_spoken_holdings("short 2 spy put 760 expiring 2026-11-20")
    assert [(h.symbol, h.kind, h.side, h.qty, h.strike, h.expiry) for h in holdings] == [
        ("SPY", "put", "short", 2.0, 760.0, "2026-11-20")
    ]


def test_parse_spoken_multiple():
    holdings = parse_spoken_holdings("long 100 SPY stock and short 2 SPY put 760")
    assert len(holdings) == 2
    assert holdings[1].strike == 760.0


def test_parse_spoken_rejects_option_without_strike():
    with pytest.raises(ValueError, match="strike"):
        parse_spoken_holdings("short 2 SPY put")


def test_parse_spoken_rejects_garbage():
    with pytest.raises(ValueError, match="say holdings like"):
        parse_spoken_holdings("hello there")


def test_save_spoken_holdings_round_trip():
    store, journal = SnapshotStore(), Journal()
    reply = save_spoken_holdings("long 100 SPY stock", store, journal, NOW)
    assert "saved 1 holding" in reply
    assert store.read_holdings()[0].symbol == "SPY"
    assert journal.entries()[-1].kind == "holdings_saved"


def test_voice_note_saves_holdings():
    store, journal = SnapshotStore(), Journal()
    reply = handle_voice_note(
        _wav_bytes(), "SPY", store, journal, NOW, live_models=True,
        env=dict(FULL_ENV), http_post_multipart=_say("save holding long 100 SPY stock"),
    )
    assert "saved 1 holding" in reply
    assert store.read_holdings()[0].qty == 100.0


def test_voice_holdings_parse_failure_guides():
    store, journal = SnapshotStore(), Journal()
    reply = handle_voice_note(
        _wav_bytes(), "SPY", store, journal, NOW, live_models=True,
        env=dict(FULL_ENV), http_post_multipart=_say("save holding hello there"),
    )
    assert "say holdings like" in reply
    assert journal.entries()[-1].kind == "voice_holdings_rejected"


def _voice(journal, transcript, now):
    return handle_voice_note(
        _wav_bytes(), "SPY", SnapshotStore(), journal, now, live_models=True,
        env=dict(FULL_ENV), http_post_multipart=_say(transcript),
    )


def test_voice_kill_arms_then_confirms():
    journal = Journal()
    armed = _voice(journal, "flatten everything now", NOW)
    assert "confirm kill" in armed
    assert autonomy.live_enabled is False
    autonomy.live_enabled = True
    try:
        confirmed = _voice(journal, "confirm kill", NOW + timedelta(seconds=60))
    finally:
        autonomy.live_enabled = False
    assert "kill switch fired" in confirmed
    assert autonomy.live_enabled is False
    kinds = [entry.kind for entry in journal.entries()]
    assert "kill_armed" in kinds and "kill_fired" in kinds


def test_voice_kill_confirm_without_arm_rejected():
    journal = Journal()
    reply = _voice(journal, "confirm kill", NOW)
    assert "no armed voice kill" in reply
    assert "kill_fired" not in [entry.kind for entry in journal.entries()]


def test_voice_kill_arm_expires():
    journal = Journal()
    _voice(journal, "stop everything", NOW - timedelta(minutes=6))
    reply = _voice(journal, "confirm kill", NOW)
    assert "no armed voice kill" in reply
    assert "kill_fired" not in [entry.kind for entry in journal.entries()]


def test_voice_kill_never_fires_on_first_command():
    journal = Journal()
    autonomy.live_enabled = True
    try:
        _voice(journal, "flatten everything now", NOW)
        assert autonomy.live_enabled is True
    finally:
        autonomy.live_enabled = False
