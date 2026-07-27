"""Shower's behaviour against a stand-in valve."""

import asyncio

import pytest

import miramode as m

from . import captures
from .conftest import FakeValve, preset_responder


class TestConnection:
    async def test_connects_and_subscribes(self, shower, valve):
        async with shower:
            assert valve.connected
            assert valve.notifying
            assert shower.is_connected

    async def test_unsubscribes_before_dropping_the_connection(
        self, shower, valve
    ):
        async with shower:
            pass
        # Leaving notifications running lets the native callback fire into
        # a dead interpreter, which used to crash the process on exit.
        assert not valve.notifying
        assert valve.disconnect_calls == 1

    async def test_disconnecting_twice_is_harmless(self, shower, valve):
        await shower.connect()
        await shower.disconnect()
        await shower.disconnect()
        assert valve.disconnect_calls == 1

    async def test_connecting_twice_does_not_reconnect(self, shower, valve):
        await shower.connect()
        await shower.connect()
        try:
            assert valve.disconnect_calls == 0
        finally:
            await shower.disconnect()

    @pytest.mark.parametrize(
        "call",
        [
            lambda s: s.status(),
            lambda s: s.stop(),
            lambda s: s.read_preset(1),
            lambda s: s.set_outlets(m.Outlet.FIRST, 39.0),
            lambda s: s.run_preset(1),
            lambda s: s.device_info(),
        ],
    )
    async def test_commands_need_a_connection(self, shower, call):
        with pytest.raises(m.NotConnected):
            await call(shower)

    async def test_says_it_is_not_connected_before_connecting(self, shower):
        assert not shower.is_connected


class TestCommands:
    async def test_sends_the_frame_that_ran_the_bath(self, shower, valve):
        async with shower:
            await shower.set_outlets(m.Outlet.FIRST, 39.0, 100)
        assert valve.last_request.opcode == m.Opcode.SET_OUTLETS
        assert valve.last_request.payload == b"\x01\x86\x64\x01"

    async def test_stopping_leaves_the_temperature_alone(self, shower, valve):
        async with shower:
            await shower.stop()
        assert valve.last_request.payload == b"\x00\x00\x00\x00"

    async def test_zeroes_the_flow_when_nothing_runs(self, shower, valve):
        async with shower:
            await shower.set_outlets(m.Outlet.NONE, 39.0, 100)
        assert valve.last_request.payload[2] == 0

    async def test_runs_a_preset_by_slot(self, shower, valve):
        async with shower:
            await shower.run_preset(2)
        assert valve.last_request.opcode == m.Opcode.RUN_PRESET
        assert valve.last_request.payload == b"\x02"

    async def test_refuses_an_impossible_flow(self, shower):
        async with shower:
            with pytest.raises(ValueError):
                await shower.set_outlets(m.Outlet.FIRST, 39.0, 101)

    async def test_raises_when_the_valve_rejects_a_command(self, valve):
        valve.responder = lambda _frame: bytes([m.Status.ERROR])
        async with m.Shower("fake", response_timeout=0.1) as shower:
            with pytest.raises(m.CommandFailed):
                await shower.run_preset(1)

    async def test_raises_on_a_reply_it_cannot_interpret(self, valve):
        valve.responder = lambda _frame: b"\x42"
        async with m.Shower("fake", response_timeout=0.1) as shower:
            with pytest.raises(m.CommandFailed):
                await shower.run_preset(1)

    async def test_times_out_when_the_valve_stays_silent(self, valve):
        valve.responder = lambda _frame: None
        async with m.Shower("fake", response_timeout=0.05) as shower:
            with pytest.raises(m.ResponseTimeout):
                await shower.run_preset(1)

    async def test_recovers_after_a_timeout(self, valve):
        replies = [None, b"\x01"]
        valve.responder = lambda _frame: replies.pop(0)
        async with m.Shower("fake", response_timeout=0.05) as shower:
            with pytest.raises(m.ResponseTimeout):
                await shower.run_preset(1)
            await shower.run_preset(2)  # must not raise


