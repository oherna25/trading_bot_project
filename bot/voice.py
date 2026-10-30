from __future__ import annotations

import hashlib
import re
import shutil
import struct
import subprocess
from datetime import datetime
from typing import Mapping

from bot import autonomy
from bot.holdings import SPOKEN_HOLDINGS_USAGE, save_spoken_holdings
from bot.journal import Journal
from bot.models import (
    MODEL_REGISTRY,
    HttpPost,
    HttpPostMultipart,
    model_api_key,
    receipt_payload,
    select_backend,
    transcribe_audio,
)
from bot.research import handle_chat_query
from bot.shapes import ModelReceipt
from bot.store import SnapshotStore

WAV_RATES = (16000, 24000)
OGG_MAGIC = b"OggS"
CONVERT_TIMEOUT_SECONDS = 30.0
VOICE_KILL_WINDOW_SECONDS = 300.0

KILL_RE = re.compile(
    r"\b(kill|flatten|stop everything|emergency stop|halt trading|halt everything)\b",
    re.IGNORECASE,
)
KILL_GUARD_RE = re.compile(
    r"\b(kill|flatten)\s+(my|the|this|that|losers|winners|positions?|trades?)\b",
    re.IGNORECASE,
)
CONFIRM_RE = re.compile(r"\bconfirm\b", re.IGNORECASE)
HOLDINGS_CMD_RE = re.compile(r"holdings?|my book", re.IGNORECASE)
HOLDINGS_VERB_RE = re.compile(r"\b(save|set|add|record|remember|update|my book)\b", re.IGNORECASE)


def is_ogg(data: bytes) -> bool:
    return data[:4] == OGG_MAGIC


def convert_ogg_to_wav(data: bytes, timeout: float = CONVERT_TIMEOUT_SECONDS) -> bytes:
    if not is_ogg(data):
        raise ValueError("not an ogg file")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ValueError("ogg conversion needs ffmpeg on PATH")
    try:
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", "-f", "wav", "pipe:1"],
            input=data,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("ogg conversion timed out") from exc
    if completed.returncode != 0 or not completed.stdout:
        raise ValueError("ogg conversion failed")
    channels, rate, bits = wav_params(completed.stdout)
    if channels != 1 or bits != 16 or rate != 16000:
        raise ValueError(f"ogg conversion produced wav channels={channels} rate={rate} bits={bits}")
    return completed.stdout


def wav_params(data: bytes) -> tuple[int, int, int]:
    if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a wav file")
    offset = 12
    channels = rate = bits = 0
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        (size,) = struct.unpack("<I", data[offset + 4 : offset + 8])
        if chunk_id == b"fmt ":
            if size < 16:
                raise ValueError("bad wav fmt chunk")
            audio_format, channels, rate, _, _, bits = struct.unpack("<HHIIHH", data[offset + 8 : offset + 24])
            if audio_format != 1:
                raise ValueError("wav must be pcm")
        offset += 8 + size
    if not channels or not rate or not bits:
        raise ValueError("wav fmt chunk missing")
    return channels, rate, bits


