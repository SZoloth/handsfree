import asyncio
import io
import math
import struct
import time
import wave

import pytest

from bargein.session import BargeInSession, PlaybackResult
from bargein.vad import DebouncedSpeechDetector


FRAME_MS = 30
SAMPLE_RATE = 16_000
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000


def pcm_frame(amplitude: int) -> bytes:
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 220 * i / SAMPLE_RATE)))
        for i in range(SAMPLES_PER_FRAME)
    )


def speech_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm_frame(12_000) * 8)
    return output.getvalue()


class EnergyVAD:
    def is_speech(self, pcm16: bytes, sample_rate: int) -> bool:
        samples = struct.iter_unpack("<h", pcm16)
        return max((abs(sample[0]) for sample in samples), default=0) > 1_000


class FakePlayback:
    def __init__(
        self,
        frames: list[bytes],
        *,
        frame_interval: float = FRAME_MS / 1000,
        stop_delay: float = 0.01,
    ):
        self.frames = frames
        self.frame_interval = frame_interval
        self.stop_delay = stop_delay
        self.stop_called_at: float | None = None
        self.completed = False

    async def play(self, _pcm: bytes, on_mic_frame):
        for frame in self.frames:
            if self.stop_called_at is not None:
                return PlaybackResult(interrupted=True, stopped_at=self.stop_called_at)
            await asyncio.sleep(self.frame_interval)
            await on_mic_frame(frame, SAMPLE_RATE)
        self.completed = True
        return PlaybackResult(interrupted=False)

    async def stop(self):
        await asyncio.sleep(self.stop_delay)
        self.stop_called_at = time.perf_counter()

    async def close_mic(self):
        return None


class ConverseSpy:
    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return "heard"


@pytest.mark.latency
def test_synthetic_loopback_stops_within_200ms(capsys):
    async def run():
        with wave.open(io.BytesIO(speech_wav()), "rb") as wav:
            pcm = wav.readframes(wav.getnframes())
        speech_frames = [
            pcm[offset:offset + SAMPLES_PER_FRAME * 2]
            for offset in range(0, len(pcm), SAMPLES_PER_FRAME * 2)
        ]
        playback = FakePlayback([pcm_frame(0)] * 2 + speech_frames)
        converse = ConverseSpy()
        detector = DebouncedSpeechDetector(EnergyVAD(), consecutive_frames=5)
        session = BargeInSession(playback, detector, converse)
        onset = time.perf_counter() + (3 * FRAME_MS / 1000)

        result = await session.run(b"assistant pcm", message="Long answer")

        assert playback.stop_called_at is not None
        latency_ms = (playback.stop_called_at - onset) * 1000
        assert latency_ms < 200
        assert result.stop_latency_ms is not None
        assert result.stop_latency_ms < 200
        assert converse.calls[0]["skip_tts"] is True
        assert converse.calls[0]["wait_for_response"] is True
        return latency_ms

    latency_ms = asyncio.run(run())
    with capsys.disabled():
        print(f"measured stop latency: {latency_ms:.1f}ms")


def test_no_barge_in_completes_playback_then_hands_off():
    async def run():
        playback = FakePlayback([pcm_frame(0)] * 3, frame_interval=0, stop_delay=0)
        converse = ConverseSpy()
        detector = DebouncedSpeechDetector(EnergyVAD(), consecutive_frames=5)
        session = BargeInSession(playback, detector, converse)

        result = await session.run(b"assistant pcm", message="Short answer", voice="af_sky")

        assert playback.completed is True
        assert playback.stop_called_at is None
        assert result.interrupted is False
        assert result.listen_result == "heard"
        assert converse.calls == [{
            "message": "Short answer",
            "voice": "af_sky",
            "skip_tts": True,
            "wait_for_response": True,
        }]

    asyncio.run(run())


def test_speech_wav_fixture_drives_vad_path():
    with wave.open(io.BytesIO(speech_wav()), "rb") as wav:
        pcm = wav.readframes(wav.getnframes())

    detector = DebouncedSpeechDetector(EnergyVAD(), consecutive_frames=5)
    frames = [pcm[offset:offset + SAMPLES_PER_FRAME * 2] for offset in range(0, len(pcm), SAMPLES_PER_FRAME * 2)]

    assert any(detector.observe(frame, SAMPLE_RATE) for frame in frames)