class TestStatus:
    async def test_asks_the_way_the_app_does(self, valve):
        valve.responder = lambda _frame: captures.STATE_RUNNING
        async with m.Shower("fake", response_timeout=0.1) as shower:
            await shower.status()
        assert valve.last_request.opcode == m.Opcode.GET_STATE
        assert valve.last_request.payload == b"\x02"

    async def test_reports_what_the_valve_is_doing(self, valve):
        valve.responder = lambda _frame: captures.STATE_RUNNING
        async with m.Shower("fake", response_timeout=0.1) as shower:
            state = await shower.status()
        assert state.outlets == m.Outlet.FIRST
        assert state.target_temperature == 42.0

    async def test_raises_on_a_reply_too_short_to_decode(self, valve):
        valve.responder = lambda _frame: b"\x01\x02\x03"
        async with m.Shower("fake", response_timeout=0.1) as shower:
            with pytest.raises(m.CommandFailed):
                await shower.status()

    async def test_reads_a_reply_split_across_notifications(self, monkeypatch):
        fake = FakeValve(lambda _frame: captures.STATE_RUNNING, chunk_size=4)
        monkeypatch.setattr(m, "BleakClient", fake)
        async with m.Shower("fake", response_timeout=0.5) as shower:
            state = await shower.status()
        assert state.target_temperature == 42.0


class TestWatch:
    async def _collect(self, shower, count, **kwargs):
        seen = []

        async def gather():
            async for state in shower.watch(interval=0, **kwargs):
                seen.append(state)
                if len(seen) == count:
                    return

        # Bounded deliberately: a watch that stops reporting changes
        # should fail this test, not hang it.
        await asyncio.wait_for(gather(), timeout=2)
        return seen

    async def test_yields_the_first_reading(self, valve):
        valve.responder = lambda _frame: captures.STATE_IDLE
        async with m.Shower("fake", response_timeout=0.1) as shower:
            seen = await self._collect(shower, 1)
        assert len(seen) == 1
        assert not seen[0].is_running

    async def test_yields_again_only_when_something_changes(self, valve):
        replies = [
            captures.STATE_IDLE,
            captures.STATE_IDLE,  # suppressed
            captures.STATE_RUNNING,
            captures.STATE_RUNNING_WARMER,
        ]
        valve.responder = lambda _frame: (
            replies.pop(0) if replies else (captures.STATE_RUNNING_WARMER)
        )
        async with m.Shower("fake", response_timeout=0.1) as shower:
            seen = await self._collect(shower, 3)
        assert [s.is_running for s in seen] == [False, True, True]
        assert [s.temperature for s in seen] == [25.6, 24.6, 26.0]

    async def test_can_report_every_reading(self, valve):
        valve.responder = lambda _frame: captures.STATE_IDLE
        async with m.Shower("fake", response_timeout=0.1) as shower:
            seen = await self._collect(shower, 3, changes_only=False)
        assert len(seen) == 3


class TestPresets:
    slots = {1: captures.PRESET_SHOWER, 2: captures.PRESET_BATH}

    async def test_reads_one_slot(self, valve):
        valve.responder = preset_responder(self.slots)
        async with m.Shower("fake", response_timeout=0.1) as shower:
            preset = await shower.read_preset(2)
        assert preset is not None
        assert preset.name == "Default Bathfill"

    async def test_ignores_a_slot_that_is_not_configured(self, valve):
        valve.responder = preset_responder(self.slots)
        async with m.Shower("fake", response_timeout=0.1) as shower:
            assert await shower.read_preset(7) is None

    async def test_ignores_a_reply_about_a_different_slot(self, valve):
        # Asking for 4 but being told about 2 must not be believed.
        valve.responder = lambda _frame: captures.PRESET_BATH
        async with m.Shower("fake", response_timeout=0.1) as shower:
            assert await shower.read_preset(4) is None

    async def test_lists_only_configured_slots(self, valve):
        valve.responder = preset_responder(self.slots)
        async with m.Shower("fake", response_timeout=0.1) as shower:
            presets = await shower.presets(range(0, 5))
        assert [(p.index, p.name) for p in presets] == [
            (1, "Default"),
            (2, "Default Bathfill"),
        ]

    async def test_writes_a_preset(self, valve):
        preset = m.Preset(
            index=3,
            name="Evening Bath",
            outlets=m.Outlet.FIRST,
            temperature=40.5,
            volume=65,
            flow=72,
        )
        async with m.Shower("fake", response_timeout=0.1) as shower:
            await shower.write_preset(preset)
        assert valve.last_request.opcode == m.Opcode.WRITE_PRESET
        # What went out must read back as what was asked for.
        stored = m._decode_preset(valve.last_request.payload)
        assert stored is not None
        assert stored.name == "Evening Bath"
        assert stored.temperature == 40.5
        assert stored.volume == 65

    async def test_refuses_to_write_an_incomplete_preset(self, valve):
        async with m.Shower("fake", response_timeout=0.1) as shower:
            with pytest.raises(ValueError):
                await shower.write_preset(m.Preset(index=3, name="Nope"))
        assert valve.requests == []

    async def test_clears_a_slot(self, valve):
        async with m.Shower("fake", response_timeout=0.1) as shower:
            await shower.delete_preset(3)
        assert valve.last_request.opcode == m.Opcode.WRITE_PRESET
        assert valve.last_request.payload[0] == 3
        assert set(valve.last_request.payload[1:]) == {0}


