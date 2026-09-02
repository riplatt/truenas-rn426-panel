# Porting to other ReadyNAS models

This driver targets the **RN426 / RN526 / RN626X** front board. The same approach
should work for other Denverton- or Atom-based ReadyNAS units, but the GPIO pad
addresses and possibly the display controller differ per model. Here's how to
adapt it.

> If you're looking to run a different OS (plain Debian / OMV) on this
> *same* hardware, that's not porting, see
> [`debian-omv.md`](debian-omv.md).

## 1. Confirm the display path

Two things to check on your unit:

- **`i2cdetect -y <i801-bus>`**. Is there an MCU around `0x1c` (and maybe an RTC
  at `0x44`)? That's the button/LED controller.
- Pull the official firmware for *your* model and reconstruct symbols (see
  [`reverse-engineering.md`](reverse-engineering.md)). Check whether your model
  uses the **`oled` (SSD130x SPI)** path or the **HD44780 (parallel)** path:
  - SSD130x → `oled_probe` / `spi_send` / `init_oled` (this driver's approach).
  - HD44780 → `hd44780_lcd_probe` / `lcm_write4` / `lcd_init` (a different,
    4-bit parallel bit-bang, same idea, different pins and command set).

## 2. Find your model's pad map

### Known pad maps

I extracted the OLED config structs for six models directly out of the stock
RN426 kernel's `.rodata` (ReadyNAS OS 6.10.x, the `KERNEL` file from the
internal ReadyNAS USB stick). One kernel image carries the config for the
whole product line, so you don't need your own model's firmware to get its
pin numbers, the RN426's kernel already has them.

| config    | gpiochip     | pins (MOSI/CLK/DC/CS/EN/RESET) |
|-----------|--------------|--------------------------------|
| `rn316`   | `gpio_ich`   | 21, 19, 16, 7, 32, 24          |
| `rn426_8` | `gpio_dnv.0` | 30, 28, 7, 8, 17, 31           |
| `rnx16`   | `gpio_ich`   | 54, 52, 32, 50, 6, 7           |
| `rnx24`   | `gpio_ich`   | 54, 1, 32, 50, 6, 7            |
| `rnx26`   | `gpio_ich`   | 54, 1, 32, 50, 6, 7            |
| `rnx28`   | `gpio_ich`   | 54, 1, 32, 50, 6, 7            |

Reason to trust this table: the `rn426_8` row matches the pad map this driver
already uses, and that map was derived independently, straight off this
project's own hardware, before this table existed. The two agree, so the
extraction method checks out, and that's what the other five rows are riding
on.

Each config struct is a `char *name`, then a `char *gpiochip_label`, then six
`u32` pin numbers starting at struct offset `0x10`. That matches what Step 3
of [`reverse-engineering.md`](reverse-engineering.md) already describes for
the RN426 struct on its own, it turns out the same layout holds across the
whole product line. In the kernel I pulled this from, the RN426 struct sits
at file offset `0xf65480`, and the virtual-to-file mapping was
`file = vaddr - 0xffffffff87e00000`. That specific offset and delta belong to
that one kernel build, and will be different in any other image, so treat
them as illustrative of the method, not values to hard-code.

The important caveat: the RN426 is the only model in this table on
`gpio_dnv`, the Denverton GPIO controller this driver already talks to via
`/dev/mem` and `PADCFG_DW0`. Every other model here, `rn316`, `rnx16`,
`rnx24`, `rnx26`, `rnx28`, uses `gpio_ich` instead, a different controller.
That means the pin numbers alone don't get you a working port on those five.

`gpio_ich` is the older Intel ICH/PCH GPIO block, and it's typically
I/O-port based rather than MMIO PADCFG. If that's right, a `gpio_ich` pad
needs a different access mechanism, closer to the `/dev/port` route this
driver already uses for the P2SB unhide (`lcd-protocol.md` Step 1) than to
the `/dev/mem` route it uses for the pads. I want to be clear this part is
unverified, I have not checked it against real hardware or against the
`gpio_ich` driver itself. Check it before estimating how much work a
`gpio_ich` port is.

Practically, that means the table above is the easy half of the job for five
of the six models, the pin numbers. The work still to do is implementing the
`gpio_ich` access path. The button side should port across unchanged either
way, MSP430 over SMBus at `0x1c` doesn't depend on the SoC GPIO controller at
all, except for the interrupt pad.

