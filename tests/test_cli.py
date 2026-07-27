"""The command line: argument parsing and whole-command runs."""

import argparse

import pytest

import miramode as m
from miramode import cli

from . import captures
from .conftest import preset_responder

ADDRESS = ["-a", "fake-address"]


class TestArgumentTypes:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("39", 39.0), ("40.5", 40.5), ("0.1", 0.1), ("99.9", 99.9)],
    )
    def test_accepts_temperatures(self, text, expected):
        assert cli._temperature(text) == expected

    @pytest.mark.parametrize("text", ["", "warm", "0", "100", "-5", "1e999"])
    def test_rejects_temperatures(self, text):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._temperature(text)

    @pytest.mark.parametrize(("text", "expected"), [("0", 0), ("100", 100)])
    def test_accepts_percentages(self, text, expected):
        assert cli._percentage(text) == expected

    @pytest.mark.parametrize("text", ["101", "-1", "lots"])
    def test_rejects_percentages(self, text):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._percentage(text)

    @pytest.mark.parametrize(("text", "expected"), [("0", 0), ("15", 15)])
    def test_accepts_preset_slots(self, text, expected):
        assert cli._preset_index(text) == expected

    @pytest.mark.parametrize("text", ["16", "-1", "first"])
    def test_rejects_preset_slots(self, text):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._preset_index(text)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1800", 1800),
            ("30:00", 1800),  # the form presets are displayed in
            ("0:45", 45),
            ("45", 45),
            ("1:01", 61),
        ],
    )
    def test_accepts_durations_either_way(self, text, expected):
        assert cli._duration(text) == expected

    @pytest.mark.parametrize(
        "text", ["0", "0:00", "-5", "soon", "10:xx", "", "70000"]
    )
    def test_rejects_durations(self, text):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._duration(text)

    @pytest.mark.parametrize("text", ["0", "-1", "70000", "a bathful"])
    def test_rejects_volumes(self, text):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._litres(text)


class TestStatusCommand:
    def test_reports_a_running_valve(self, valve, capsys):
        valve.responder = lambda _frame: captures.STATE_RUNNING
        assert cli.main(["status", *ADDRESS]) == 0
        out = capsys.readouterr().out
        assert "yes, outlet 1" in out
        assert "42.0C" in out
        assert "50%" in out

    def test_hides_target_and_flow_when_idle(self, valve, capsys):
        # Idle, the valve reports no target and a resting flow, so
        # printing them would be misleading.
        valve.responder = lambda _frame: captures.STATE_IDLE
        assert cli.main(["status", *ADDRESS]) == 0
        out = capsys.readouterr().out
        assert "Running:      no" in out
        assert "Target" not in out
        assert "Flow" not in out

    def test_shows_the_raw_reply_when_asked(self, valve, capsys):
        valve.responder = lambda _frame: captures.STATE_IDLE
        assert cli.main(["-v", "status", *ADDRESS]) == 0
        assert captures.STATE_IDLE.hex(" ") in capsys.readouterr().out