class TestDeviceInfo:
    def _responder(self, frame: m.Frame) -> bytes | None:
        return {
            m.Opcode.GET_NAME: captures.NAME,
            m.Opcode.GET_SERIAL: captures.SERIAL,
            m.Opcode.GET_MANUFACTURED: captures.MANUFACTURED,
        }.get(m.Opcode(frame.opcode), b"\x01")

    async def test_reads_identification_over_the_protocol(self, valve):
        valve.responder = self._responder
        async with m.Shower("fake", response_timeout=0.1) as shower:
            info = await shower.device_info()
        assert info.name == "Bathroom"
        assert info.serial_number == "3140090125121946"
        assert info.manufactured is not None
        assert info.manufactured.year == 2025

    async def test_copes_with_a_valve_that_answers_nothing(self, valve):
        valve.responder = lambda _frame: None
        async with m.Shower("fake", response_timeout=0.05) as shower:
            info = await shower.device_info()
        assert info.name is None
        assert info.serial_number is None
        assert info.manufactured is None

    async def test_lists_the_services_it_found(self, shower):
        async with shower:
            services = shower.services()
        assert m.SERVICE_UUID in services
        assert m.COMMAND_CHAR_UUID in services[m.SERVICE_UUID]

    async def test_does_not_invent_gatt_fields(self, valve):
        # This hardware has no device-information service, so these stay
        # empty rather than being filled in from somewhere else.
        valve.responder = self._responder
        async with m.Shower("fake", response_timeout=0.1) as shower:
            info = await shower.device_info()
        assert info.manufacturer is None
        assert info.model is None


class TestNotificationHandling:
    async def test_ignores_a_reply_nobody_asked_for(self, shower, valve):
        async with shower:
            valve.notify(captures.ACK)  # must not raise
            await shower.run_preset(1)

    async def test_ignores_a_corrupt_reply(self, valve):
        corrupt = bytearray(captures.frame(b"\x01"))
        corrupt[-1] ^= 0xFF
        valve.responder = lambda _frame: None
        async with m.Shower("fake", response_timeout=0.05) as shower:
            valve.notify(bytes(corrupt))
            with pytest.raises(m.ResponseTimeout):
                await shower.run_preset(1)

    async def test_disconnecting_fails_a_command_in_flight(self, valve):
        valve.responder = lambda _frame: None
        shower = m.Shower("fake", response_timeout=5)
        await shower.connect()
        pending = asyncio.ensure_future(shower.run_preset(1))
        await asyncio.sleep(0)
        await shower.disconnect()
        with pytest.raises(m.NotConnected):
            await pending


class TestDiscovery:
    async def test_finds_valves_by_service_and_by_name(self, monkeypatch):
        class Advert:
            def __init__(self, name, uuids):
                self.local_name = name
                self.service_uuids = uuids

        class Device:
            def __init__(self, address, name):
                self.address = address
                self.name = name

        found = {
            "A": (Device("AA:1", "Mira 004C Bathroom"), Advert(None, [])),
            "B": (Device("BB:2", None), Advert(None, [m.SERVICE_UUID])),
            "C": (Device("CC:3", "Someone's Headphones"), Advert(None, [])),
        }

        async def fake_discover(timeout=0.0, return_adv=False):
            assert return_adv
            return found

        monkeypatch.setattr(m.BleakScanner, "discover", fake_discover)
        devices = await m.discover(timeout=0)
        assert {d.address for d in devices} == {"AA:1", "BB:2"}
