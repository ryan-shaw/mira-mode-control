"""Control Mira Mode digital showers and bath fillers over Bluetooth LE.

Current-generation Mira Mode / Kohler valves expose a GATT service
(``SERVICE_UUID``) carrying a simple binary protocol. Commands are written
to one characteristic and responses arrive as notifications on another,
one response per command.

Every message is a frame::

    aa 55 <channel> <opcode> <length> <payload...> <checksum>

where the checksum is the two's complement of the sum of all preceding
bytes, so that the bytes of a whole valid frame sum to zero modulo 256.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, IntFlag

from bleak import BleakClient, BleakScanner

__all__ = [
    "CommandFailed",
    "DeviceInfo",
    "DiscoveredDevice",
    "Frame",
    "MiraError",
    "NotConnected",
    "Opcode",
    "Outlet",
    "Preset",
    "ResponseTimeout",
    "Shower",
    "State",
    "Status",
    "discover",
    "encode_frame",
]

LOG = logging.getLogger(__name__)

SERVICE_UUID = "267f0001-eb15-43f5-94c3-67d2221188f7"
COMMAND_CHAR_UUID = "267f0002-eb15-43f5-94c3-67d2221188f7"
EVENT_CHAR_UUID = "267f0003-eb15-43f5-94c3-67d2221188f7"

_DEVICE_NAME_CHAR_UUID = "00002a00-0000-1000-8000-00805f9b34fb"
_MODEL_CHAR_UUID = "00002a24-0000-1000-8000-00805f9b34fb"
_MANUFACTURER_CHAR_UUID = "00002a29-0000-1000-8000-00805f9b34fb"

PREAMBLE = b"\xaa\x55"

#: preamble, channel, opcode and payload length.
_HEADER_SIZE = 5
#: header plus the trailing checksum byte.
_ENVELOPE_SIZE = _HEADER_SIZE + 1

#: Presets store their name in a fixed-width, NUL-padded ASCII field.
_NAME_SIZE = 31
#: The name field in the shorter reply layout the app also recognises.
_SHORT_NAME_SIZE = 15
#: Payload length of the preset reply this decodes in full.
_PRESET_REPLY_SIZE = 61

#: The state request carries one constant byte, as the vendor app sends.
_STATE_REQUEST = bytes([0x02])
#: Smallest reply we can decode every documented state field from.
_STATE_SIZE = 16

#: The serial number request carries one constant byte.
_SERIAL_REQUEST = bytes([0x01])
#: Serial numbers come back as NUL-padded ASCII digits.
_SERIAL_SIZE = 18
#: Bytes in the packed manufacturing timestamp.
_MANUFACTURED_SIZE = 4

#: How often :meth:`Shower.watch` re-reads the valve, in seconds.
DEFAULT_WATCH_INTERVAL = 2.0

#: Flow is expressed as a percentage of the valve's maximum.
MAX_FLOW = 100

#: Highest preset slot the valve will answer for.
MAX_PRESET = 15

DEFAULT_CONNECT_TIMEOUT = 20.0
DEFAULT_RESPONSE_TIMEOUT = 5.0
DEFAULT_SCAN_TIMEOUT = 5.0


class Opcode(IntEnum):
    """Message types used by the valve."""

    ACK = 0x01
    GET_STATE = 0x2B
    GET_SERIAL = 0x40
    GET_MANUFACTURED = 0x41
    GET_NAME = 0x44
    GET_PRESET = 0x5D
    SET_OUTLETS = 0xAB
    RUN_PRESET = 0xB1


class Status(IntEnum):
    """Result byte carried by an :attr:`Opcode.ACK` response."""

    OK = 0x01
    ERROR = 0x80


class Outlet(IntFlag):
    """Which outlets should be running.

    The valve takes the running outlets as a single bitfield rather than
    one field per outlet, so a command always states the complete desired
    state: anything not named here is turned off.
    """

    NONE = 0
    FIRST = 1
    SECOND = 2
    THIRD = 4


class MiraError(Exception):
    """Base class for errors raised by this package."""


class NotConnected(MiraError):
    """Raised when a command needs a connection that isn't established."""


class ResponseTimeout(MiraError):
    """Raised when the valve doesn't answer a command in time."""


class CommandFailed(MiraError):
    """Raised when the valve explicitly rejects a command."""


