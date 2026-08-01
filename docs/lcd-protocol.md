# LCD protocol (SSD1305, bit-bang SPI via /dev/mem)

> ⚠️ **Before touching RST or EN:** never pulse **RST**, never drive **EN**
> low. Both are *shared* front-board lines, not LCD-private. Lowering
> either wedges the **MSP430** out of button-reporting mode, and only a full
> **AC power-cycle** recovers it (a warm reboot is not enough). See
> [`buttons-protocol.md`](buttons-protocol.md) for the related `reg 0x02`
> hazard on the same MCU.

The display is a 128×32 SSD1305-class panel driven by 4-wire SPI
(CS, CLK, MOSI/SDIN, D/C) plus **RESET** and a display/backlight **EN** line.
There is no SPI controller in play. The host bit-bangs all six signals on
Denverton SoC GPIO pads, written directly through `/dev/mem`.

## Step 1: un-hide the P2SB (SBREG) window

The Denverton GPIO community registers live behind the **P2SB** sideband bridge
at `0xFD000000`, which the BIOS hides. Clear **bit 0 of PCI `00:1f.1` config
register `0xE1`** to reveal it. Pure-Python via legacy PCI config ports:

```python
def pci_cfg_write_byte(bus, dev, fn, reg, val):
    addr = 0x80000000 | (bus<<16) | (dev<<11) | (fn<<8) | (reg & 0xFC)
    with open("/dev/port", "r+b", 0) as p:
        p.seek(0xCF8); p.write(struct.pack("<I", addr))   # outl CONFIG_ADDRESS
        p.seek(0xCFC + (reg & 3)); p.write(bytes([val]))  # outb CONFIG_DATA
pci_cfg_write_byte(0, 0x1F, 1, 0xE1, 0x00)
```

## Step 2: the GPIO pads (PADCFG_DW0 registers)

After unhiding, the two GPIO communities are mmap-able from `/dev/mem`. Each pad
has a `PADCFG_DW0` register (PADBAR = `0x400` on both communities here):

| Signal | Community base | PADCFG_DW0 address |
|--------|----------------|--------------------|
| EN (display/backlight) | North `0xFDC20000` | `0xFDC20418` |
| CLK  | North | `0xFDC20470` |
| MOSI | North | `0xFDC20480` |
| RESET| North | `0xFDC20488` |
| D/C  | South `0xFDC50000` | `0xFDC50580` |
| CS   | South | `0xFDC505C8` |

**Driving a pad.** The firmware leaves these pads in **GPIO mode**, so you only
touch two bits of `PADCFG_DW0`:

- **bit 0** = `GPIOTXSTATE` (the output level)
- **bit 9** = `GPIOTXDIS` (1 = output disabled). Clear it to enable the driver.

```python
v = read32(pad)
v &= ~(1 << 9)            # enable output
v = (v & ~1) | (level)    # set level
write32(pad, v)
```

> These exact addresses are specific to the RN426 (`rn426_8`) pad map. Other
> models map different pads. See [`porting.md`](porting.md).

## Step 3: SPI byte transfer

MSB-first, mode 0 (clock idle low, latch on rising edge):

```
CS = 0
DC = 0 (command) or 1 (data)
for bit in 7..0:
    CLK = 0
    MOSI = bit
    CLK = 1            # latched here
DC = 1
CS = 1
```

## Step 4: init sequence

> ⚠️ **Do NOT pulse RESET.** RST (pin 31) is a **shared front-board reset**
> line. It does not just reset the SSD1305, it also resets the **MSP430**
> into a non-button-reporting mode, recoverable only by a full **AC
> power-cycle** (a warm reboot is not enough). Hold **RST high, always**. The
> SSD1305 is already powered by the BIOS before TrueNAS boots, so the command
> sequence below is sufficient to re-init it on its own, no reset needed.

Hold RESET high, then send the 33-byte init as commands (D/C = 0):

```
ae d5 71 a8 1f d9 22 20 02 a1 c8 da 12 d8 00 81 cf b0 d3 00 21 04 83 22 00 03 10 00 40 a6 a4 db 18
```

Decoded (standard SSD130x): `AE` display off · `D5 71` clock divide · `A8 1F`
mux=32 · `D9 22` precharge · `20 02` page addressing · `A1` segment remap ·
`C8` COM scan dir · `DA 12` COM pins · `81 CF` contrast · `D3 00` offset ·
`21 04 83` column range 4–131 · `22 00 03` page range 0–3 · `40` start line ·
`A6` normal · `A4` follow RAM · `DB 18` VCOMH.

Then set EN high and send `0xAF` (display ON). The init sequence intentionally
has *no* display-on; if you forget the `0xAF`, the panel stays blank.

## Step 5: drawing

128×32 = **4 pages × 8 rows**. Each data byte is **8 vertical pixels** of one
column (bit 0 = top row of the page). For each page:

```
cmd(0xB0 | page)     # set page
cmd(0x00 | (col & 0x0F))   # column low nibble
cmd(0x10 | (col >> 4))     # column high nibble
dat(byte) x 132            # one byte per column
```

`rn426_panel.py` renders text with Pillow into a 1-bit 132×32 image and converts
it to this page layout.

## Sleep / wake

> ⚠️ **Never drive EN low.** EN (pin 17) is, like RST, a **shared
> front-board line**. It does not just gate the LCD, it also resets the
> **MSP430** out of button-reporting mode, recoverable only by a full **AC
> power-cycle**. Sleep and wake never touch EN.

- **Sleep:** `cmd(0xAE)` (pixels off) **only**. EN is never driven low. With
  the pixels off there's no image-retention/burn-in; the backlight/MCU power
  stays as it was.
- **Wake:** `cmd(0xAF)` (pixels on) **only**. Because EN was never lowered,
  no re-init is needed, just the one command.
