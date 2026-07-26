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
  system's Bluetooth settings. The valve refuses to accept commands over
  an unbonded connection.

## Installation

```console
git clone git@github.com:ryan-shaw/mira-mode-control.git
cd mira-mode-control
uv sync
```

This uses [uv](https://docs.astral.sh/uv/), which puts a virtual
environment in `.venv` and installs the versions pinned in `uv.lock`.
Prefix commands with `uv run` to use it. Plain `pip install .` works too.

### Development

The same checks CI runs:

```console
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

Use `uv run ruff format .` to apply formatting.

## Command line usage

Find your valve:

```console
$ uv run miramode scan
1FE6A4BD-73A5-055E-6463-F4785E21D49D  Mira 004C Bathroom
```

Addresses are MAC addresses on Linux and Windows, and opaque UUIDs on
macOS. Every command below takes one with `-a`.

### Presets

Presets are the programmes stored in the valve — each carries its own
target temperature and run time, and they're how the vendor app starts
the shower or fills the bath. Listing them only reads from the valve and
runs no water:

```console
$ uv run miramode presets -a <address>
  1  Default
  2  Default Bathfill
```

Factory-fitted presets are numbered from 1; slot 0 is not used. Start one
by number:

```console
uv run miramode start -a <address> 2
```

### Outlets

To run an outlet directly, at a temperature you choose:

```console
uv run miramode outlets -a <address> --first --temperature 39
```

Use `--second` for the second outlet, and both flags together to run both
at once. A command states the complete desired state, so any outlet you
don't name is switched off. `--flow` sets the flow rate as a percentage,
defaulting to 100.

Which physical fixture is "first" depends on how your unit is plumbed.

To shut everything off, including a running preset:

```console
uv run miramode stop -a <address>
```

### Troubleshooting

`-v` logs every frame exchanged with the valve:

```console
$ uv run miramode -v stop -a <address>
Connected to 1FE6A4BD-73A5-055E-6463-F4785E21D49D
Sending aa 55 00 ab 04 00 00 00 00 52
Received channel=1 opcode=0x01 payload=01
```

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

Three opcodes are implemented:

| Opcode | Meaning | Payload |
| --- | --- | --- |
| `0xab` | Set outlets | temperature (2 bytes, tenths of a degree, big endian), flow percentage, outlet bitfield |
| `0xb1` | Run preset | preset slot |
| `0x5d` | Read preset | preset slot; the reply echoes the slot then a 16 byte NUL-padded name |

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
