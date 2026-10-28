import io
import json
import os
import subprocess
import tempfile
import wave
import pytest
from solobooks import server, voice
from tests.test_server import SyncThread, post


def tone(fmt: str) -> bytes:
    """One second of a 440 Hz tone in a phone-style audio format, made with ffmpeg."""
    args = {"amr": ["-ar", "8000", "-ac", "1", "-c:a", "libopencore_amrnb", "-b:a", "12.2k", "-f", "amr"],
            "m4a": ["-c:a", "aac", "-f", "ipod"], "mp3": ["-f", "mp3"], "ogg": ["-c:a", "libopus", "-f", "ogg"]}[fmt]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "tone")
        out = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                              *args, path], capture_output=True)
        if out.returncode != 0:
            pytest.skip(f"this ffmpeg can't encode {fmt} (the server only needs to decode it)")
        with open(path, "rb") as f:
            return f.read()


@pytest.mark.parametrize("fmt", ["amr", "m4a", "mp3", "ogg"])
def test_phone_audio_becomes_mono_16bit_24khz_wav(fmt):
    with wave.open(io.BytesIO(voice.to_wav(tone(fmt)))) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000)
        assert 0.8 < w.getnframes() / 24000 < 1.3


def test_garbage_audio_raises_a_clear_error():
    with pytest.raises(voice.VoiceError):
        voice.to_wav(b"not audio at all")


def test_transcribe_sends_what_the_meta_endpoint_expects(monkeypatch):
    seen = {}

    class Response:
        status_code = 200
        def json(self):
            return {"transcript": " drove 12 miles to the Henderson job ", "sessionId": "s1"}

    def fake_post(url, headers, files, timeout):
        seen.update(url=url, headers=headers, files=files)
        return Response()

    monkeypatch.setattr(voice.requests, "post", fake_post)
    monkeypatch.setattr(voice.config, "MODEL_API_KEY", "key123")
    assert voice.transcribe(tone("mp3"), keywords=["Fuel"]) == "drove 12 miles to the Henderson job"
    assert seen["url"] == "https://api.meta.ai/v1/asr/transcribe"
    assert seen["headers"] == {"Authorization": "Bearer key123"}
    name, body, ctype = seen["files"]["request"]
    assert name is None and ctype == "application/json"
    assert json.loads(body) == {"mode": "PUSH_TO_TALK", "model": "muse-voice-transcribe-1.0",
                                "audioEncoding": "WAV", "keywords": ["Fuel"]}
    assert seen["files"]["audio"][1][:4] == b"RIFF"


def test_transcribe_error_is_reported(monkeypatch):
    class Response:
        status_code, text = 401, "bad key"
    monkeypatch.setattr(voice.requests, "post", lambda *a, **k: Response())
    with pytest.raises(voice.VoiceError, match="401"):
        voice.transcribe(tone("mp3"))


@pytest.fixture
def sent(monkeypatch):
    texts = []
    monkeypatch.setattr(server, "send_text", lambda to, body: texts.append(body))
    monkeypatch.setattr(server.threading, "Thread", SyncThread)
    monkeypatch.setattr(server, "download", lambda url: b"audio-bytes")
    return texts


VOICE_NOTE = {"From": "+15550001111", "Body": "", "NumMedia": "1",
              "MediaUrl0": "https://api.twilio.com/m/1", "MediaContentType0": "audio/amr"}


def test_voice_note_goes_to_the_agent_and_reply_shows_what_was_heard(sent, monkeypatch):
    calls = []
    monkeypatch.setattr(server.voice, "transcribe", lambda audio, keywords=None: "they paid 400 cash")
    monkeypatch.setattr(server.agent, "run", lambda text, **k: calls.append((text, k)) or "✅ Recorded income (I1A2B)")
    r = post(server.app.test_client(), VOICE_NOTE)
    assert "voice note" in r.get_data(as_text=True)
    assert calls[0][0] == "they paid 400 cash" and calls[0][1]["source"] == "voice"
    assert sent == ['🎙️ Heard: "they paid 400 cash"\n\n✅ Recorded income (I1A2B)']


def test_silent_voice_note_does_not_reach_the_agent(sent, monkeypatch):
    monkeypatch.setattr(server.voice, "transcribe", lambda audio, keywords=None: "")
    monkeypatch.setattr(server.agent, "run", lambda *a, **k: pytest.fail("agent must not run"))
    post(server.app.test_client(), VOICE_NOTE)
    assert "couldn't hear anything" in sent[0]