@dataclass(frozen=True)
class Frame:
    """A decoded protocol message."""

    channel: int
    opcode: int
    payload: bytes

    def __str__(self) -> str:
        return (
            f"channel={self.channel} opcode={self.opcode:#04x} "
            f"payload={self.payload.hex(' ')}"
        )


@dataclass(frozen=True)
class State:
    """What the valve is doing right now.

    Only the fields confirmed against a running valve are broken out;
    the reply carries more that isn't decoded yet, kept in :attr:`raw`.
    """

    #: Which outlets are running.
    outlets: Outlet
    #: Measured water temperature, in Celsius.
    temperature: float
    #: Temperature the valve is aiming for, or None when nothing is
    #: running and no target is set.
    target_temperature: float | None
    #: Flow setting, as the percentage given to :meth:`Shower.set_outlets`.
    #: Rests at a non-zero default when nothing is running.
    flow: int
    #: The undecoded reply payload.
    raw: bytes

    @property
    def is_running(self) -> bool:
        return bool(self.outlets)


@dataclass(frozen=True)
class Preset:
    """A stored programme, e.g. a bath fill.

    A preset either runs for a time or delivers a volume: whichever it
    doesn't use is None. Everything but the name is None for reply
    layouts this doesn't recognise.
    """

    index: int
    name: str
    #: Which outlets the preset runs.
    outlets: Outlet = Outlet.NONE
    #: Target temperature, in Celsius.
    temperature: float | None = None
    #: How long it runs, in seconds.
    duration: int | None = None
    #: How much water it delivers, in litres.
    volume: int | None = None
    #: Flow, on the same 0-100 scale as :meth:`Shower.set_outlets`. The
    #: vendor app shows a quarter of this, on a 0-25 scale.
    flow: int | None = None
    #: The undecoded reply payload.
    raw: bytes = b""


@dataclass(frozen=True)
class DeviceInfo:
    """How the valve identifies itself.

    Most valves answer these over the protocol rather than through the
    standard GATT device-information service, which they don't
    implement, so any field may be None.
    """

    #: The valve's own name, usually where it is installed.
    name: str | None
    #: From the standard GATT characteristics, where they exist at all.
    manufacturer: str | None
    model: str | None
    #: Serial number, as printed on the unit.
    serial_number: str | None = None
    #: When the unit was manufactured.
    manufactured: datetime | None = None


@dataclass(frozen=True)
class DiscoveredDevice:
    """A valve seen while scanning."""

    address: str
    name: str | None


def _checksum(data: bytes) -> int:
    """Return the byte that makes ``data`` sum to zero modulo 256."""
    return -sum(data) & 0xFF


def encode_frame(opcode: int, payload: bytes = b"", channel: int = 0) -> bytes:
    """Build a complete frame, checksum included."""
    if not 0 <= channel <= 0xFF:
        raise ValueError(f"channel out of range: {channel}")
    if len(payload) > 0xFF:
        raise ValueError(f"payload too long: {len(payload)} bytes")
    body = PREAMBLE + bytes((channel, int(opcode), len(payload))) + payload
    return body + bytes((_checksum(body),))


def _decode_frame(raw: bytes) -> Frame | None:
    """Decode one complete frame, or return None if it doesn't check out."""
    if len(raw) < _ENVELOPE_SIZE or not raw.startswith(PREAMBLE):
        return None
    if len(raw) != _ENVELOPE_SIZE + raw[4]:
        return None
    if sum(raw) & 0xFF:
        return None
    return Frame(channel=raw[2], opcode=raw[3], payload=raw[_HEADER_SIZE:-1])


def encode_temperature(celsius: float) -> bytes:
    """Encode a temperature as tenths of a degree, big endian.

    Zero is meaningful: the valve reads it as "leave the temperature
    alone", which is what a plain stop command sends.
    """
    tenths = round(celsius * 10)
    if not 0 <= tenths <= 0xFFFF:
        raise ValueError(f"temperature out of range: {celsius}")
    return tenths.to_bytes(2, "big")


