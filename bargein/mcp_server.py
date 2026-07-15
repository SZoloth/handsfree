from __future__ import annotations

from typing import Optional, Union

from fastmcp import FastMCP


mcp = FastMCP(
    "handsfree",
    instructions=(
        "Use speak_and_listen for spoken turns. It owns echo-cancelled playback, "
        "then hands listening to VoiceMode. If this server is unavailable, call "
        "the separate voicemode converse tool."
    ),
)


async def _voicemode_converse(**kwargs) -> str:
    from voice_mode.tools.converse import converse

    return await converse(**kwargs)


@mcp.tool()
async def speak_and_listen(
    message: str,
    voice: Optional[str] = "af_sky",
    listen_duration_max: float = 120.0,
    listen_duration_min: float = 2.0,
    timeout: float = 60.0,
    disable_silence_detection: Union[bool, str] = False,
    vad_aggressiveness: Optional[Union[int, str]] = None,
    speed: Optional[float] = 1.0,
    chime_enabled: Optional[Union[bool, str]] = None,
    ack: Union[bool, str] = False,
) -> str:
    """Speak through Apple's echo-cancelled engine, allow barge-in, then listen.

    The helper owns speaker output and its microphone tap together. After normal
    completion or an interruption, that tap is closed before VoiceMode starts
    Whisper recording. The first 100-300ms of an interrupted utterance may be
    clipped during the handoff.
    """
    from .service import SpeakAndListenService

    service = SpeakAndListenService(_voicemode_converse)
    return await service.speak_and_listen(
        message,
        voice=voice,
        listen_duration_max=listen_duration_max,
        listen_duration_min=listen_duration_min,
        timeout=timeout,
        disable_silence_detection=disable_silence_detection,
        vad_aggressiveness=vad_aggressiveness,
        speed=speed,
        chime_enabled=chime_enabled,
        ack=ack,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
