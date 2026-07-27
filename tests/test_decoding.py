"""Decoding replies: state, presets and identification."""

from datetime import datetime

import pytest

import miramode as m

from . import captures


class TestState:
    def test_reads_an_idle_valve(self):
        state = m._decode_state(captures.STATE_IDLE)
        assert state is not None
        assert state.outlets == m.Outlet.NONE
        assert not state.is_running
        assert state.target_temperature is None
        assert state.temperature == 25.6

    def test_reads_the_values_that_were_commanded(self):
        # This run was commanded as outlet 1, 42.0C, flow 50.
        state = m._decode_state(captures.STATE_RUNNING)
        assert state is not None
        assert state.outlets == m.Outlet.FIRST
        assert state.is_running
        assert state.target_temperature == 42.0
        assert state.flow == 50

    def test_follows_the_water_warming_up(self):
        cool = m._decode_state(captures.STATE_RUNNING)
        warm = m._decode_state(captures.STATE_RUNNING_WARMER)
        assert cool is not None and warm is not None
        assert cool.temperature == 24.6
        assert warm.temperature == 26.0
        # The target is what was asked for, and does not move.
        assert cool.target_temperature == warm.target_temperature == 42.0

    def test_reads_a_valve_just_stopped(self):
        state = m._decode_state(captures.STATE_STOPPED)
        assert state is not None
        assert not state.is_running
        assert state.target_temperature is None

    def test_keeps_the_undecoded_payload(self):
        state = m._decode_state(captures.STATE_IDLE)
        assert state is not None
        assert state.raw == captures.STATE_IDLE

    def test_rejects_a_short_reply(self):
        assert m._decode_state(captures.STATE_IDLE[:8]) is None

    def test_reports_every_outlet_in_the_bitfield(self):
        raw = bytearray(captures.STATE_RUNNING)
        raw[13] = 0b111
        state = m._decode_state(bytes(raw))
        assert state is not None
        assert state.outlets == (
            m.Outlet.FIRST | m.Outlet.SECOND | m.Outlet.THIRD
        )

    def test_ignores_bits_it_does_not_understand(self):
        # Bit 6 means paused, which is not decoded; it must not leak into
        # the outlet set.
        raw = bytearray(captures.STATE_RUNNING)
        raw[13] = 0b1000001
        state = m._decode_state(bytes(raw))
        assert state is not None
        assert state.outlets == m.Outlet.FIRST


class TestStateComparison:
    """`watch` must not report a change on every poll."""

    def test_ignores_bytes_that_move_on_their_own(self):
        # These two differ only in undecoded bytes 5 and 16.
        a = m._decode_state(captures.STATE_RUNNING)
        b = bytearray(captures.STATE_RUNNING)
        b[5], b[16] = 0x33, 0x33
        assert a is not None
        other = m._decode_state(bytes(b))
        assert other is not None
        assert not m._differs(a, other)

    def test_notices_the_temperature_changing(self):
        a = m._decode_state(captures.STATE_RUNNING)
        b = m._decode_state(captures.STATE_RUNNING_WARMER)
        assert a is not None and b is not None
        assert m._differs(a, b)

    def test_notices_an_outlet_stopping(self):
        a = m._decode_state(captures.STATE_RUNNING)
        b = m._decode_state(captures.STATE_IDLE)
        assert a is not None and b is not None
        assert m._differs(a, b)


class TestPreset:
    def test_reads_the_factory_shower_preset(self):
        preset = m._decode_preset(captures.PRESET_SHOWER)
        assert preset is not None
        assert preset.index == 1
        assert preset.name == "Default"
        assert preset.temperature == 38.0
        assert preset.outlets == m.Outlet.SECOND
        assert preset.duration == 1800
        assert preset.volume is None
        assert preset.flow == 88

    def test_reads_the_factory_bath_preset(self):
        preset = m._decode_preset(captures.PRESET_BATH)
        assert preset is not None
        assert preset.index == 2
        assert preset.name == "Default Bathfill"
        assert preset.temperature == 42.0
        assert preset.outlets == m.Outlet.FIRST
        assert preset.volume == 50
        assert preset.duration is None

    def test_a_preset_sets_a_duration_or_a_volume_not_both(self):
        shower = m._decode_preset(captures.PRESET_SHOWER)
        bath = m._decode_preset(captures.PRESET_BATH)
        assert shower is not None and bath is not None
        for preset in (shower, bath):
            assert (preset.duration is None) != (preset.volume is None)

    def test_ignores_an_unconfigured_slot(self):
        assert m._decode_preset(captures.PRESET_EMPTY) is None

    def test_ignores_an_invalid_slot(self):
        assert m._decode_preset(captures.PRESET_INVALID) is None

    def test_rejects_a_reply_too_short_to_hold_a_name(self):
        assert m._decode_preset(b"\x01\x02") is None

    def test_reads_a_name_longer_than_sixteen_characters(self):
        # The field is 31 bytes. Both factory names are shorter, so only
        # a made-up one exercises the full width.
        name = "Long Evening Bath Fill Preset!!"
        assert len(name) == 31
        raw = bytearray(captures.PRESET_BATH)
        raw[1:32] = name.encode("ascii")
        preset = m._decode_preset(bytes(raw))
        assert preset is not None
        assert preset.name == name

    def test_reads_only_the_layout_it_recognises(self):
        # A reply that is not the 61 byte layout yields the name and
        # nothing invented. Its name field is narrower, 15 bytes, so a
        # 16 character name comes back a character short - which is why
        # the fields below must stay None rather than being guessed from
        # offsets that belong to the layout we do understand.
        preset = m._decode_preset(captures.PRESET_BATH[:20])
        assert preset is not None
        assert preset.name == "Default Bathfil"
        assert preset.temperature is None
        assert preset.outlets == m.Outlet.NONE
        assert preset.flow is None
        assert preset.duration is None
        assert preset.volume is None


class TestIdentification:
    def test_reads_the_valve_name(self):
        assert m._decode_text(captures.NAME) == "Bathroom"

    def test_reads_the_serial_number(self):
        assert m._decode_text(captures.SERIAL) == "3140090125121946"

    def test_reads_the_manufacturing_timestamp(self):
        # The serial number embeds 09 01 25 12 19, agreeing with this.
        assert m._decode_manufactured(captures.MANUFACTURED) == datetime(
            2025, 1, 9, 12, 19
        )

    @pytest.mark.parametrize(
        ("raw", "why"),
        [
            (b"\x19\x09", "too short"),
            (bytes([25, 9, 0x02, 0xE3]), "month zero"),
            (bytes([25, 9, 0xD2, 0xE3]), "month thirteen"),
            (bytes([25, 0, 0x12, 0xE3]), "day zero"),
            (bytes([25, 99, 0x12, 0xE3]), "day ninety-nine"),
            (bytes([25, 9, 0x1F, 0xFF]), "more minutes than a day"),
            (bytes([25, 31, 0x22, 0xE3]), "31 February"),
        ],
    )
    def test_rejects_an_implausible_timestamp(self, raw, why):
        assert m._decode_manufactured(raw) is None, why

    @pytest.mark.parametrize(
        "raw",
        [
            b"\x00" * 16,
            b"\x03\x9f\x00\xe2",
            captures.PRESET_INVALID[1:17],
        ],
        ids=["all zero", "binary junk", "an invalid slot's bytes"],
    )
    def test_rejects_text_that_is_not_text(self, raw):
        assert m._decode_text(raw) is None

    def test_strips_padding_and_whitespace(self):
        assert m._decode_text(b"Bathroom   \x00\x00") == "Bathroom"
