"""Real replies recorded from a Mira Mode dual shower and bath filler.

These are the bytes the valve actually sent, which is what makes them
worth keeping: a decoder that agrees with them agrees with hardware,
not with our reading of a decompiled app. Where a value was commanded
before being read back, the comment says so, because those pin a field
down far more firmly than a single observation of an idle valve.
"""


def frame(payload: bytes, opcode: int = 0x01, channel: int = 1) -> bytes:
    """Wrap a recorded payload in the envelope it arrived in."""
    body = b"\xaa\x55" + bytes((channel, opcode, len(payload))) + payload
    return body + bytes((-sum(body) & 0xFF,))


def payload(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


#: The valve's reply to any command it accepted.
ACK = payload("aa 55 01 01 01 01 fd")

# --- state, opcode 0x2b ----------------------------------------------

#: Nothing running.
STATE_IDLE = payload("21 20 00 00 00 00 00 00 80 00 00 00 10 00 01 00 00 01")

#: Outlet 1 running, four seconds after being commanded to 42.0C at a
#: flow of 50. Both values appear in the reply: 01 a4 is 420 tenths of a
#: degree, and 32 is 50.
STATE_RUNNING = payload(
    "21 20 00 00 00 12 00 00 00 00 01 a4 32 01 00 f6 12 01"
)

#: The same run six seconds later. The temperature at bytes 14-15 has
#: climbed from 24.6C to 26.0C, while the target and flow are unchanged.
STATE_RUNNING_WARMER = payload(
    "21 20 00 00 00 1b 00 00 00 00 01 a4 32 01 01 04 1b 01"
)

#: Just after stopping.
STATE_STOPPED = payload(
    "21 20 00 00 00 00 00 00 80 00 00 00 10 00 01 03 00 01"
)

# --- presets, opcode 0x5d --------------------------------------------

_TAIL = " 00" * 21

#: Slot 1, the factory shower preset: 38.0C, outlet 2, 30 minutes.
PRESET_SHOWER = payload(
    "01 44 65 66 61 75 6c 74 00 00 00 00 00 00 00 00"
    "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    "00 07 08 00 00 09 7c 58" + _TAIL
)

#: Slot 2, the factory bath preset: 42.0C, outlet 1, 50 litres. Its name
#: is exactly 16 characters, which is why a 16 byte name field appeared
#: to work before the real 31 byte field was found.
PRESET_BATH = payload(
    "02 44 65 66 61 75 6c 74 20 42 61 74 68 66 69 6c"
    "6c 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    "00 00 00 00 32 05 a4 58" + _TAIL
)

#: Slot 3, configured on nothing: the slot echoes back, all else zero.
PRESET_EMPTY = payload("03" + " 00" * 60)

#: Slot 0, which is not a valid slot. The valve echoes 0xff instead of
#: the slot asked for, and returns unrelated bytes after it.
PRESET_INVALID = payload(
    "ff 20 00 00 00 00 00 00 80 00 00 00 10 00 01 01 00 01" + " 00" * 43
)

# --- identification --------------------------------------------------

#: Opcode 0x44. The valve is named after where it is installed.
NAME = payload("42 61 74 68 72 6f 6f 6d 00 00 00 00 00 00 00 00")

#: Opcode 0x40, as printed on the unit.
SERIAL = payload("33 31 34 30 30 39 30 31 32 35 31 32 31 39 34 36 00 00")

#: Opcode 0x41: 9 January 2025 at 12:19. The serial number above embeds
#: the same digits - 09 01 25 12 19 - which is what confirms the decode.
MANUFACTURED = payload("19 09 12 e3")

#: Opcode 0x42 on this valve, which has no device ID set.
DEVICE_ID_EMPTY = payload("02" + " 00" * 14)
