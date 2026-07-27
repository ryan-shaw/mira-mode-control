"""Encoding presets for writing back to the valve.

The load-bearing test here is the round trip: a preset read off real
hardware must re-encode to exactly the bytes the valve sent. That checks
the encoder against the device's own representation rather than against
our reading of it.
"""

import dataclasses

import pytest

import miramode as m

from . import captures


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        (captures.PRESET_SHOWER, "the factory shower preset"),
        (captures.PRESET_BATH, "the factory bath preset"),
    ],
)
def test_re_encodes_a_real_preset_byte_for_byte(raw, name):
    preset = m._decode_preset(raw)
    assert preset is not None, name
    assert m.encode_preset(preset) == raw


def test_encodes_to_the_length_the_valve_expects():
    preset = m._decode_preset(captures.PRESET_BATH)
    assert preset is not None
    assert len(m.encode_preset(preset)) == 61


def test_a_written_preset_decodes_back_to_itself():
    wanted = m.Preset(
        index=3,
        name="Evening Bath",
        outlets=m.Outlet.FIRST,
        temperature=40.5,
        volume=65,
        flow=72,
    )
    got = m._decode_preset(m.encode_preset(wanted))
    assert got is not None
    for field in ("index", "name", "outlets", "temperature", "volume", "flow"):
        assert getattr(got, field) == getattr(wanted, field), field
    assert got.duration is None


def test_keeps_a_fractional_temperature():
    # The valve stores tenths even though the vendor app rounds them off.
    preset = m.Preset(
        index=4,
        name="Warm",
        outlets=m.Outlet.SECOND,
        temperature=39.5,
        duration=600,
    )
    got = m._decode_preset(m.encode_preset(preset))
    assert got is not None
    assert got.temperature == 39.5


def test_encodes_every_outlet():
    preset = m.Preset(
        index=5,
        name="Both",
        outlets=m.Outlet.FIRST | m.Outlet.SECOND,
        temperature=40.0,
        duration=300,
    )
    got = m._decode_preset(m.encode_preset(preset))
    assert got is not None
    assert got.outlets == (m.Outlet.FIRST | m.Outlet.SECOND)


def test_puts_the_outlets_two_bits_higher_than_a_live_command():
    # The preset flags byte uses bits 2-4, while SET_OUTLETS uses 0-2.
    preset = m.Preset(
        index=1,
        name="x",
        outlets=m.Outlet.FIRST,
        temperature=40.0,
        duration=60,
    )
    assert m.encode_preset(preset)[37] & 0b11100 == m.Outlet.FIRST << 2


def test_a_full_length_name_survives():
    name = "x" * 31
    preset = m.Preset(
        index=1,
        name=name,
        outlets=m.Outlet.FIRST,
        temperature=40.0,
        duration=60,
    )
    got = m._decode_preset(m.encode_preset(preset))
    assert got is not None
    assert got.name == name


#: A preset that encodes cleanly, for tests that spoil one field of it.
VALID = m.Preset(
    index=3,
    name="Test",
    outlets=m.Outlet.FIRST,
    temperature=40.0,
    volume=60,
    flow=88,
)


class TestValidation:
    """A write replaces a whole slot, so bad input must not reach it."""

    def test_accepts_the_baseline(self):
        assert m.encode_preset(VALID)

    @pytest.mark.parametrize(
        ("changes", "why"),
        [
            ({"duration": 600}, "a duration as well as a volume"),
            ({"volume": None}, "neither a duration nor a volume"),
            ({"outlets": m.Outlet.NONE}, "no outlet"),
            ({"temperature": None}, "no temperature"),
            ({"name": "x" * 32}, "a name past the field width"),
            ({"index": 99}, "a slot the valve has no room for"),
            ({"index": -1}, "a negative slot"),
            ({"flow": 101}, "more flow than exists"),
            ({"flow": -1}, "negative flow"),
            ({"volume": 70000}, "a volume past two bytes"),
            ({"temperature": 110.0}, "a temperature needing a third bit"),
            (
                {"volume": None, "duration": 70000},
                "a duration past two bytes",
            ),
        ],
    )
    def test_refuses(self, changes, why):
        with pytest.raises(ValueError):
            m.encode_preset(dataclasses.replace(VALID, **changes))

    def test_refuses_a_name_it_cannot_encode(self):
        preset = dataclasses.replace(VALID, name="Bath—fill")
        with pytest.raises(UnicodeEncodeError):
            m.encode_preset(preset)

    def test_the_highest_safe_temperature_is_allowed(self):
        # 102.3C is the most the two spare bits can carry. It is absurd
        # for a shower but it is the encoding's real limit.
        assert m.encode_preset(dataclasses.replace(VALID, temperature=102.3))


class TestClearing:
    def test_sends_the_slot_and_nothing_else(self):
        cleared = m._encode_empty_preset(3)
        assert cleared[0] == 3
        assert set(cleared[1:]) == {0}
        assert len(cleared) == 61

    def test_a_cleared_slot_reads_as_unconfigured(self):
        assert m._decode_preset(m._encode_empty_preset(3)) is None

    @pytest.mark.parametrize("index", [-1, 16, 99])
    def test_refuses_a_slot_out_of_range(self, index):
        with pytest.raises(ValueError):
            m._encode_empty_preset(index)


def test_editing_a_preset_changes_only_what_was_asked():
    original = m._decode_preset(captures.PRESET_BATH)
    assert original is not None
    edited = dataclasses.replace(original, temperature=41.0)
    got = m._decode_preset(m.encode_preset(edited))
    assert got is not None
    assert got.temperature == 41.0
    assert got.name == original.name
    assert got.volume == original.volume
    assert got.outlets == original.outlets
    assert got.flow == original.flow