The same kernel also has config-struct names for `rn422_4`, `rn313x`,
`rn42x`, `rnx2x`, and `rnx220`, but none of them produced an OLED config in
this scan. The likely explanation is that those models use the HD44780
parallel path instead of the SSD130x path (see Section 1 above), which has
different probe symbols and wouldn't turn up in a scan built around the OLED
config struct. That's the likely explanation, not a confirmed one.

If you own one of the models above, or one of the ones missing from the
scan, run [`tools/rn-probe.sh`](../tools/rn-probe.sh) on it and open an issue
with the output, see Contributing below.

### If your model isn't in the table

In the firmware's `.rodata`, find the OLED config struct for your model. Each
gives a gpiochip label and 6 pin numbers in NETGEAR's `gpio_dnv` numbering.

Then translate those to **physical `PADCFG_DW0` addresses** using the
`gpio_dnv` driver's `table5` array and `dnv_gpio_reg` formula
(`addr = community_base + PADBAR + table5[pin]`, with `+0x30000` selecting the
South community). Read PADBAR live from the hardware. The result is your
equivalent of this table:

```
MOSI / CLK / D/C / CS / EN / RESET  ->  PADCFG_DW0 physical addresses
```

Plug those into `LCD.__init__` in `rn426_panel.py`.

This only applies as-is to models on `gpio_dnv`. If your model's config
struct names a different gpiochip (`gpio_ich`, as above, or something else
entirely), see the caveat above, you're past the point this driver has
already solved.

> Models on a different SoC (older ReadyNAS on a non-Denverton chip) will have
> different GPIO community base addresses and a different P2SB/sideband scheme.
> The button side (MSP430 over SMBus) is more likely to be portable as-is.

## 3. Confirm the init sequence and geometry

Dump your firmware's `init_oled` table. If your panel is a different size
(e.g. 128×64), adjust the page count in `LCD.show` (`range(4)` → `range(8)`) and
the mux/addressing bytes accordingly. Remember the table likely **omits
display-on**, send `0xAF` after init.

## 4. Buttons

`i2cfb_reporter` shows which register holds the button bitmap (`0x04` here) and
the per-button bit shifts. Confirm empirically: poll the register and press each
button. Keep the MCU **read-only** unless you've verified its control register is
safe to write (see the `reg 0x02` warning in
[`buttons-protocol.md`](buttons-protocol.md)).

## Prior art: legacy models already covered elsewhere

Probe reports (issues #5 to #9) and forum links turned up existing projects
for the older units, so check these before porting anything here:

- **Pro6 / RNDU6000**: the LCD is not GPIO at all. It's a serial device on
  `/dev/ttyS1` (128x32, simple text protocol), and
  [berouques/rndu6000_lcd_status](https://github.com/berouques/rndu6000_lcd_status)
  already drives it under Debian / OMV with plain bash.
- **Ultra 4 / RNDU4000**: [fmor/rndu4000](https://github.com/fmor/rndu4000) is a
  kernel module for the LCD, LEDs and buttons (ICH9 LPC).
- **RN314**: a fork of the above,
  [psyrykh/readynas-rn314](https://github.com/psyrykh/readynas-rn314)
  (set `LPC_DEVICE_ID` to `0x3a18`).

This project stays focused on the RN426/428 and, next, the 52x/62x gpio_ich
route.

## Live-boot notes for probing

- ReadyNAS OS6 is Jessie-based: no `gpiod`, no `i2c-tools` by default. Boot a
  live USB to probe properly.
- The GRML 2026 image will not legacy-BIOS boot these machines. **GRML 25.12
  works.** (Reported by portalman on the Pro6 and RN516.)
- Debian kernels (bookworm and trixie, so also TrueNAS SCALE / OMV / GRML) do
  **not ship `gpio-ich.ko`** at all. `modprobe gpio-ich` failing there is
  expected and tells you nothing about the hardware. The stock OS6 kernel does
  bind `gpio_ich`, so its dmesg is the more useful evidence.

## Contributing

Not sure yet whether your model can even use this driver, or which of the
above applies to it? Run [`tools/rn-probe.sh`](../tools/rn-probe.sh) and open
an issue with its output, that's the fastest way to get a read on it.

If you get this working on another model, please open a PR adding your pad map
and init sequence (a new `LCD` subclass or a per-model config dict would be a
welcome refactor).
