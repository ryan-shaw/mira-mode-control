# A battery powered button

Firmware for a Seeed Studio XIAO ESP32C3 that starts a Mira Mode preset
when a button is pressed. It sleeps between presses, so it runs from a
small battery for a long time.

Starting a preset is a single constant seven byte write, so there is no
protocol to speak here beyond sending those bytes and waiting to be
acknowledged. The firmware is almost entirely Bluetooth plumbing.

## Wiring

| Part | Connection |
| --- | --- |
| Momentary button | between `D2` (GPIO 2) and `GND` |
| LED, optional | `D3` (GPIO 3) through a 330Ω resistor to `GND` |
| Battery | a 3.7V LiPo to the `+`/`-` pads underneath |

Only GPIO 0 to 5 can wake an ESP32-C3 from deep sleep, so the button has
to be on one of those. The internal pull-up is enabled and held through
sleep; adding a 10kΩ pull-up from the button pin to `3V3` costs nothing
and makes a long or noisy cable more dependable.

The XIAO has no user LED of its own, only a charge indicator, so the LED
above is a part you add. Set `LED_PIN` to `GPIO_NUM_NC` to leave it out.
It flashes once on the press, twice when the valve confirms it started,
and five times quickly on failure.

The XIAO charges the cell over USB-C, so the battery can stay connected
while flashing.

## Building

```console
pio run --target upload
pio device monitor
```

Set `PRESET_SLOT` at the top of `src/main.cpp` to the preset you want.
List what your valve has with `miramode presets -a <address>`; on the
unit this was developed against, slot 1 is the shower and slot 2 the
bath fill.

The valve does not need to be configured here. On the first press the
board scans for anything advertising the Mira service, then keeps that
address in RTC memory, which survives deep sleep. Only the first press
after a power cycle pays for a scan.

## Pairing

The valve refuses commands from a device it has not bonded with, so the
firmware pairs before writing and NimBLE keeps the keys in NVS, which
survives both deep sleep and a power cycle. Pairing is Just Works: the
board has no display or keypad, so there is no code to enter.

Two things to be aware of, neither of which has been tested here:

- Whether the valve limits how many devices it will bond with. If it
  does, adding this button could in principle displace your phone. Try
  the phone app again after the first successful press.
- If the valve is factory reset, or forgets this board, the stored keys
  go stale. Erasing the board's flash with `pio run --target erase`
  clears its side so the two can pair afresh.

## Battery life

The board sleeps at roughly 44µA, so idle draw dominates: about 0.4mAh
per day, or under 150mAh a year. A press costs a second or two of radio
at perhaps 80mA, which is around 0.05mAh — so a few hundred presses a
year add up to less than a day of idling. A 500mAh cell should last
comfortably over a year.

That figure depends on getting back to sleep, so if battery life turns
out poor, check the serial log: a failing scan burns five seconds of
radio on every press.

## What to expect

The valve is connected to from cold on each press, so water starts one
to three seconds after the button, a little longer on the first press
after a power cycle when it also has to scan.