def _turn_text(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    text = str(body.get("transcript") or "").strip()
    if text:
        return text
    turns = body.get("turns") or []
    return " ".join(str(t.get("transcript", "")) for t in turns if isinstance(t, dict)).strip()


def transcribe(
    audio_wav: bytes,
    journal: Journal,
    now: datetime,
    snapshot_id: str = "",
    env: Mapping[str, str] | None = None,
    http_post_multipart: HttpPostMultipart | None = None,
) -> str:
    channels, rate, bits = wav_params(audio_wav)
    if channels != 1 or bits != 16 or rate not in WAV_RATES:
        raise ValueError(f"unsupported wav: channels={channels} rate={rate} bits={bits}")
    backend, reason = select_backend("transcriber", journal, now, env)
    if backend != "model":
        raise ValueError(reason)
    spec = MODEL_REGISTRY["transcriber"]
    body, latency_ms = transcribe_audio(spec, model_api_key(env), audio_wav, http_post_multipart)
    transcript = _turn_text(body)
    if not transcript:
        raise ValueError("empty transcript")
    minutes = 0.0
    if isinstance(body, dict):
        try:
            minutes = float(body.get("audioDurationMs", 0) or 0) / 60000.0
        except (TypeError, ValueError):
            minutes = 0.0
    receipt = ModelReceipt(
        model_id=spec.model_id,
        role_name="transcriber",
        prompt_hash=hashlib.sha256(audio_wav).hexdigest(),
        tokens_in=0,
        tokens_out=0,
        cost_usd=minutes * spec.price_in,
        latency_ms=latency_ms,
    )
    journal.append(now, "model_receipt", receipt_payload(receipt, snapshot_id))
    return transcript


def is_kill_command(transcript: str) -> bool:
    return bool(KILL_RE.search(transcript)) and not KILL_GUARD_RE.search(transcript)


def is_holdings_command(transcript: str) -> bool:
    return bool(HOLDINGS_CMD_RE.search(transcript)) and bool(HOLDINGS_VERB_RE.search(transcript))


def _kill_armed(journal: Journal, now: datetime) -> bool:
    for entry in journal.entries():
        if entry.kind != "kill_armed" or entry.payload.get("source") != "voice":
            continue
        if (now - entry.timestamp_utc).total_seconds() <= VOICE_KILL_WINDOW_SECONDS:
            return True
    return False


def handle_voice_kill(transcript: str, journal: Journal, now: datetime) -> str | None:
    if not is_kill_command(transcript):
        return None
    if CONFIRM_RE.search(transcript):
        if not _kill_armed(journal, now):
            journal.append(now, "kill_rejected", {"source": "voice", "reason": "no armed kill"})
            return "no armed voice kill; say 'flatten everything' first"
        autonomy.live_enabled = False
        journal.append(now, "kill_fired", {"source": "voice"})
        return "voice kill switch fired: auto-ordering flattened, operator reset required"
    journal.append(now, "kill_armed", {"source": "voice"})
    return "voice kill armed: say 'confirm kill' within 5 minutes to flatten auto-ordering"


def handle_voice_holdings(
    transcript: str, store: SnapshotStore, journal: Journal, now: datetime
) -> str | None:
    if not is_holdings_command(transcript):
        return None
    try:
        return save_spoken_holdings(transcript, store, journal, now)
    except ValueError as exc:
        journal.append(now, "voice_holdings_rejected", {"reason": str(exc)})
        if SPOKEN_HOLDINGS_USAGE in str(exc):
            return str(exc)
        return f"{exc}. {SPOKEN_HOLDINGS_USAGE}"


def handle_voice_note(
    audio: bytes,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    grounding: bool = False,
    cards: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    http_post_multipart: HttpPostMultipart | None = None,
) -> str:
    if not live_models:
        journal.append(now, "voice_rejected", {"reason": "live models off"})
        return "voice notes need live models"
    try:
        audio_wav = convert_ogg_to_wav(audio) if is_ogg(audio) else audio
        if audio_wav is not audio:
            journal.append(now, "voice_converted", {"from": "ogg", "to": "wav"})
        transcript = transcribe(audio_wav, journal, now, "", env, http_post_multipart)
    except ValueError as exc:
        journal.append(now, "voice_rejected", {"reason": str(exc)})
        return f"voice note rejected: {exc}"
    except Exception as exc:
        journal.append(now, "voice_rejected", {"reason": type(exc).__name__})
        return f"voice note rejected: {type(exc).__name__}"
    journal.append(now, "voice_transcript", {"transcript": transcript})
    kill_reply = handle_voice_kill(transcript, journal, now)
    if kill_reply is not None:
        return kill_reply
    holdings_reply = handle_voice_holdings(transcript, store, journal, now)
    if holdings_reply is not None:
        return holdings_reply
    reply, _ = handle_chat_query(transcript, underlying, store, journal, now, live_models, grounding, cards, env, http_post)
    return reply
