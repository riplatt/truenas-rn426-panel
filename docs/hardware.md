# Hardware

## The unit

- **NETGEAR ReadyNAS RN426** (8-bay). The front board is marked
  **"RN526&RN626X Front Board V20"** — it is shared across the RN426 / RN526 /
  RN626X, so this driver should apply to all three.
- **SoC:** Intel Atom **C3538 "Denverton"** (Harcuvar platform).
- **BIOS:** AMI, `RN426v33`.

## Front board

Located behind the front bezel, connected to the mainboard by a ribbon cable.

| Component | Part | Role |
|-----------|------|------|
| Microcontroller | **TI MSP430G2518** (U1) | Scans the buttons, drives the button backlights / front LEDs; host talks to it over i2c/SMBus |
| Display | **2832ALBC** module | 128×32 graphic LCD, **SSD1305-class** controller, SPI |
| Buttons | S1–S9 | 5-way navigation pad (up / down / left / right / center) + others |
| LEDs | CR1–CR9 | Button backlights and indicators (driven by the MSP430) |

## Buses and connections

The ribbon carries **two independent interfaces** plus control lines:

1. **SPI to the LCD**, bit-banged from the host over **Denverton SoC GPIO pads**
   (it is *not* routed through the MSP430). Six pads are used: CLK, MOSI, D/C,
   CS, RESET, and a display/backlight **EN** line. See
   [`lcd-protocol.md`](lcd-protocol.md) for the exact pad addresses.

2. **i2c (Intel i801 SMBus) to the MSP430** at address **`0x1c`**. See
   [`buttons-protocol.md`](buttons-protocol.md).

There is also a small **RTC** on the same SMBus at **`0x44`** (DS1307-class).

> ⚠️ The `/dev/i2c-N` number for the i801 SMBus is **not stable across reboots**
> — TrueNAS also exposes an **iSMT** SMBus adapter, and the two can swap between
> `i2c-0` and `i2c-1`. Always locate the i801 by name
> (`/sys/class/i2c-dev/i2c-*/name` contains `SMBus I801 adapter`).

## Related but separate: chassis fan control

**Chassis fan** control is via the **IT8613/IT8622** Super-I/O over **LPC**
(hwmon), a completely different chip and bus from the front board covered by
this doc — confirmed on this unit as an **IT8622**. If your fans run
flat-out under TrueNAS, see [`fan-control.md`](fan-control.md) and
[`../tools/it87-fancontrol.sh`](../tools/it87-fancontrol.sh).
