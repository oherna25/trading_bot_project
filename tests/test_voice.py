import json
import shutil
import struct
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from bot.journal import Journal
from bot.models import select_backend
from bot.research import handle_chat_query
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore
from bot.voice import convert_ogg_to_wav, handle_voice_note, is_ogg, transcribe, wav_params

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

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


def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=1000.0,
                    gamma=0.05, price=2.50, bid=2.40, ask=2.60, iv=0.30,
                ),
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="put", open_interest=2000.0,
                    gamma=0.03, price=3.00, bid=2.90, ask=3.10, iv=0.32,
                ),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW - timedelta(days=1),
                               open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0))
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    return store


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


def _strict_multipart(bodies):
    log = []

    def post(url, headers, parts, timeout):
        log.append(parts)
        if len(log) > len(bodies):
            raise AssertionError("unexpected extra http call")
        item = bodies[len(log) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    post.log = log
    return post


def _tool_leg():
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {"name": "get_gex_profile", "arguments": json.dumps({"underlying": "TEST"})},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def _final_leg(content):
    return {
        "choices": [{"message": {"content": content, "tool_calls": []}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 30},
    }


def _responses_body(text):
    return {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }


def _transcript_body(text="gex brief"):
    return {"transcript": text, "audioDurationMs": 1600, "turns": []}


def test_wav_params_read_valid_header():
    assert wav_params(_wav_bytes()) == (1, 16000, 16)
    assert wav_params(_wav_bytes(rate=24000)) == (1, 24000, 16)


@pytest.mark.parametrize("data", [_wav_bytes(rate=22050), _wav_bytes(channels=2), b"not audio at all" + bytes(64)])
def test_transcribe_rejects_bad_audio_without_network(data):
    def explode(*args, **kwargs):
        raise AssertionError("bad audio must not call http")

    with pytest.raises(ValueError):
        transcribe(data, Journal(), NOW, "", FULL_ENV, explode)


def test_transcribe_posts_parts_and_journals_receipt():
    http_multipart = _strict_multipart([_transcript_body()])
    journal = Journal()
    audio = _wav_bytes()
    assert transcribe(audio, journal, NOW, "TEST:snap", FULL_ENV, http_multipart) == "gex brief"
    parts = {name: (content_type, filename, data) for name, content_type, filename, data in http_multipart.log[0]}
    request = json.loads(parts["request"][2].decode("utf-8"))
    assert request["model"] == "muse-voice-transcribe-1.0"
    assert request["audioEncoding"] == "WAV"
    assert parts["audio"][2] == audio
    receipts = [e for e in journal.entries() if e.kind == "model_receipt"]
    assert len(receipts) == 1
    assert receipts[0].payload["role_name"] == "transcriber"
    assert receipts[0].payload["cost_usd"] == pytest.approx(1600 / 60000 * 0.003)
    assert len(receipts[0].payload["prompt_hash"]) == 64
    assert receipts[0].payload["snapshot_id"] == "TEST:snap"


def test_transcribe_empty_transcript_raises_and_journals_nothing():
    journal = Journal()
    with pytest.raises(ValueError, match="empty transcript"):
        transcribe(_wav_bytes(), journal, NOW, "", FULL_ENV, _strict_multipart([{"transcript": "", "turns": []}]))
    assert journal.entries() == []


def test_transcribe_missing_key_raises_without_network():
    def explode(*args, **kwargs):
        raise AssertionError("no key means no http")

    with pytest.raises(ValueError, match="missing META_MODEL_API_KEY"):
        transcribe(_wav_bytes(), Journal(), NOW, "", {}, explode)


def test_voice_note_defaults_to_rejected_offline():
    def explode(*args, **kwargs):
        raise AssertionError("offline path must not call http")

    journal = Journal()
    reply = handle_voice_note(_wav_bytes(), "TEST", _seeded_store(), journal, NOW,
                              env=FULL_ENV, http_post=explode, http_post_multipart=explode)
    assert reply == "voice notes need live models"
    assert journal.entries()[-1].kind == "voice_rejected"


def test_voice_note_bad_audio_rejects_without_signal():
    journal = Journal()
    reply = handle_voice_note(b"junk" + bytes(64), "TEST", _seeded_store(), journal, NOW,
                              live_models=True, env=FULL_ENV)
    assert reply.startswith("voice note rejected")
    assert not any(e.kind == "signal" for e in journal.entries())


def test_voice_brief_matches_typed_brief_numbers():
    typed_reply, _ = handle_chat_query("gex brief", "TEST", _seeded_store(), Journal(), NOW)
    http_post = _strict_post([_tool_leg(), _final_leg("Model thesis.\nDIRECTION: short\nCONFIDENCE: 0.8")])
    journal = Journal()
    reply = handle_voice_note(_wav_bytes(), "TEST", _seeded_store(), journal, NOW,
                              live_models=True, env=FULL_ENV, http_post=http_post,
                              http_post_multipart=_strict_multipart([_transcript_body()]))
    for line in ("total exposure: $-104,040", "walls: 100 $-104,040"):
        assert line in typed_reply
        assert line in reply
    assert [e.kind for e in journal.entries()].count("model_receipt") == 3
    transcript = next(e for e in journal.entries() if e.kind == "voice_transcript")
    assert transcript.payload["transcript"] == "gex brief"
    assert any(e.kind == "signal" for e in journal.entries())


def test_grounding_appends_note_and_receipt():
    http_post = _strict_post([_tool_leg(), _final_leg("Model thesis.\nDIRECTION: short\nCONFIDENCE: 0.8"),
                              _responses_body("No contradiction found.")])
    journal = Journal()
    _, signal = handle_chat_query("brief", "TEST", _seeded_store(), journal, NOW,
                                  live_models=True, grounding=True, env=FULL_ENV, http_post=http_post)
    assert signal.thesis == "Model thesis.\nGrounded note: No contradiction found."
    grounding = [e for e in journal.entries() if e.kind == "model_receipt" and e.payload["role_name"] == "grounding"]
    assert len(grounding) == 1
    assert grounding[0].payload["cost_usd"] == pytest.approx(50 / 1e6 * 1.25 + 10 / 1e6 * 4.25)


def test_grounding_uses_final_message_text():
    body = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "I will search now."}]},
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{"type": "output_text", "text": "No contradiction found."}]},
        ],
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }
    http_post = _strict_post([_tool_leg(), _final_leg("Model thesis.\nDIRECTION: short\nCONFIDENCE: 0.8"), body])
    _, signal = handle_chat_query("brief", "TEST", _seeded_store(), Journal(), NOW,
                                  live_models=True, grounding=True, env=FULL_ENV, http_post=http_post)
    assert signal.thesis == "Model thesis.\nGrounded note: No contradiction found."