class TestPresetsCommand:
    slots = {1: captures.PRESET_SHOWER, 2: captures.PRESET_BATH}

    def test_lists_presets_with_their_settings(self, valve, capsys):
        valve.responder = preset_responder(self.slots)
        assert cli.main(["presets", *ADDRESS, "--end", "3"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].split() == [
            "1",
            "Default",
            "38.0C",
            "outlet",
            "2",
            "30:00",
            "flow",
            "88%",
        ]
        assert lines[1].split() == [
            "2",
            "Default",
            "Bathfill",
            "42.0C",
            "outlet",
            "1",
            "50",
            "litres",
            "flow",
            "88%",
        ]

    def test_says_so_when_there_are_none(self, valve, capsys):
        valve.responder = preset_responder({})
        assert cli.main(["presets", *ADDRESS, "--end", "2"]) == 1
        assert "No presets configured" in capsys.readouterr().out


class TestSavePreset:
    def test_writes_and_reports_what_was_stored(self, valve, capsys):
        written: dict[int, bytes] = {}

        def respond(frame):
            if frame.opcode == m.Opcode.WRITE_PRESET:
                written[frame.payload[0]] = frame.payload
                return b"\x01"
            if frame.opcode == m.Opcode.GET_PRESET:
                return written.get(frame.payload[0], b"\x00" * 61)
            return b"\x01"

        valve.responder = respond
        code = cli.main(
            [
                "save-preset",
                *ADDRESS,
                "3",
                "--name",
                "Evening Bath",
                "--temperature",
                "40.5",
                "--volume",
                "65",
                "--first",
                "--flow",
                "72",
            ]
        )
        assert code == 0
        assert "Saved preset 3: Evening Bath" in capsys.readouterr().out
        stored = m._decode_preset(written[3])
        assert stored is not None
        assert (stored.temperature, stored.volume, stored.flow) == (
            40.5,
            65,
            72,
        )
        assert stored.outlets == m.Outlet.FIRST

    def test_requires_an_outlet(self, valve, capsys):
        code = cli.main(
            [
                "save-preset",
                *ADDRESS,
                "3",
                "--name",
                "Nope",
                "--temperature",
                "40",
                "--volume",
                "60",
            ]
        )
        assert code == 2
        assert "choose an outlet" in capsys.readouterr().err
        assert valve.requests == [], "nothing should have been written"

    def test_requires_a_duration_or_a_volume(self, valve):
        with pytest.raises(SystemExit):
            cli.main(
                [
                    "save-preset",
                    *ADDRESS,
                    "3",
                    "--name",
                    "Nope",
                    "--temperature",
                    "40",
                    "--first",
                ]
            )

    def test_refuses_both_a_duration_and_a_volume(self, valve):
        with pytest.raises(SystemExit):
            cli.main(
                [
                    "save-preset",
                    *ADDRESS,
                    "3",
                    "--name",
                    "Nope",
                    "--temperature",
                    "40",
                    "--first",
                    "--volume",
                    "60",
                    "--duration",
                    "600",
                ]
            )

    def test_warns_if_the_slot_reads_back_empty(self, valve, capsys):
        # Acknowledged but not actually stored.
        valve.responder = lambda frame: (
            b"\x00" * 61 if frame.opcode == m.Opcode.GET_PRESET else b"\x01"
        )
        code = cli.main(
            [
                "save-preset",
                *ADDRESS,
                "4",
                "--name",
                "Ghost",
                "--temperature",
                "40",
                "--duration",
                "10:00",
                "--second",
            ]
        )
        assert code == 1
        assert "reads back as empty" in capsys.readouterr().err


class TestDeletePreset:
    def test_deletes_and_names_what_it_removed(self, valve, capsys):
        slots = {2: captures.PRESET_BATH}

        def respond(frame):
            if frame.opcode == m.Opcode.WRITE_PRESET:
                slots.pop(frame.payload[0], None)
                return b"\x01"
            if frame.opcode == m.Opcode.GET_PRESET:
                index = frame.payload[0]
                return slots.get(index, bytes([index]) + b"\x00" * 60)
            return b"\x01"

        valve.responder = respond
        assert cli.main(["delete-preset", *ADDRESS, "2"]) == 0
        assert "Deleted preset 2: Default Bathfill" in capsys.readouterr().out
        assert slots == {}

    def test_says_nothing_to_do_for_an_empty_slot(self, valve, capsys):
        valve.responder = preset_responder({})
        assert cli.main(["delete-preset", *ADDRESS, "5"]) == 0
        assert "already empty" in capsys.readouterr().out
        assert all(
            r.opcode != m.Opcode.WRITE_PRESET for r in valve.requests
        ), "an empty slot needs no write"


class TestOutletCommands:
    def test_runs_the_outlets_asked_for(self, valve, capsys):
        code = cli.main(
            ["outlets", *ADDRESS, "--first", "--temperature", "39"]
        )
        assert code == 0
        assert valve.last_request.payload == b"\x01\x86\x64\x01"
        assert "Running outlet(s) 1 at 39.0C" in capsys.readouterr().out

    def test_stops_everything(self, valve, capsys):
        assert cli.main(["stop", *ADDRESS]) == 0
        assert valve.last_request.payload == b"\x00\x00\x00\x00"
        assert "All outlets off" in capsys.readouterr().out

    def test_starts_a_preset(self, valve, capsys):
        assert cli.main(["start", *ADDRESS, "2"]) == 0
        assert valve.last_request.payload == b"\x02"
        assert "Started preset 2" in capsys.readouterr().out


class TestInfoCommand:
    def test_reports_identification_and_protocol(self, valve, capsys):
        valve.responder = lambda frame: {
            m.Opcode.GET_NAME: captures.NAME,
            m.Opcode.GET_SERIAL: captures.SERIAL,
            m.Opcode.GET_MANUFACTURED: captures.MANUFACTURED,
        }.get(m.Opcode(frame.opcode), b"\x01")
        assert cli.main(["info", *ADDRESS]) == 0
        out = capsys.readouterr().out
        assert "Bathroom" in out
        assert "3140090125121946" in out
        assert "09 Jan 2025, 12:19" in out
        assert "GCS (supported)" in out

    def test_reports_an_unsupported_valve(self, valve, capsys):
        valve._services = {"0000180a-0000-1000-8000-00805f9b34fb": []}
        valve.responder = lambda _frame: None
        assert cli.main(["info", *ADDRESS]) == 1
        assert "not GCS" in capsys.readouterr().out


class TestFailureHandling:
    def test_reports_a_rejected_command(self, valve, capsys):
        valve.responder = lambda _frame: bytes([m.Status.ERROR])
        assert cli.main(["start", *ADDRESS, "1"]) == 1
        assert "error:" in capsys.readouterr().err

    def test_reports_a_silent_valve(self, valve, capsys):
        valve.responder = lambda _frame: None
        assert cli.main(["stop", *ADDRESS]) == 1
        assert "error:" in capsys.readouterr().err

    def test_needs_a_subcommand(self):
        with pytest.raises(SystemExit):
            cli.main([])

    def test_needs_an_address(self):
        with pytest.raises(SystemExit):
            cli.main(["status"])


class TestScanCommand:
    def test_lists_what_it_found(self, monkeypatch, capsys):
        async def fake_discover(timeout=0.0):
            return [m.DiscoveredDevice("AA:1", "Mira 004C Bathroom")]

        monkeypatch.setattr(cli, "discover", fake_discover)
        assert cli.main(["scan"]) == 0
        assert "AA:1  Mira 004C Bathroom" in capsys.readouterr().out

    def test_says_so_when_nothing_is_there(self, monkeypatch, capsys):
        async def fake_discover(timeout=0.0):
            return []

        monkeypatch.setattr(cli, "discover", fake_discover)
        assert cli.main(["scan"]) == 1
        assert "No valves found" in capsys.readouterr().out
