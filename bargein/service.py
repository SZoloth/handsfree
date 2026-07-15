from __future__ import annotations

import logging
from typing import Awaitable, Callable

from .audio_helper import HelperPlayback, HelperUnavailable
from .session import BargeInSession
from .tts import KokoroClient
from .vad import DebouncedSpeechDetector, SileroVAD


logger = logging.getLogger("handsfree.bargein")


class SpeakAndListenService:
    def __init__(
        self,
        converse: Callable[..., Awaitable[str]],
        *,
        tts: KokoroClient | None = None,
        playback_factory=HelperPlayback,
        detector_factory=None,
    ):
        self.converse = converse
        self.tts = tts or KokoroClient()
        self.playback_factory = playback_factory
        self.detector_factory = detector_factory or (
            lambda: DebouncedSpeechDetector(SileroVAD(), consecutive_frames=5)
        )

    async def speak_and_listen(self, message: str, **kwargs) -> str:
        voice = kwargs.pop("voice", None) or "af_sky"
        speed = float(kwargs.pop("speed", 1.0) or 1.0)
        playback = self.playback_factory()
        try:
            pcm = await self.tts.synthesize(message, voice=voice, speed=speed)
            try:
                detector = self.detector_factory()
            except Exception as error:
                raise HelperUnavailable(f"Silero VAD could not initialize: {error}") from error
            session = BargeInSession(
                playback,
                detector,
                self.converse,
            )
            result = await session.run(pcm, message=message, voice=voice, speed=speed, **kwargs)
            return result.listen_result or "No speech was captured."
        except Exception as error:
            # Protocol failures include malformed JSON, unknown message types,
            # truncated frames, and future helper errors we do not recognize.
            # Always release helper-owned audio before VoiceMode opens PortAudio.
            try:
                await playback.close_mic()
            except Exception as cleanup_error:
                logger.warning("barge-in helper cleanup failed: %s", cleanup_error)
            logger.warning("barge-in unavailable; using half-duplex fallback: %s", error)
            return await self.converse(
                message=message,
                voice=voice,
                speed=speed,
                wait_for_response=True,
                **kwargs,
            )
