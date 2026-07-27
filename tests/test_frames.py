"""Framing: checksums, encoding, decoding and reassembly."""

import pytest

import miramode as m

from . import captures


def test_checksum_makes_a_frame_sum_to_zero():
    frame = m.encode_frame(m.Opcode.RUN_PRESET, b"\x02")
    assert sum(frame) % 256 == 0


@pytest.mark.parametrize(
    ("opcode", "payload", "expected"),
    [
        # Confirmed against the valve: this ran outlet 1 at 39.0C.
        (m.Opcode.SET_OUTLETS, b"\x01\x86\x64\x01", "aa5500ab040186640166"),
        # Confirmed: this stopped it.
        (m.Opcode.SET_OUTLETS, b"\x01\x86\x00\x00", "aa5500ab0401860000cb"),
        # Confirmed: temperature 0 means "leave it alone".
        (m.Opcode.SET_OUTLETS, b"\x00\x00\x00\x00", "aa5500ab040000000052"),
        # This opcode and payload started the bath preset; the checksum
        # is 0x4d because the rest of the frame sums to 435.
        (m.Opcode.RUN_PRESET, b"\x02", "aa5500b101024d"),
        # Confirmed: read the state.
        (m.Opcode.GET_STATE, b"\x02", "aa55002b0102d3"),
    ],
)
def test_encodes_frames_the_valve_accepted(opcode, payload, expected):
    assert m.encode_frame(opcode, payload).hex() == expected


def test_decodes_a_reply_the_valve_sent():
    frame = m._decode_frame(captures.ACK)
    assert frame is not None
    assert (frame.channel, frame.opcode, frame.payload) == (1, 1, b"\x01")


@pytest.mark.parametrize(
    "opcode",
    [m.Opcode.RUN_PRESET, m.Opcode.GET_PRESET, m.Opcode.WRITE_PRESET],
)
def test_round_trips(opcode):
    payload = bytes(range(10))
    frame = m._decode_frame(m.encode_frame(opcode, payload, channel=3))
    assert frame is not None
    assert (frame.channel, frame.opcode, frame.payload) == (3, opcode, payload)


def test_rejects_a_corrupted_checksum():
    frame = bytearray(m.encode_frame(m.Opcode.RUN_PRESET, b"\x02"))
    frame[-1] ^= 0xFF
    assert m._decode_frame(bytes(frame)) is None


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xaa",
        b"\xaa\x55",
        b"\xaa\x55\x00\xb1\x01",  # header only, payload missing
        b"\x00\x55\x00\xb1\x01\x02\x4c",  # wrong preamble
    ],
    ids=["empty", "one byte", "preamble only", "truncated", "bad preamble"],
)
def test_rejects_malformed_frames(raw):
    assert m._decode_frame(raw) is None


def test_rejects_a_length_that_disagrees_with_the_frame():
    frame = bytearray(m.encode_frame(m.Opcode.RUN_PRESET, b"\x02"))
    frame[4] = 9  # claim nine payload bytes where there is one
    assert m._decode_frame(bytes(frame)) is None


def test_encoding_rejects_out_of_range_arguments():
    with pytest.raises(ValueError):
        m.encode_frame(m.Opcode.ACK, b"", channel=256)
    with pytest.raises(ValueError):
        m.encode_frame(m.Opcode.ACK, b"\x00" * 256)


@pytest.mark.parametrize(
    ("celsius", "expected"),
    [(39.0, "0186"), (42.0, "01a4"), (0.0, "0000"), (39.55, "018c")],
)
def test_encodes_temperature_in_tenths(celsius, expected):
    assert m.encode_temperature(celsius).hex() == expected


@pytest.mark.parametrize("celsius", [-1.0, 7000.0])
def test_rejects_impossible_temperatures(celsius):
    with pytest.raises(ValueError):
        m.encode_temperature(celsius)


class TestFrameReader:
    """Notifications are not guaranteed to align with frames."""

    def test_reads_a_frame_delivered_whole(self):
        reader = m._FrameReader()
        assert len(reader.feed(captures.ACK)) == 1

    @pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7])
    def test_reassembles_a_fragmented_frame(self, chunk_size):
        frame = captures.frame(captures.PRESET_BATH, opcode=0x5D)
        reader = m._FrameReader()
        frames = []
        for i in range(0, len(frame), chunk_size):
            frames += reader.feed(frame[i : i + chunk_size])
        assert len(frames) == 1
        assert frames[0].payload == captures.PRESET_BATH

    def test_splits_two_frames_arriving_together(self):
        reader = m._FrameReader()
        frames = reader.feed(captures.ACK + captures.ACK)
        assert [f.payload for f in frames] == [b"\x01", b"\x01"]

    def test_skips_leading_junk(self):
        reader = m._FrameReader()
        frames = reader.feed(b"\x00\x13\x99" + captures.ACK)
        assert len(frames) == 1

    def test_survives_a_preamble_split_across_chunks(self):
        reader = m._FrameReader()
        assert reader.feed(captures.ACK[:1]) == []
        frames = reader.feed(captures.ACK[1:])
        assert len(frames) == 1

    def test_discards_a_frame_that_fails_its_checksum(self):
        corrupt = bytearray(captures.ACK)
        corrupt[-1] ^= 0xFF
        reader = m._FrameReader()
        assert reader.feed(bytes(corrupt)) == []

    def test_recovers_after_a_corrupt_frame(self):
        corrupt = bytearray(captures.ACK)
        corrupt[-1] ^= 0xFF
        reader = m._FrameReader()
        reader.feed(bytes(corrupt))
        assert len(reader.feed(captures.ACK)) == 1

    def test_reset_drops_buffered_bytes(self):
        reader = m._FrameReader()
        reader.feed(captures.ACK[:3])
        reader.reset()
        assert reader.feed(captures.ACK[3:]) == []

    def test_does_not_grow_without_bound_on_junk(self):
        reader = m._FrameReader()
        for _ in range(100):
            assert reader.feed(b"\x11\x22\x33\x44") == []
        assert len(reader._buffer) <= 4
