"""A stand-in for the valve, so Shower can be tested without one."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

import miramode as m

from . import captures

#: What a real valve offers: the protocol service and a DFU service, and
#: notably no device-information service.
FAKE_SERVICES = {
    m.SERVICE_UUID: [m.COMMAND_CHAR_UUID, m.EVENT_CHAR_UUID],
    "0000fe59-0000-1000-8000-00805f9b34fb": [
        "8ec90003-f315-4f60-9fb8-838830daea50"
    ],
}


class _FakeCharacteristic:
    def __init__(self, uuid: str) -> None:
        self.uuid = uuid


class _FakeService:
    def __init__(self, uuid: str, characteristics: list[str]) -> None:
        self.uuid = uuid
        self.characteristics = [
            _FakeCharacteristic(c) for c in characteristics
        ]


class FakeValve:
    """Answers written frames the way the hardware does.

    ``responder`` is handed each decoded request and returns the payload
    to reply with, or None to stay silent so timeouts can be exercised.
    Replies are wrapped in the envelope real replies arrive in, which
    always carries the generic acknowledgement opcode.
    """

    def __init__(
        self,
        responder: Callable[[m.Frame], bytes | None] | None = None,
        *,
        services: dict[str, list[str]] | None = None,
        chunk_size: int | None = None,
    ) -> None:
        self.responder = responder or (lambda _frame: b"\x01")
        self.requests: list[m.Frame] = []
        self.connected = False
        self.notifying = False
        self.disconnect_calls = 0
        self.gatt_reads: list[str] = []
        self._services = FAKE_SERVICES if services is None else services
        self._chunk_size = chunk_size
        self._callback: Callable[[object, bytearray], None] | None = None

    # -- the slice of BleakClient that Shower uses --------------------

    def __call__(self, address: str, timeout: float = 0.0) -> FakeValve:
        """Stand in for the BleakClient constructor."""
        self.address = address
        return self

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    async def start_notify(self, uuid: str, callback) -> None:
        assert uuid == m.EVENT_CHAR_UUID
        self._callback = callback
        self.notifying = True

    async def stop_notify(self, uuid: str) -> None:
        self.notifying = False

    @property
    def services(self) -> list[_FakeService]:
        return [
            _FakeService(uuid, chars) for uuid, chars in self._services.items()
        ]

    async def read_gatt_char(self, uuid: str) -> bytearray:
        self.gatt_reads.append(uuid)
        raise Exception("no device information service")

    async def write_gatt_char(
        self, uuid: str, data: bytes, response: bool = True
    ) -> None:
        assert uuid == m.COMMAND_CHAR_UUID
        frame = m._decode_frame(bytes(data))
        assert frame is not None, "Shower wrote a frame that will not decode"
        self.requests.append(frame)
        payload = self.responder(frame)
        if payload is not None:
            self.notify(captures.frame(payload))

    # -- driving the fake --------------------------------------------

    def notify(self, raw: bytes) -> None:
        """Deliver bytes as the valve would, honouring any chunking."""
        assert self._callback is not None, "nothing is listening"
        size = self._chunk_size or len(raw)
        for i in range(0, len(raw), size):
            self._callback(object(), bytearray(raw[i : i + size]))

    @property
    def last_request(self) -> m.Frame:
        return self.requests[-1]


@pytest.fixture(autouse=True)
def _quick_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop tests waiting out the real timeouts.

    The CLI builds a Shower with the production defaults, so a test of a
    valve that never answers would otherwise sleep for five seconds per
    request. Tests that pass a timeout explicitly keep it.
    """
    original = m.Shower.__init__

    def patched(self, address, **kwargs):
        kwargs.setdefault("response_timeout", 0.05)
        kwargs.setdefault("connect_timeout", 0.05)
        original(self, address, **kwargs)

    monkeypatch.setattr(m.Shower, "__init__", patched)


@pytest.fixture
def valve(monkeypatch: pytest.MonkeyPatch) -> FakeValve:
    fake = FakeValve()
    monkeypatch.setattr(m, "BleakClient", fake)
    return fake


@pytest.fixture
def shower(valve: FakeValve) -> m.Shower:
    # Keep the response timeout short so timeout tests stay quick.
    return m.Shower("fake-address", response_timeout=0.1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def preset_responder(slots: dict[int, bytes]):
    """Reply to preset reads from ``slots``, acknowledging anything else."""

    def respond(frame: m.Frame) -> bytes | None:
        if frame.opcode == m.Opcode.GET_PRESET:
            index = frame.payload[0]
            return slots.get(index, bytes([index]) + b"\x00" * 60)
        return b"\x01"

    return respond


async def let_the_loop_run() -> None:
    """Yield control so a background task can make progress."""
    await asyncio.sleep(0)
