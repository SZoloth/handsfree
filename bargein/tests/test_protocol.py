import io
import struct

from bargein.protocol import FrameReader, MessageType, encode_frame


def test_frame_round_trip_handles_partial_reads():
    encoded = encode_frame(MessageType.MIC_PCM, b"speech")

    class SlowStream(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 2))

    frame = FrameReader(SlowStream(encoded)).read()

    assert frame.message_type is MessageType.MIC_PCM
    assert frame.payload == b"speech"


def test_frame_header_is_little_endian_type_plus_length():
    encoded = encode_frame(MessageType.STOP, b"")

    assert encoded == struct.pack("<BI", MessageType.STOP, 0)