def _decode_state(payload: bytes) -> State | None:
    """Decode a state reply, or return None if it's too short.

    Field offsets were confirmed by commanding known values and reading
    them back: a 42.0C target arrived as 01 a4 at 10, a flow of 50 as 32
    at 12, and the outlet bit at 13, while the temperature at 14 tracked
    the water warming up.
    """
    if len(payload) < _STATE_SIZE:
        return None
    target = int.from_bytes(payload[10:12], "big") / 10
    return State(
        # Bit 6 additionally means "paused", which isn't decoded here
        # because it was never observed on the test valve.
        outlets=Outlet(
            payload[13] & (Outlet.FIRST | Outlet.SECOND | Outlet.THIRD)
        ),
        temperature=int.from_bytes(payload[14:16], "big") / 10,
        target_temperature=target or None,
        flow=payload[12],
        raw=bytes(payload),
    )


def _differs(state: State, other: State) -> bool:
    """Whether two readings differ in any field we actually decode."""
    return (
        state.outlets,
        state.temperature,
        state.target_temperature,
        state.flow,
    ) != (
        other.outlets,
        other.temperature,
        other.target_temperature,
        other.flow,
    )


def _decode_text(raw: bytes) -> str | None:
    """Decode a NUL-padded ASCII field, or None if it isn't one.

    Unset fields come back as arbitrary bytes rather than empty, so
    anything that isn't printable ASCII with NUL padding is rejected.
    """
    if any(byte and not 0x20 <= byte < 0x7F for byte in raw):
        return None
    return raw.split(b"\x00", 1)[0].decode("ascii").strip() or None


