from __future__ import annotations

import dataclasses
import enum
import struct
from typing import BinaryIO


HEADER = struct.Struct("<BI")


class MessageType(enum.IntEnum):
    PLAY_PCM = 0x01
    STOP = 0x02
    SHUTDOWN = 0x03
    MIC_METADATA = 0x81
    MIC_PCM = 0x82
    EVENT = 0x83


@dataclasses.dataclass(frozen=True)
class Frame:
    message_type: MessageType
    payload: bytes


def encode_frame(message_type: MessageType, payload: bytes = b"") -> bytes:
    return HEADER.pack(message_type, len(payload)) + payload


class FrameReader:
    def __init__(self, stream: BinaryIO):
        self._stream = stream

    def _read_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self._stream.read(size - len(chunks))
            if not chunk:
                raise EOFError(f"stream ended with {size - len(chunks)} bytes missing")
            chunks.extend(chunk)
        return bytes(chunks)

    def read(self) -> Frame:
        raw_type, length = HEADER.unpack(self._read_exact(HEADER.size))
        return Frame(MessageType(raw_type), self._read_exact(length))
