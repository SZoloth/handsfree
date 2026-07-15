from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any, Awaitable, Callable, Protocol

from .vad import DebouncedSpeechDetector


logger = logging.getLogger("handsfree.bargein")


@dataclasses.dataclass(frozen=True)
class PlaybackResult:
    interrupted: bool
    stopped_at: float | None = None
    listen_result: str | None = None
    stop_latency_ms: float | None = None


class Playback(Protocol):
    async def play(self, pcm: Any, on_mic_frame) -> PlaybackResult: ...
    async def stop(self) -> None: ...
    async def close_mic(self) -> None: ...


class BargeInSession:
    def __init__(
        self,
        playback: Playback,
        detector: DebouncedSpeechDetector,
        converse: Callable[..., Awaitable[str]],
    ):
        self.playback = playback
        self.detector = detector
        self.converse = converse
        self._stop_requested = False

    async def run(self, pcm: Any, *, message: str, **converse_kwargs) -> PlaybackResult:
        self.detector.reset()
        self._stop_requested = False
        speech_onset: float | None = None
        stop_latency_ms: float | None = None

        async def on_mic_frame(frame: bytes, sample_rate: int) -> None:
            nonlocal speech_onset, stop_latency_ms
            if not self._stop_requested and self.detector.observe(frame, sample_rate):
                if speech_onset is None:
                    speech_onset = time.perf_counter()
                self._stop_requested = True
                await self.playback.stop()
                stop_latency_ms = (time.perf_counter() - speech_onset) * 1000
                logger.info("barge-in playback stop requested %.1fms after debounced trigger", stop_latency_ms)
            elif self.detector.last_voiced and speech_onset is None:
                speech_onset = time.perf_counter()

        try:
            result = await self.playback.play(pcm, on_mic_frame)
        finally:
            # This closes the AUVoiceProcessingIO mic tap before VoiceMode opens
            # its independent listen stream. The two ownership windows cannot overlap.
            await self.playback.close_mic()

        listen_result = await self.converse(
            message=message,
            **converse_kwargs,
            skip_tts=True,
            wait_for_response=True,
        )
        return dataclasses.replace(
            result,
            listen_result=listen_result,
            stop_latency_ms=stop_latency_ms,
        )
