"""
Voice notes: phones send audio as AMR, M4A, MP3, OGG and so on. Muse Voice
Transcribe takes a mono 16-bit WAV, so ffmpeg converts the note first.
"""
import json
import os
import subprocess
import tempfile
import requests
from . import config

TRANSCRIBE_URL = os.getenv("TRANSCRIBE_URL", config.META_BASE_URL.rstrip("/") + "/asr/transcribe")
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "muse-voice-transcribe-1.0")


class VoiceError(Exception):
    pass


def to_wav(audio: bytes) -> bytes:
    """Converts any audio ffmpeg understands to mono 16-bit 24 kHz WAV."""
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "note"), os.path.join(tmp, "note.wav")
        with open(src, "wb") as f:
            f.write(audio)
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
             "-ac", "1", "-ar", "24000", "-sample_fmt", "s16", dst],
            capture_output=True, timeout=60)
        if result.returncode != 0:
            raise VoiceError(f"ffmpeg could not convert the audio: {result.stderr.decode()[:200]}")
        with open(dst, "rb") as f:
            return f.read()


def transcribe(audio: bytes, keywords: list = None) -> str:
    """Returns the text of a voice note."""
    request = {"mode": "PUSH_TO_TALK", "model": TRANSCRIBE_MODEL, "audioEncoding": "WAV"}
    if keywords:
        request["keywords"] = keywords
    response = requests.post(
        TRANSCRIBE_URL,
        headers={"Authorization": f"Bearer {config.MODEL_API_KEY}"},
        files={
            # The settings travel as a JSON part, not as form fields
            "request": (None, json.dumps(request), "application/json"),
            "audio": ("note.wav", to_wav(audio), "audio/wav"),
        },
        timeout=120)
    if response.status_code != 200:
        raise VoiceError(f"Transcription failed [{response.status_code}]: {response.text[:200]}")
    return (response.json().get("transcript") or "").strip()