def test_grounding_failure_skips_silently():
    http_post = _strict_post([_tool_leg(), _final_leg("Model thesis.\nDIRECTION: short\nCONFIDENCE: 0.8"),
                              RuntimeError("search down")])
    journal = Journal()
    _, signal = handle_chat_query("brief", "TEST", _seeded_store(), journal, NOW,
                                  live_models=True, grounding=True, env=FULL_ENV, http_post=http_post)
    assert signal.thesis == "Model thesis."
    skipped = [e for e in journal.entries() if e.kind == "grounding_skipped"]
    assert len(skipped) == 1
    assert skipped[0].payload["reason"] == "RuntimeError"


def test_grounding_off_makes_no_extra_call():
    http_post = _strict_post([_tool_leg(), _final_leg("Model thesis.\nDIRECTION: short\nCONFIDENCE: 0.8")])
    journal = Journal()
    _, signal = handle_chat_query("brief", "TEST", _seeded_store(), journal, NOW,
                                  live_models=True, env=FULL_ENV, http_post=http_post)
    assert signal.thesis == "Model thesis."
    assert not any(e.kind == "grounding_skipped" for e in journal.entries())


def test_grounding_role_selects_model():
    assert select_backend("grounding", Journal(), NOW, FULL_ENV) == ("model", "")


def _ogg_bytes():
    completed = subprocess.run(
        [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-ac", "1", "-ar", "16000", "-f", "ogg", "pipe:1"],
        input=_wav_bytes(nframes=8000),
        capture_output=True,
        timeout=30.0,
        check=True,
    )
    return completed.stdout


def test_is_ogg_detects_magic_only():
    assert is_ogg(b"OggS" + bytes(64)) is True
    assert is_ogg(_wav_bytes()) is False
    assert is_ogg(b"") is False


def test_convert_rejects_non_ogg_without_ffmpeg():
    with pytest.raises(ValueError, match="not an ogg file"):
        convert_ogg_to_wav(_wav_bytes())


@needs_ffmpeg
def test_convert_rejects_truncated_ogg():
    with pytest.raises(ValueError, match="ogg conversion failed"):
        convert_ogg_to_wav(b"OggS" + bytes(16))


@needs_ffmpeg
def test_convert_ogg_to_16k_mono_wav():
    wav = convert_ogg_to_wav(_ogg_bytes())
    assert wav_params(wav) == (1, 16000, 16)


@needs_ffmpeg
def test_voice_note_accepts_ogg_and_journals_conversion():
    http_post = _strict_post([_tool_leg(), _final_leg("Model thesis.\nDIRECTION: short\nCONFIDENCE: 0.8")])
    journal = Journal()
    reply = handle_voice_note(_ogg_bytes(), "TEST", _seeded_store(), journal, NOW,
                              live_models=True, env=FULL_ENV, http_post=http_post,
                              http_post_multipart=_strict_multipart([_transcript_body()]))
    assert "total exposure: $-104,040" in reply
    kinds = [e.kind for e in journal.entries()]
    assert "voice_converted" in kinds
    assert "voice_transcript" in kinds
    assert "signal" in kinds
