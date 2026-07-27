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

The valve refuses commands from a device it has not bonded with, and it
only accepts a new bond while it is in pairing mode. So the first press
has to happen inside that window:

1. Put the valve into pairing mode at its controller. On current
   controllers, press and hold the button on the front for five seconds
   until it starts flashing, then release. On models with a rotating
   dial, press the Menu button, turn to `Settings`, select, then turn to
   `Connect` and select.
2. Press the button on the board while the controller is still
   flashing. It scans, connects and pairs, which takes a few seconds.
3. Watch the serial monitor: it prints `bonded=1 encrypted=1` when the
   valve has accepted it, then `started`.

Pairing is Just Works, since the board has no display or keypad, so
there is no code to enter anywhere. After that the keys live in NVS,
which survives deep sleep and a power cycle, and every later press
connects straight away with no pairing mode needed.

The valve holds up to ten paired devices at once, so adding the button
does not displace your phone unless you are already at the limit. The
app lists them under Manage Paired Devices if you need to prune one.

If the two ever fall out of step — the valve is reset, or you remove the
button from the app's list — clear the board's side with
`pio run --target erase` and pair again from step 1, otherwise it keeps
presenting keys the valve no longer recognises.

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
