import asyncio
from types import SimpleNamespace

import pytest

from bargein.audio_helper import HelperPlayback, HelperUnavailable, PCM
from bargein.protocol import HEADER, MessageType, encode_frame
from bargein.service import SpeakAndListenService


class BrokenPlayback:
    def __init__(self):
        self.closed = False

    async def play(self, _pcm, _on_mic_frame):
        raise HelperUnavailable("daemon down")

    async def stop(self):
        raise AssertionError("stop should not be called")

    async def close_mic(self):
        self.closed = True


class FakeTTS:
    async def synthesize(self, *_args, **_kwargs):
        return b"wav"


class SilentDetector:
    def reset(self):
        return None

    def observe(self, _frame, _sample_rate):
        return False


class FakeStdin:
    def write(self, _data):
        return None

    async def drain(self):
        return None


class FramedPlayback(HelperPlayback):
    def __init__(self, frame):
        super().__init__()
        self.frame = frame

    async def _start(self):
        stdout = asyncio.StreamReader()
        stdout.feed_data(self.frame)
        stdout.feed_eof()
        self.process = SimpleNamespace(
            stdin=FakeStdin(),
            stdout=stdout,
            stderr=None,
            returncode=0,
        )


def assert_helper_frame_falls_back(frame):
    async def run():
        playback = FramedPlayback(frame)
        calls = []

        async def converse(**kwargs):
            calls.append(kwargs)
            return "fallback transcript"

        class PCMTTS:
            async def synthesize(self, *_args, **_kwargs):
                return PCM(data=b"", sample_rate=16_000, channels=1, sample_width=2)

        service = SpeakAndListenService(
            converse,
            tts=PCMTTS(),
            playback_factory=lambda: playback,
            detector_factory=SilentDetector,
        )

        result = await service.speak_and_listen("hello")

        assert result == "fallback transcript"
        assert len(calls) == 1
        assert calls[0]["wait_for_response"] is True
        assert "skip_tts" not in calls[0]
        assert playback.process is None

    asyncio.run(run())


def test_helper_failure_closes_mic_before_plain_voicemode_fallback():
    async def run():
        playback = BrokenPlayback()
        observed = []

        async def converse(**kwargs):
            observed.append((playback.closed, kwargs))
            return "fallback transcript"

        service = SpeakAndListenService(
            converse,
            tts=FakeTTS(),
            playback_factory=lambda: playback,
            detector_factory=SilentDetector,
        )
        result = await service.speak_and_listen("hello", voice="af_sky")

        assert result == "fallback transcript"
        assert observed[0][0] is True
        assert observed[0][1]["wait_for_response"] is True
        assert "skip_tts" not in observed[0][1]

    asyncio.run(run())


@pytest.mark.parametrize(
    "corrupted_frame",
    [
        encode_frame(MessageType.EVENT, b"{not-json"),
        HEADER.pack(0xFF, 0),
    ],
    ids=["invalid-json", "unknown-message-type"],
)
def test_corrupted_helper_frame_falls_back_to_plain_voicemode(corrupted_frame):
    assert_helper_frame_falls_back(corrupted_frame)


def test_configuration_change_event_falls_back_to_plain_voicemode():
    frame = encode_frame(
        MessageType.EVENT,
        b'{"event":"helper_unavailable","message":"audio device changed"}',
    )

    assert_helper_frame_falls_back(frame)
