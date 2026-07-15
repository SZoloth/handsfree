import asyncio

from bargein.audio_helper import HelperUnavailable
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
