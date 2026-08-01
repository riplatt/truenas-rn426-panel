# Button protocol (MSP430 over the i801 SMBus)

The 5-way navigation pad is not wired to the host directly. The front-board
TI MSP430G2518 scans the buttons and exposes their state over the Intel
i801 SMBus at address **`0x1C`**.

## Reading buttons

Read **register `0x04`** with an SMBus *read-byte-data* transaction. The result
is an active-high bitmap (bit set = button currently pressed):

| Bit | Mask | Button |
|-----|------|--------|
| 0 | `0x01` | LEFT |
| 1 | `0x02` | RIGHT |
| 2 | `0x04` | UP / TOP |
| 3 | `0x08` | DOWN / BOTTOM |
| 4 | `0x10` | CENTER |

The i801 is SMBus-only. It does not support raw i2c transfers
(`i2ctransfer` fails with *"Adapter does not have I2C transfers capability"*), so
you must use SMBus byte/word ops. From Python without `smbus2`:

```python
import fcntl, ctypes, os
I2C_SLAVE, I2C_SMBUS = 0x0703, 0x0720
class D(ctypes.Structure):
    _fields_ = [("rw",ctypes.c_ubyte),("cmd",ctypes.c_ubyte),
                ("size",ctypes.c_uint),("data",ctypes.c_void_p)]
fd = os.open("/dev/i2c-<i801>", os.O_RDWR); fcntl.ioctl(fd, I2C_SLAVE, 0x1C)
def read_byte_data(reg):
    buf = (ctypes.c_ubyte*34)()
    a = D(1, reg, 2, ctypes.cast(buf, ctypes.c_void_p))  # rw=1(read) size=2(byte-data)
    fcntl.ioctl(fd, I2C_SMBUS, a)
    return buf[0]
```

For edge detection (act once per press): `newly_pressed = current & ~previous`.

## Read on interrupt: do NOT poll `reg 0x04` on a timer

⚠️ **Polling `reg 0x04` on a timer eventually breaks the buttons.** It appears to
work at first, but the MSP430 puts *itself* into a low-power sleep when idle, and
repeatedly reading it over i2c while it is asleep **corrupts its button scanning**
over time (mine died after a few hours of a gentle ~1 Hz poll). Once corrupted,
`reg 0x04` reads `0x00` no matter what you press, and **only a cold power-cycle
recovers it**, the same failure mode as writing `reg 0x02` (below). Polling faster
makes it worse, not better.

The stock firmware never polled: it was interrupt-driven (`button_irq_init` +
`i2cfb_reporter`), reading `reg 0x04` only the instant the MCU asserted its IRQ.
You can reproduce that from userspace, with no kernel module, because the MCU's
interrupt line is a Denverton SoC GPIO pad you can read via `/dev/mem`:

- **INT pad: South GPIO community `0xFDC50000` + offset `0x570`** (pad 46).
  Read **`RXSTATE` = bit 1** of `PADCFG_DW0`. **Active-low**, idle `1` (high),
  pulled `0` (low) on button activity. (This is the same P2SB/SBREG window the LCD
  driver already uses; see [lcd-protocol.md](lcd-protocol.md). Read it only, never
  drive it.)

The pattern: watch the pad at ~100–200 Hz, a microsecond memory read that never
touches the MCU, and only when it goes low do one `read-byte-data` of
`reg 0x04` to learn which button. This catches every press instantly (including the
first press after a long idle, the symptom that started this) and never wedges the
MCU, because i2c is touched only when the MCU is awake and asserting its INT line.

```python
import mmap, struct
mS = mmap.mmap(os.open("/dev/mem", os.O_RDWR), 0x1000, offset=0xFDC50000)
def int_active():                              # True = button activity (line low)
    return ((struct.unpack_from("<I", mS, 0x570)[0]) >> 1) & 1 == 0
```

**Debounce the pulse-train:** the MCU does not hold the line cleanly low, it emits
a burst of pulses while a button is active. Treat it as one press, then re-arm only
after the pad has been continuously **high** for ~50 ms.

### Finding the INT pad on a different board

If your board isn't an RN426/RN526/RN626X, the INT pad may differ. Use the
included finder, [`tools/find-int-pad.py`](../tools/find-int-pad.py):

```bash
sudo systemctl stop rn426-panel        # so the LCD doesn't bit-bang pads
sudo python3 tools/find-int-pad.py     # do nothing, then tap all buttons
```

It sweeps every North + South `PADCFG_DW0` `RXSTATE` bit in two phases, idle vs
pressed, and reports the pad that is *stable while idle* but
*toggles on presses* (and cross-checks `reg 0x04` to confirm the MCU is reporting).
**⚠️ Beware decoys:** on the RN426, North `0xFDC20520` is a **free-running** signal
that toggles even when untouched. It looks like the INT line if you only sample
*during* presses, but it is **not** it. That's why the tool always runs an idle
phase first.

## ⚠️ Do NOT write register `0x02`

`reg 0x02` is the MCU's **LED / control** register (`0x0F` at power-on; it drives
the button backlights). On this firmware it **also gates button scanning**:

- Writing `reg 0x02 = 0x00` turns the backlights off **and disables button
  reporting**. `reg 0x04` then reads `0x00` no matter what you press.
- Writing it back to `0x0F` (or any value) restores the **lights** but **not**
  the scanning.
- The disabled state **survives a warm reboot** (the front board is on standby
  power). **Only a full power-off/on recovers it.**

So treat the MSP430 as **read-only**. This driver never writes it. If your
buttons are dead from earlier experimentation, do one cold power-cycle.

## Other registers (observed)

| Reg | Value | Notes |
|-----|-------|-------|
| `0x00` | `0x46` | ID / version |
| `0x01` | status | not the button bitmap |
| `0x02` | `0x0F` | LED / control (see warning above) |
| `0x04` | bitmap | **buttons** |
| `0x05` | `0x0C` | unknown |

A DS1307-class RTC also sits on this SMBus at **`0x44`**.
