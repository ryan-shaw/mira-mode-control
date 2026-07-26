"""Command line interface for controlling Mira Mode valves."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from . import (
    DEFAULT_SCAN_TIMEOUT,
    MAX_FLOW,
    MAX_PRESET,
    SERVICE_UUID,
    MiraError,
    Outlet,
    Shower,
    discover,
)

LOG = logging.getLogger(__name__)


def _temperature(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a number") from None
    if not 0 < value < 100:
        raise argparse.ArgumentTypeError(
            f"{text} is not a plausible temperature in Celsius")
    return value


def _percentage(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a number") from None
    if not 0 <= value <= MAX_FLOW:
        raise argparse.ArgumentTypeError(f"{text} is not between 0 and 100")
    return value


def _preset_index(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a number") from None
    if not 0 <= value <= MAX_PRESET:
        raise argparse.ArgumentTypeError(
            f"{text} is not between 0 and {MAX_PRESET}")
    return value


async def _scan(args: argparse.Namespace) -> int:
    devices = await discover(timeout=args.timeout)
    if not devices:
        print("No valves found.")
        return 1
    for device in devices:
        print(f"{device.address}  {device.name or '(unnamed)'}")
    return 0


async def _info(args: argparse.Namespace) -> int:
    async with Shower(args.address) as shower:
        info = await shower.device_info()
        services = shower.services()
    for label, value in (("Name", info.name),
                         ("Manufacturer", info.manufacturer),
                         ("Model", info.model)):
        if value:
            print(f"{label + ':':14}{value}")
    supported = SERVICE_UUID in services
    print(f"{'Protocol:':14}"
          f"{'GCS (supported)' if supported else 'not GCS (unsupported)'}")
    print("Services:")
    for service, characteristics in sorted(services.items()):
        print(f"  {service}")
        for characteristic in characteristics:
            print(f"    {characteristic}")
    return 0 if supported else 1


async def _presets(args: argparse.Namespace) -> int:
    async with Shower(args.address) as shower:
        presets = await shower.presets(range(args.first, args.last + 1))
    if not presets:
        print("No presets configured.")
        return 1
    for preset in presets:
        print(f"{preset.index:>3}  {preset.name}")
    return 0


async def _start(args: argparse.Namespace) -> int:
    async with Shower(args.address) as shower:
        await shower.run_preset(args.preset)
    print(f"Started preset {args.preset}.")
    return 0


async def _outlets(args: argparse.Namespace) -> int:
    outlets = Outlet.NONE
    if args.first:
        outlets |= Outlet.FIRST
    if args.second:
        outlets |= Outlet.SECOND
    async with Shower(args.address) as shower:
        await shower.set_outlets(outlets, args.temperature, args.flow)
    running = ", ".join(
        name for name, on in (("1", args.first), ("2", args.second)) if on)
    print(f"Running outlet(s) {running} at {args.temperature}C."
          if running else "All outlets off.")
    return 0


async def _stop(args: argparse.Namespace) -> int:
    async with Shower(args.address) as shower:
        await shower.stop()
    print("All outlets off.")
    return 0


def _add_address(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-a", "--address", required=True,
        help="valve address (a MAC address, or a UUID on macOS)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="miramode",
        description="Control Mira Mode digital showers and bath fillers.")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="log the frames exchanged with the valve")
    subcommands = parser.add_subparsers(dest="command", required=True)

    scan = subcommands.add_parser("scan", help="find nearby valves")
    scan.add_argument(
        "-t", "--timeout", type=float, default=DEFAULT_SCAN_TIMEOUT,
        help=f"seconds to scan for (default: {DEFAULT_SCAN_TIMEOUT:g})")
    scan.set_defaults(handler=_scan)

    info = subcommands.add_parser("info", help="show valve identification")
    _add_address(info)
    info.set_defaults(handler=_info)

    presets = subcommands.add_parser(
        "presets", help="list stored presets (reads only, runs no water)")
    _add_address(presets)
    presets.add_argument(
        "--first", type=_preset_index, default=0,
        help="lowest slot to read (default: 0)")
    presets.add_argument(
        "--last", type=_preset_index, default=MAX_PRESET,
        help=f"highest slot to read (default: {MAX_PRESET})")
    presets.set_defaults(handler=_presets)

    start = subcommands.add_parser("start", help="run a stored preset")
    _add_address(start)
    start.add_argument(
        "preset", type=_preset_index,
        help="preset slot; factory-fitted presets start at 1")
    start.set_defaults(handler=_start)

    outlets = subcommands.add_parser(
        "outlets", help="run specific outlets at a chosen temperature")
    _add_address(outlets)
    outlets.add_argument(
        "-1", "--first", action="store_true", help="run the first outlet")
    outlets.add_argument(
        "-2", "--second", action="store_true", help="run the second outlet")
    outlets.add_argument(
        "-t", "--temperature", type=_temperature, required=True,
        help="target temperature in Celsius")
    outlets.add_argument(
        "-f", "--flow", type=_percentage, default=MAX_FLOW,
        help=f"flow as a percentage (default: {MAX_FLOW})")
    outlets.set_defaults(handler=_outlets)

    stop = subcommands.add_parser("stop", help="turn every outlet off")
    _add_address(stop)
    stop.set_defaults(handler=_stop)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # Only turn up our own logging: bleak's debug output is a firehose of
    # per-platform GATT callbacks that buries the frames being exchanged.
    logging.basicConfig(format="%(message)s", level=logging.WARNING,
                        stream=sys.stderr)
    if args.verbose:
        logging.getLogger(__package__).setLevel(logging.DEBUG)
    try:
        return asyncio.run(args.handler(args))
    except MiraError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
