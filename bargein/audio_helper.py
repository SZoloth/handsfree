from __future__ import annotations

import atexit
import asyncio
import dataclasses
import json
import os
import signal
import struct
import threading
import time
import wave
from io import BytesIO
from pathlib import Path
from typing import Awaitable, Callable

from .protocol import HEADER, MessageType, encode_frame
from .session import PlaybackResult


_LIVE_HELPER_GROUPS: set[int] = set()
_LIVE_HELPER_GROUPS_LOCK = threading.Lock()
_SIGNAL_HANDLERS_INSTALLED = False


def _signal_helper_group(process_group: int, signal_number: int) -> None:
    try:
        os.killpg(process_group, signal_number)
    except ProcessLookupError:
        pass


def _terminate_live_helper_groups(*, force: bool = False) -> None:
    with _LIVE_HELPER_GROUPS_LOCK:
        process_groups = tuple(_LIVE_HELPER_GROUPS)
    for process_group in process_groups:
        _signal_helper_group(process_group, signal.SIGTERM)

    if not process_groups:
        return
    if not force:
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            if all(not _helper_group_exists(group) for group in process_groups):
                return
            time.sleep(0.01)
    for process_group in process_groups:
        _signal_helper_group(process_group, signal.SIGKILL)


def _helper_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


def _install_signal_cleanup_handlers() -> None:
    global _SIGNAL_HANDLERS_INSTALLED
    if _SIGNAL_HANDLERS_INSTALLED:
        return
    try:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(signal_number)

            def cleanup_handler(signum, frame, previous_handler=previous):
                _terminate_live_helper_groups(force=True)
                if callable(previous_handler):
                    previous_handler(signum, frame)
                elif previous_handler == signal.SIG_IGN:
                    return
                else:
                    signal.signal(signum, signal.SIG_DFL)
                    os.kill(os.getpid(), signum)

            signal.signal(signal_number, cleanup_handler)
    except ValueError:
        # Signal handlers can only be installed from Python's main thread. The
        # atexit guard still covers embedded/non-main-thread use.
        return
    _SIGNAL_HANDLERS_INSTALLED = True


atexit.register(_terminate_live_helper_groups)


class HelperUnavailable(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class PCM:
    data: bytes
    sample_rate: int
    channels: int
    sample_width: int

    def helper_payload(self) -> bytes:
        return struct.pack("<III", self.sample_rate, self.channels, self.sample_width) + self.data


def decode_wav(wav_bytes: bytes) -> PCM:
    try:
        with wave.open(BytesIO(wav_bytes), "rb") as source:
            pcm = PCM(
                data=source.readframes(source.getnframes()),
                sample_rate=source.getframerate(),
                channels=source.getnchannels(),
                sample_width=source.getsampwidth(),
            )
    except (wave.Error, EOFError) as error:
        raise HelperUnavailable(f"Kokoro returned an invalid WAV: {error}") from error
    if pcm.channels != 1 or pcm.sample_width != 2:
        raise HelperUnavailable(
            f"helper requires mono 16-bit WAV, got channels={pcm.channels} width={pcm.sample_width}"
        )
    return pcm


class HelperPlayback:
    def __init__(self, helper_path: Path | None = None):
        default = Path(__file__).parents[1] / ".build" / "release" / "handsfree-audio-helper"
        self.helper_path = helper_path or Path(os.environ.get("HANDSFREE_AUDIO_HELPER", default))
        self.process: asyncio.subprocess.Process | None = None
        self.sample_rate = 16_000
        self.stop_called_at: float | None = None
        self.output_owned = False

    async def _start(self) -> None:
        if not self.helper_path.is_file():
            raise HelperUnavailable(f"audio helper is missing: {self.helper_path}")
        _install_signal_cleanup_handlers()
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(self.helper_path),
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise HelperUnavailable(f"audio helper could not start: {error}") from error
        with _LIVE_HELPER_GROUPS_LOCK:
            _LIVE_HELPER_GROUPS.add(self.process.pid)

    async def _read_frame(self) -> tuple[MessageType, bytes]:
        if self.process is None or self.process.stdout is None:
            raise HelperUnavailable("audio helper is not running")
        try:
            header = await self.process.stdout.readexactly(HEADER.size)
            raw_type, length = HEADER.unpack(header)
            payload = await self.process.stdout.readexactly(length)
        except asyncio.IncompleteReadError as error:
            details = ""
            if self.process.stderr is not None:
                details = (await self.process.stderr.read()).decode(errors="replace").strip()
            raise HelperUnavailable(f"audio helper exited during playback: {details}") from error
        return MessageType(raw_type), payload

    async def play(
        self,
        pcm: PCM,
        on_mic_frame: Callable[[bytes, int], Awaitable[None]],
    ) -> PlaybackResult:
        await self._start()
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(encode_frame(MessageType.PLAY_PCM, pcm.helper_payload()))
        await self.process.stdin.drain()

        while True:
            message_type, payload = await self._read_frame()
            if message_type is MessageType.MIC_METADATA:
                metadata = json.loads(payload)
                if not metadata.get("voice_processing_input") or not metadata.get("voice_processing_output"):
                    raise HelperUnavailable("helper did not own voice-processed input and output")
                self.sample_rate = round(float(metadata["sample_rate"]))
            elif message_type is MessageType.MIC_PCM:
                if self.output_owned:
                    await on_mic_frame(payload, self.sample_rate)
            elif message_type is MessageType.EVENT:
                event = json.loads(payload)
                name = event.get("event")
                if name == "playback_started":
                    if not event.get("voice_processing_input") or not event.get("voice_processing_output"):
                        raise HelperUnavailable("helper started playback without shared AEC ownership")
                    self.output_owned = True
                if name == "playback_completed":
                    self.output_owned = False
                    return PlaybackResult(interrupted=False)
                if name == "playback_stopped":
                    self.output_owned = False
                    return PlaybackResult(interrupted=True, stopped_at=self.stop_called_at)
                if name == "error":
                    raise HelperUnavailable(event.get("message", "unknown helper error"))
                if name == "helper_unavailable":
                    raise HelperUnavailable(
                        event.get("message", "audio helper became unavailable")
                    )

    async def stop(self) -> None:
        loop = asyncio.get_running_loop()
        self.stop_called_at = loop.time()
        if self.process is None or self.process.stdin is None:
            raise HelperUnavailable("cannot stop a helper that is not running")
        self.process.stdin.write(encode_frame(MessageType.STOP))
        await self.process.stdin.drain()

    async def close_mic(self) -> None:
        if self.process is None:
            return
        process = self.process
        try:
            if process.returncode is None and process.stdin is not None:
                try:
                    process.stdin.write(encode_frame(MessageType.SHUTDOWN))
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    _signal_helper_group(process.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=1)
                    except asyncio.TimeoutError:
                        _signal_helper_group(process.pid, signal.SIGKILL)
                        await process.wait()
        finally:
            with _LIVE_HELPER_GROUPS_LOCK:
                _LIVE_HELPER_GROUPS.discard(getattr(process, "pid", -1))
            self.process = None
            self.output_owned = False
