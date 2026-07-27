# mira-mode-control

[![Lint](https://github.com/ryan-shaw/mira-mode-control/actions/workflows/lint.yml/badge.svg)](https://github.com/ryan-shaw/mira-mode-control/actions/workflows/lint.yml)

Control Mira Mode digital showers and bath fillers from Python, over
Bluetooth Low Energy.

Mira Mode valves (now sold under Kohler) ship with no interface other than
BLE and the vendor's phone app, which means no Home Assistant, no
automation, and no way to start filling the bath from anywhere but the
bathroom. This package implements the valve's BLE protocol directly, so
you can drive it from a Raspberry Pi, a laptop, or anything else with a
Bluetooth radio.

> **Use at your own risk.** This drives real plumbing that produces real
> hot water. The protocol was reverse engineered, not documented by the
> vendor. Don't run it unattended until you've watched it behave.

## Supported hardware

This implements the protocol used by current-generation valves, which
advertise the GATT service `267f0001-eb15-43f5-94c3-67d2221188f7`. Check
yours with `miramode info` — it reports whether the valve speaks a
protocol this package understands.

Earlier Mira Mode valves advertise `bccb0001-ca66-11e5-88a4-0002a5d5c51b`
and use an unrelated, CRC-16 based protocol that this package does not
support.

Developed against a dual outlet shower and bath filler unit. Other valves
in the range should work, but only the outlets and presets your unit
actually has will do anything.

## Requirements

- Python 3.10 or newer
- A Bluetooth Low Energy adapter
- On Linux, BlueZ
- The valve must be paired with the host first, through your operating
  system's Bluetooth settings. It refuses commands over an unbonded
  connection, and only accepts a new bond while it is in pairing mode:
  press and hold the button on the front of the controller for five
  seconds until it flashes, or on a dial model use Menu, `Settings`,
  `Connect`. It holds up to ten paired devices at once.

## Installation

```console
pip install miramode
```

Or, with [uv](https://docs.astral.sh/uv/) — `uv tool install miramode`
to get just the `miramode` command, or `uv add miramode` to use it as a
library in a project.

### From source

```console
git clone git@github.com:ryan-shaw/mira-mode-control.git
cd mira-mode-control
uv sync
```

That installs the versions pinned in `uv.lock` into `.venv`. Prefix the
commands below with `uv run` to use it.

### Development

The same checks CI runs:

```console
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

Use `uv run ruff format .` to apply formatting.

The tests need no valve. They run against replies recorded from real
hardware, kept in `tests/captures.py` with a note on where each came
from, and against a stand-in valve that answers written frames. The
sharpest of them re-encodes a preset read off a real valve and requires
the bytes to come back identical, which checks the encoding against the
device's own representation rather than against our reading of it.

## Command line usage

Find your valve:

```console
$ miramode scan
1FE6A4BD-73A5-055E-6463-F4785E21D49D  Mira 004C Bathroom
```

Addresses are MAC addresses on Linux and Windows, and opaque UUIDs on
macOS. Every command below takes one with `-a`.

### Status

To see what the valve is doing right now, without changing anything:

```console
$ miramode status -a <address>
Running:      yes, outlet 1
Temperature:  38.4C
Target:       41.0C
Flow:         75%
```

When nothing is running, only the measured temperature is shown, since
the valve reports no target and a resting flow value while idle. Add
`-v` to see the undecoded reply alongside it.

To follow it instead of taking a single reading — useful while a bath
fills — use `watch`, which prints a line whenever the state changes and
runs until interrupted:

```console
$ miramode watch -a <address>
19:18:26  idle            25.5C
19:18:29  outlet 1        25.5C -> 40.0C  flow 60%
19:18:36  outlet 1        31.2C -> 40.0C  flow 60%
19:18:39  idle            25.5C
```

`--interval` sets how often it re-reads, defaulting to two seconds.

### Presets

Presets are the programmes stored in the valve — each carries its own
target temperature and run time, and they're how the vendor app starts
the shower or fills the bath. Listing them only reads from the valve and
runs no water:

```console
$ miramode presets -a <address>
  1  Default           38.0C  outlet 2      30:00  flow 88%
  2  Default Bathfill  42.0C  outlet 1  50 litres  flow 88%
```

A preset either runs for a time or delivers a volume, so each shows one
or the other. This is also the easiest way to find out which outlet
feeds which fixture on your unit: above, the bath fill runs outlet 1, so
outlet 2 is the shower.

Flow is on the same 0 to 100 scale that `outlets --flow` takes. The
vendor app shows a quarter of this, on a 0 to 25 scale, so a preset
reading 88% here appears as 22 in the app.

Factory-fitted presets are numbered from 1; slot 0 is not used. Start one
by number:

```console
miramode start -a <address> 2
```

To create your own, or replace an existing one:

```console
miramode save-preset -a <address> 3 --name "Evening Bath" \
    --temperature 40.5 --volume 65 --first --flow 72
```

A preset either runs for a time or delivers a volume, so give exactly one
of `--volume` (litres) or `--duration` (seconds, or `mm:ss`). Pick the
outlets with `--first` and `--second` as for `outlets`. The slot is read
back afterwards and the stored preset reported, rather than trusting the
valve's acknowledgement.

`save-preset` writes every field, so it replaces a preset rather than
amending it — there is no partial update. Slots occupied by the presets
your valve shipped with are fair game, but they are not recoverable
afterwards, so note down what `presets` reports before overwriting one.

```console
miramode delete-preset -a <address> 3
```

### Outlets

To run an outlet directly, at a temperature you choose:

```console
miramode outlets -a <address> --first --temperature 39
```

Use `--second` for the second outlet, and both flags together to run both
at once. A command states the complete desired state, so any outlet you
don't name is switched off. `--flow` sets the flow rate as a percentage,
defaulting to 100.

Which physical fixture is "first" depends on how your unit is plumbed;
`presets` will tell you, since each preset reports the outlet it uses.

To shut everything off, including a running preset:

```console
miramode stop -a <address>
```

### Troubleshooting

`-v` logs every frame exchanged with the valve:

```console
$ miramode -v stop -a <address>
Connected to 1FE6A4BD-73A5-055E-6463-F4785E21D49D
Sending aa 55 00 ab 04 00 00 00 00 52
Received channel=1 opcode=0x01 payload=01
```

## A physical button

`firmware/xiao-button` is firmware for a Seeed Studio XIAO ESP32C3 that
starts a preset when a button is pressed, and sleeps in between so it
runs from a small battery. Starting a preset is a single constant seven
byte write, so the board needs none of this library — see that
directory's README for wiring and caveats.

## Library usage

The API is async, built on [bleak](https://github.com/hbldh/bleak):

```python
import asyncio
from miramode import Outlet, Shower


async def run_a_bath():
    async with Shower("1FE6A4BD-73A5-055E-6463-F4785E21D49D") as shower:
        for preset in await shower.presets():
            print(preset.index, preset.name)

        await shower.run_preset(2)  # start the bath filling
        await asyncio.sleep(300)
        await shower.stop()

        # or drive an outlet directly
        await shower.set_outlets(Outlet.FIRST, temperature=39.0)


asyncio.run(run_a_bath())
```

Failures raise subclasses of `MiraError`: `NotConnected`,
`ResponseTimeout`, and `CommandFailed` when the valve rejects a command.

## Protocol notes

Messages in both directions share one frame layout:

```
aa 55 <channel> <opcode> <length> <payload...> <checksum>
```

The checksum is the two's complement of the sum of the preceding bytes,
so a whole valid frame sums to zero modulo 256. Commands are written to
characteristic `267f0002-…` and replies arrive as notifications on
`267f0003-…`, one reply per command.

Four opcodes are implemented:

| Opcode | Meaning | Payload |
| --- | --- | --- |
| `0xab` | Set outlets | temperature (2 bytes, tenths of a degree, big endian), flow percentage, outlet bitfield |
| `0xb1` | Run preset | preset slot |
| `0x5d` | Read preset | preset slot; the reply is described below |
| `0x2b` | Read state | a constant `0x02`; the reply is described below |
| `0xdd` | Write preset | the same 61 byte layout the read returns |
| `0x44` | Read name | none; the reply is a 16 byte NUL-padded name |
| `0x40` | Read serial number | a constant `0x01`; the reply is NUL-padded ASCII digits |
| `0x41` | Read manufacture date | none; the reply is four packed bytes |

The manufacture date packs years since 2000, then the day, then a
nibble holding the month followed by twelve bits of minutes past
midnight. The month has to be the nibble rather than the day, since a
day needs five bits — and the serial number embeds the same digits,
which confirms it.

A preset reply is 61 bytes:

| Bytes | Meaning |
| --- | --- |
| 0 | the slot, echoed back |
| 1-31 | name, NUL-padded ASCII |
| 33-34 | duration in seconds, or zero if the preset delivers a volume |
| 35-36 | volume in litres, or zero if the preset runs for a time |
| 37 | outlets in bits 2-4, plus the top two bits of the temperature |
| 38 | low byte of the temperature, in tenths of a degree |
| 39 | flow |

The outlet bits sit two places higher here than in the bitfield sent to
`0xab`. An unconfigured slot answers with zeroes where the name belongs,
and an invalid one echoes the wrong slot number.

Writing a preset sends that same layout back, so every preset read off a
valve re-encodes to the bytes it arrived as — which is how the encoding
was checked. Clearing a slot writes just the slot number and zeroes.
Note the name field is 32 bytes on the wire but only its first 31 are
read back, so names are limited to 31 characters. The valve stores
temperature to a tenth of a degree, though the vendor app rounds it to
whole degrees.

The state reply carries more than is decoded here. These offsets into
its payload were confirmed by commanding known values and reading them
back, and everything else is left in `State.raw`:

| Bytes | Meaning |
| --- | --- |
| 10-11 | target temperature, tenths of a degree, big endian |
| 12 | flow percentage |
| 13 | outlet bitfield, same layout as the one sent to `0xab` |
| 14-15 | measured water temperature, tenths of a degree, big endian |

Note that replies to `0x2b` use the same `0x01` opcode as an ordinary
acknowledgement, just with a long payload, so a reply cannot be
identified by its opcode alone.

The outlet field is a bitfield in a single byte — bit 0 is the first
outlet, bit 1 the second, bit 2 a third — rather than one byte per
outlet. A temperature of zero means "leave the setting alone", which is
what `stop` sends.

## Acknowledgements

Nigel Hannam's [protocol
documentation](https://github.com/nhannam/shower-controller-documentation)
was a useful reference for the older generation of these valves.

## License

MIT — see [LICENSE](LICENSE).
