# Porting to other ReadyNAS models

This driver targets the **RN426 / RN526 / RN626X** front board. The same approach
should work for other Denverton- or Atom-based ReadyNAS units, but the **GPIO pad
addresses and possibly the display controller differ per model**. Here's how to
adapt it.

## 1. Confirm the display path

Two things to check on your unit:

- **`i2cdetect -y <i801-bus>`** — is there an MCU around `0x1c` (and maybe an RTC
  at `0x44`)? That's the button/LED controller.
- Pull the official firmware for *your* model and reconstruct symbols (see
  [`reverse-engineering.md`](reverse-engineering.md)). Check whether your model
  uses the **`oled` (SSD130x SPI)** path or the **HD44780 (parallel)** path:
  - SSD130x → `oled_probe` / `spi_send` / `init_oled` (this driver's approach).
  - HD44780 → `hd44780_lcd_probe` / `lcm_write4` / `lcd_init` (a different,
    4-bit parallel bit-bang — same idea, different pins and command set).

## 2. Find your model's pad map

In the firmware's `.rodata`, find the OLED config struct for your model (they're
named like `rn422_4`, `rn426_8`, `rn316`, `rnx16`, `rnx24`, `rnx26`, `rnx28`).
Each gives a gpiochip label and 6 pin numbers in NETGEAR's `gpio_dnv` numbering.

Then translate those to **physical `PADCFG_DW0` addresses** using the
`gpio_dnv` driver's `table5` array and `dnv_gpio_reg` formula
(`addr = community_base + PADBAR + table5[pin]`, with `+0x30000` selecting the
South community). Read PADBAR live from the hardware. The result is your
equivalent of this table:

```
MOSI / CLK / D/C / CS / EN / RESET  ->  PADCFG_DW0 physical addresses
```

Plug those into `LCD.__init__` in `rn426_panel.py`.

> Models on a **different SoC** (older ReadyNAS on a non-Denverton chip) will have
> different GPIO community base addresses and a different P2SB/sideband scheme.
> The button side (MSP430 over SMBus) is more likely to be portable as-is.

## 3. Confirm the init sequence and geometry

Dump your firmware's `init_oled` table. If your panel is a different size
(e.g. 128×64), adjust the page count in `LCD.show` (`range(4)` → `range(8)`) and
the mux/addressing bytes accordingly. Remember the table likely **omits
display-on** — send `0xAF` after init.

## 4. Buttons

`i2cfb_reporter` shows which register holds the button bitmap (`0x04` here) and
the per-button bit shifts. Confirm empirically: poll the register and press each
button. Keep the MCU **read-only** unless you've verified its control register is
safe to write (see the `reg 0x02` warning in
[`buttons-protocol.md`](buttons-protocol.md)).

## Contributing

If you get this working on another model, please open a PR adding your pad map
and init sequence (a new `LCD` subclass or a per-model config dict would be a
welcome refactor).
