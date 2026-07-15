from __future__ import annotations

import httpx

from .audio_helper import PCM, decode_wav


class KokoroClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8880", timeout: float = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def synthesize(self, message: str, voice: str = "af_sky", speed: float = 1.0) -> PCM:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/v1/audio/speech",
                json={
                    "model": "kokoro",
                    "input": message,
                    "voice": voice,
                    "response_format": "wav",
                    "speed": speed,
                },
            )
            response.raise_for_status()
        return decode_wav(response.content)