def _decode_manufactured(payload: bytes) -> datetime | None:
    """Decode the packed manufacturing timestamp, or None if implausible.

    Four bytes: years since 2000, the day, then a nibble holding the
    month followed by twelve bits of minutes past midnight. The month
    has to be the nibble rather than the day, since a day needs five
    bits. Cross-checks against the serial number, which embeds the same
    digits.
    """
    if len(payload) < _MANUFACTURED_SIZE:
        return None
    year, day = 2000 + payload[0], payload[1]
    month = payload[2] >> 4
    minutes = ((payload[2] & 0x0F) << 8) | payload[3]
    if not (1 <= month <= 12 and 1 <= day <= 31 and minutes < 24 * 60):
        return None
    try:
        return datetime(year, month, day, minutes // 60, minutes % 60)
    except ValueError:
        return None


def _decode_preset(payload: bytes) -> Preset | None:
    """Decode a preset slot, or return None if the slot isn't in use.

    An unconfigured slot answers with a mismatched index, or with zeroes
    or arbitrary bytes where the name belongs.

    Only the 61 byte layout is decoded in full. The app recognises a
    shorter one too, which no valve here produces, so for anything else
    just the name is read and the rest left None rather than guessed at.
    """
    if len(payload) < 1 + _SHORT_NAME_SIZE:
        return None
    long_form = len(payload) == _PRESET_REPLY_SIZE
    name_size = _NAME_SIZE if long_form else _SHORT_NAME_SIZE
    name = _decode_text(payload[1 : 1 + name_size])
    if name is None:
        return None
    if not long_form:
        return Preset(index=payload[0], name=name, raw=bytes(payload))

    flags = payload[37]
    return Preset(
        index=payload[0],
        name=name,
        # The outlet bits sit two places higher here than in the
        # bitfield sent to SET_OUTLETS, which uses bits 0 to 2.
        outlets=Outlet((flags >> 2) & 0b111),
        # The low two bits of the flags byte extend the temperature,
        # which is why it needs masking off before reading the outlets.
        temperature=(((flags & 0b11) << 8) | payload[38]) / 10,
        duration=int.from_bytes(payload[33:35], "big") or None,
        volume=int.from_bytes(payload[35:37], "big") or None,
        flow=payload[39],
        raw=bytes(payload),
    )


class _FrameReader:
    """Reassembles frames from notification chunks.

    A response usually arrives in a single notification, but nothing
    guarantees that, so incoming bytes are buffered and frames are taken
    out as they complete.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buffer += chunk
        frames: list[Frame] = []
        while True:
            start = self._buffer.find(PREAMBLE)
            if start < 0:
                # Keep a trailing byte, which may be a split preamble.
                del self._buffer[: max(0, len(self._buffer) - 1)]
                break
            del self._buffer[:start]
            if len(self._buffer) < _ENVELOPE_SIZE:
                break
            size = _ENVELOPE_SIZE + self._buffer[4]
            if len(self._buffer) < size:
                break
            raw = bytes(self._buffer[:size])
            del self._buffer[:size]
            frame = _decode_frame(raw)
            if frame is None:
                LOG.debug("Discarding malformed frame: %s", raw.hex(" "))
            else:
                frames.append(frame)
        return frames


async def discover(
    timeout: float = DEFAULT_SCAN_TIMEOUT,
) -> list[DiscoveredDevice]:
    """Scan for nearby valves.

    Matches on the advertised service UUID, falling back to the name for
    valves that advertise without it.
    """
    found: dict[str, DiscoveredDevice] = {}
    for device, advertisement in (
        await BleakScanner.discover(timeout=timeout, return_adv=True)
    ).values():
        name = advertisement.local_name or device.name
        uuids = {uuid.lower() for uuid in advertisement.service_uuids or ()}
        if SERVICE_UUID in uuids or (name and "mira" in name.lower()):
            found[device.address] = DiscoveredDevice(device.address, name)
    return sorted(found.values(), key=lambda d: (d.name or "", d.address))


class Shower:
    """A connection to one valve.

    Usable as an async context manager::

        async with Shower(address) as shower:
            await shower.run_preset(2)
    """

    def __init__(
        self,
        address: str,
        *,
        channel: int = 0,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        response_timeout: float = DEFAULT_RESPONSE_TIMEOUT,
    ) -> None:
        self._address = address
        self._channel = channel
        self._connect_timeout = connect_timeout
        self._response_timeout = response_timeout
        self._client: BleakClient | None = None
        self._reader = _FrameReader()
        self._pending: asyncio.Future | None = None

    @property
    def address(self) -> str:
        return self._address

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def __aenter__(self) -> Shower:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        if self._client is not None:
            return
        client = BleakClient(self._address, timeout=self._connect_timeout)
        await client.connect()
        try:
            self._reader.reset()
            await client.start_notify(EVENT_CHAR_UUID, self._on_notification)
        except Exception:
            await client.disconnect()
            raise
        self._client = client
        LOG.debug("Connected to %s", self._address)

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        self._fail_pending(NotConnected("Disconnected while awaiting a reply"))
        try:
            if client.is_connected:
                await client.stop_notify(EVENT_CHAR_UUID)
        except Exception:
            LOG.debug(
                "Ignoring error while stopping notifications", exc_info=True
            )
        await client.disconnect()
        LOG.debug("Disconnected from %s", self._address)

    def _require_client(self) -> BleakClient:
        if self._client is None:
            raise NotConnected("Not connected; call connect() first")
        return self._client

    def _fail_pending(self, error: Exception) -> None:
        pending, self._pending = self._pending, None
        if pending is not None and not pending.done():
            pending.set_exception(error)

    def _on_notification(
        self, _characteristic: object, data: bytearray
    ) -> None:
        for frame in self._reader.feed(bytes(data)):
            LOG.debug("Received %s", frame)
            pending, self._pending = self._pending, None
            if pending is not None and not pending.done():
                pending.set_result(frame)
            else:
                LOG.debug("Ignoring unsolicited frame")

    async def _request(self, opcode: int, payload: bytes = b"") -> Frame:
        """Send a command and wait for the valve's reply."""
        client = self._require_client()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending = future
        frame = encode_frame(opcode, payload, self._channel)
        LOG.debug("Sending %s", frame.hex(" "))
        try:
            await client.write_gatt_char(
                COMMAND_CHAR_UUID, frame, response=False
            )
            return await asyncio.wait_for(future, self._response_timeout)
        except asyncio.TimeoutError:
            raise ResponseTimeout(
                f"No reply to command {opcode:#04x}"
            ) from None
        finally:
            if self._pending is future:
                self._pending = None

    @staticmethod
    def _check_ack(frame: Frame) -> None:
        status = frame.payload[0] if frame.payload else None
        if status == Status.OK:
            return
        if status == Status.ERROR:
            raise CommandFailed("The valve rejected the command")
        raise CommandFailed(f"Unexpected reply: {frame}")

    async def set_outlets(
        self,
        outlets: Outlet,
        temperature: float,
        flow: int = MAX_FLOW,
    ) -> None:
        """Set exactly which outlets run, at a given temperature and flow.

        Outlets absent from ``outlets`` are turned off.
        """
        if not 0 <= flow <= MAX_FLOW:
            raise ValueError(f"flow out of range: {flow}")
        payload = encode_temperature(temperature) + bytes(
            (flow if outlets else 0, int(outlets))
        )
        self._check_ack(await self._request(Opcode.SET_OUTLETS, payload))

    async def stop(self) -> None:
        """Turn every outlet off, leaving the temperature setting alone."""
        await self.set_outlets(Outlet.NONE, 0.0, 0)

    async def status(self) -> State:
        """Read what the valve is doing right now.

        This only reads; it doesn't change anything or run any water.
        """
        frame = await self._request(Opcode.GET_STATE, _STATE_REQUEST)
        state = _decode_state(frame.payload)
        if state is None:
            raise CommandFailed(f"Could not read the valve's state: {frame}")
        return state

    async def watch(
        self,
        interval: float = DEFAULT_WATCH_INTERVAL,
        *,
        changes_only: bool = True,
    ) -> AsyncIterator[State]:
        """Re-read the valve forever, yielding its state as it changes.

        The first reading is always yielded. After that, ``changes_only``
        suppresses readings identical to the last one, comparing the
        decoded fields and ignoring the undecoded remainder, some of
        which changes on every poll.
        """
        previous: State | None = None
        while True:
            state = await self.status()
            if (
                previous is None
                or not changes_only
                or _differs(state, previous)
            ):
                yield state
                previous = state
            await asyncio.sleep(interval)

    async def run_preset(self, index: int) -> None:
        """Start a stored preset.

        Factory-fitted presets are numbered from 1.
        """
        self._check_ack(
            await self._request(Opcode.RUN_PRESET, bytes((index,)))
        )

    async def read_preset(self, index: int) -> Preset | None:
        """Read one preset slot, returning None if it isn't configured."""
        frame = await self._request(Opcode.GET_PRESET, bytes((index,)))
        preset = _decode_preset(frame.payload)
        if preset is None or preset.index != index:
            return None
        return preset

    async def presets(
        self,
        indexes: Iterable[int] | None = None,
    ) -> list[Preset]:
        """Read every configured preset in ``indexes``.

        This only reads from the valve; it doesn't run any water.
        """
        if indexes is None:
            indexes = range(MAX_PRESET + 1)
        found = []
        for index in indexes:
            preset = await self.read_preset(index)
            if preset is not None:
                found.append(preset)
        return found

    def services(self) -> dict[str, list[str]]:
        """Map each GATT service the valve offers to its characteristics."""
        client = self._require_client()
        return {
            service.uuid.lower(): sorted(
                char.uuid.lower() for char in service.characteristics
            )
            for service in client.services
        }

    async def device_info(self) -> DeviceInfo:
        """Read whatever identification the valve exposes.

        Not every valve implements the standard device-information
        characteristics, so any of these fields may be None.
        """
        client = self._require_client()
        available = {
            uuid for uuids in self.services().values() for uuid in uuids
        }

        async def read(uuid: str) -> str | None:
            if uuid not in available:
                return None
            try:
                value = await client.read_gatt_char(uuid)
            except Exception:
                LOG.debug("Could not read %s", uuid, exc_info=True)
                return None
            return value.decode("utf-8", "replace").strip() or None

        values: Sequence[str | None] = await asyncio.gather(
            read(_DEVICE_NAME_CHAR_UUID),
            read(_MANUFACTURER_CHAR_UUID),
            read(_MODEL_CHAR_UUID),
        )

        # These come over the protocol, and are the only ones this
        # hardware actually answers - it has no device-information
        # service at all. Each is optional, so a valve that doesn't
        # know one of them still reports the rest.
        async def ask(opcode: int, payload: bytes = b"") -> bytes | None:
            try:
                return (await self._request(opcode, payload)).payload
            except MiraError:
                LOG.debug("Valve did not answer %#04x", opcode, exc_info=True)
                return None

        name = await ask(Opcode.GET_NAME)
        serial = await ask(Opcode.GET_SERIAL, _SERIAL_REQUEST)
        made = await ask(Opcode.GET_MANUFACTURED)
        return DeviceInfo(
            name=_decode_text(name) if name else values[0],
            serial_number=(
                _decode_text(serial[:_SERIAL_SIZE]) if serial else None
            ),
            manufactured=_decode_manufactured(made) if made else None,
            manufacturer=values[1],
            model=values[2],
        )
