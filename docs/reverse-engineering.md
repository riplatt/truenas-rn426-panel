# Reverse-engineering notes

How the protocol was recovered from NETGEAR's stock ReadyNAS firmware. This is
the methodology, so it can be repeated for other ReadyNAS models / front boards.

## Why the display is stuck on "Booting..."

TrueNAS SCALE has **no driver** for NETGEAR's front board. The ReadyNAS
BIOS/bootloader writes `Booting...` to the LCD's GDDRAM during boot, and since an
LCD **retains its image without active driving**, that frame just stays there
forever under TrueNAS. (In the stock kernel, `readynas_lcd_init` is what writes
the `"Booting.."` string.) Nothing is broken — the panel simply needs a driver.

## Step 1 — get the stock kernel and its symbols

1. Download the official ReadyNAS OS 6 firmware `.img` for the RN426.
2. Unpack it: the `.img` is a 16 KB `info::` header followed by a tar containing
   `kernel` (a bzImage), `initrd.gz`, and `root.tlz` (an lzma'd tar rootfs).
3. Extract `vmlinux` from the bzImage (decompress the embedded xz/gzip payload).
4. The kernel has no symbol table, but it has an embedded **kallsyms** table.
   Reconstruct an ELF with symbols using
   [`vmlinux-to-elf`](https://github.com/marin-m/vmlinux-to-elf):
   ```
   vmlinux-to-elf vmlinux vmlinux.elf
   ```
   Now `nm -n vmlinux.elf` and `objdump -d` work normally.

## Step 2 — find the display driver

`nm`/`strings` surface the relevant functions. The driver supports two display
types; the RN426 uses the **SSD130x "oled" (SPI)** path, not the HD44780 one:

| Symbol | What it does |
|--------|--------------|
| `oled_probe` | reads the per-model config (gpiochip label + 6 pin numbers), claims the gpiod lines |
| `spi_send` | bit-bangs one SPI byte over 4 gpiod descriptors (CS/CLK/MOSI/DC) |
| `init_oled` | pulses RESET, sends a 33-byte init table, sets EN, backlight on (safe only in NETGEAR's own cold-boot sequence, where the MSP430 is coming up anyway — not safe to replicate once the system is running, see [`lcd-protocol.md`](lcd-protocol.md)) |
| `oled_data_write` | writes a character (font-rendered) |
| `readynas_lcd_init` | registers the driver and writes `"Booting.."` |

Disassembling `spi_send` reveals the 4-wire bit-bang (MSB-first, latch on rising
CLK). `init_oled` reads its 33 init bytes from a `.rodata` table — dump it to get
the **SSD1305 init sequence**. Note it has **no display-on**; `0xAF` is sent
elsewhere.

## Step 3 — find the GPIO pad map (the tricky part)

`oled_probe` loads a config struct (`gpiochip label` + pins at offsets
`0x10..0x24`). Scanning `.rodata` for structs whose label points to a gpiochip
name and is followed by valid pin numbers yields the per-model configs:

```
name='rn426_8'  chip=gpio_dnv.0  pins=[30, 28, 7, 8, 17, 31]
```

But those pin numbers are in NETGEAR's **custom `gpio_dnv` driver numbering**,
which does **not** match mainline `pinctrl-denverton`. So you cannot just load
mainline pinctrl and use offsets 30/28/7/8/17/31 — you'll drive the wrong pads
(this wasted real effort; the symptom was "no display change").

Instead, decode `gpio_dnv`'s own mapping:

- `dnv_gpio_set(chip, off, val)` calls `dnv_gpio_reg(chip, off, 5)` and sets
  **bit 0** of the returned register → confirms output is `PADCFG_DW0` bit 0.
- `dnv_gpio_reg` computes `addr = mmio_base + reg_base[5] + table5[off]`, where
  **`table5`** (a `.rodata` array) holds each pad's **PADCFG offset**. For
  `off >= 14` it's cleanly `(off-14)*8`; offsets 3–13 get `+0x30000`, which maps
  the **North** community (`0xFDC20000`) to the **South** one (`0xFDC50000`,
  since `0xFDC50000 - 0xFDC20000 = 0x30000`).

Reading `table5` for the RN426 pins and adding the community base + PADBAR
(read live from the hardware: `0x400`) gives the **physical PADCFG_DW0
addresses** used by this driver. From there, drive bit 0 (level) and clear bit 9
(`GPIOTXDIS`) directly via `/dev/mem` — bypassing the gpiochip numbering
entirely. See [`lcd-protocol.md`](lcd-protocol.md) for the resulting table.

## Step 4 — the buttons

| Symbol | What it does |
|--------|--------------|
| `i2cfb_button_depressed` / `i2cfb_reporter` | `i2c_smbus_read_byte_data(client, 0x04)` → button bitmap |
| `button_irq_init` | sets up a GPIO IRQ from the MCU (interrupt-driven reads) |

So buttons = MSP430 `reg 0x04` bitmap over SMBus. The bit→button mapping was then
confirmed empirically by holding each button and watching `reg 0x04`
(see [`buttons-protocol.md`](buttons-protocol.md)).

## Dead-ends and gotchas worth knowing

- **GPIO chardev v1 ABI is disabled** on this TrueNAS kernel
  (`CONFIG_GPIO_CDEV_V1=n`): `GPIO_GET_LINEHANDLE` returns `EINVAL`. Use the v2
  ABI (`GPIO_V2_GET_LINE`) or, as here, just `/dev/mem`.
- **Mainline `pinctrl-denverton` line offsets ≠ stock `gpio_dnv` offsets.** Don't
  trust the firmware's pin numbers against a mainline gpiochip.
- **i801 is SMBus-only** — no raw i2c (`i2ctransfer` fails); use SMBus ops.
- **i2c bus numbering swaps across reboots** (i801 ↔ iSMT); find i801 by name.
- **MSP430 `reg 0x02` gates button scanning** and only resets on a cold
  power-cycle — see [`buttons-protocol.md`](buttons-protocol.md).
- The unhidden **P2SB** is needed for the SoC GPIO MMIO; the BIOS re-hides it on
  every boot, so the driver un-hides it at startup.
