#!/usr/bin/env python3
"""
RN426/RN428 front-panel driver for TrueNAS SCALE on the NETGEAR ReadyNAS RN426
and RN428 (one board, two models -- the stock firmware drives both from the
same "rn426_8" config struct).

Also carries EXPERIMENTAL support for the ReadyNAS 528X/628X (C224 chipset,
`gpio_ich`). That path is stage-gated bring-up: it has not been confirmed on
real hardware, buttons and LCD both included. See docs/porting.md for what
each stage checks. Everything else in this docstring (the "only" language
below, the register details) describes the RN426/RN428 path, which is the
one this driver was originally verified against.

NOT for any other model. The two backends above write hard-coded SoC
register addresses (Denverton PADCFG_DW0 via /dev/mem, or ICH I/O ports via
/dev/port); on a different chipset either one pokes unrelated hardware (see
issue #4 and docs/porting.md). The driver refuses to start on unrecognized
hardware; RN_MODEL=rn426, RN_MODEL=rnx26 or RN_MODEL=rn316 overrides the
check if you know better.

It drives the 128x32 SSD1305 graphic LCD by bit-banging SPI over SoC GPIO,
and reads the 5-way navigation buttons from the front-board MSP430
microcontroller over the Intel i801 SMBus. No kernel module required.

See docs/ for the full reverse-engineering writeup and protocol details.

Usage:  rn426_panel.py [run|sleep|wake]
Env:    RN_SLEEP = idle seconds before the display sleeps (default 90; 0 = never)
        RN_MODEL = force model detection (rn426 / rnx26 / rn316)

Requires: python3-pil (Pillow) and the DejaVu fonts (both ship with TrueNAS SCALE),
          i2c-dev + i2c-i801 kernel modules, and root (for /dev/mem, /dev/port, i2c).
"""
import mmap, struct, time, socket, subprocess, os, ctypes, glob, sys, re
try:
    import fcntl                        # POSIX only; needed by Buttons (real i2c hardware)
except ImportError:
    fcntl = None                        # lets this module import (e.g. for tests) on Windows

# --------------------------------------------------------------------------
# P2SB unhide -- pure Python PCI-config write via /dev/port (no helper binary).
# The Denverton GPIO community registers live behind the P2SB/SBREG window,
# which the BIOS hides. Clearing bit 0 of 00:1f.1 cfg reg 0xE1 reveals it.
# Only the Dnv backend needs this; the Ich backend's registers aren't hidden.
# --------------------------------------------------------------------------
def _pci_cfg_write_byte(bus, dev, fn, reg, val):
    addr = 0x80000000 | (bus << 16) | (dev << 11) | (fn << 8) | (reg & 0xFC)
    with open("/dev/port", "r+b", 0) as p:
        p.seek(0xCF8); p.write(struct.pack("<I", addr))
        p.seek(0xCFC + (reg & 3)); p.write(bytes([val & 0xFF]))

def p2sb_unhide():
    try:
        _pci_cfg_write_byte(0, 0x1F, 1, 0xE1, 0x00)
    except Exception as e:
        print("p2sb_unhide failed:", e, file=sys.stderr)

# --------------------------------------------------------------------------
# Gpio seam. Two backends, one interface: set(sig, val) drives one of the six
# signals CS/CLK/MOSI/DC/RST/EN; int_active() reads the MCU interrupt line;
# idle() (shared here) parks the SPI lines in their safe idle state. RST and
# EN are never driven low except where the Dnv backend's init() explicitly
# does so for EN -- see en_can_pulse below.
# --------------------------------------------------------------------------
class Gpio:
    has_int_line = True   # overridden False by backends/models with no known MCU/chip interrupt line

    def idle(self):
        for sig, v in [("CS", 1), ("RST", 1), ("CLK", 0), ("MOSI", 0), ("DC", 0)]:
            self.set(sig, v)

# --------------------------------------------------------------------------
# DnvMmioGpio: bit-bang SPI on Denverton PADCFG_DW0 registers via /dev/mem.
#   North GPIO community base 0xFDC20000, South 0xFDC50000 (PADBAR 0x400).
#   Drive a pad = clear bit 9 (GPIOTXDIS -> output enable) + set bit 0 (TX value).
#   The firmware leaves these pads in GPIO mode, so only those two bits matter.
# --------------------------------------------------------------------------
class DnvMmioGpio(Gpio):
    """RN426/RN428 backend: Denverton (Atom C3000) SoC GPIO via /dev/mem."""
    NORTH, SOUTH = 0xFDC20000, 0xFDC50000
    en_can_pulse = True   # Dnv init() pulses EN low then high; see LCD.init()

    def __init__(self, spec=None):
        # spec (the model's whole MODELS[...] dict) is accepted and ignored,
        # so _build can construct every backend the same way regardless of
        # what a given model's spec carries: spec["backend"](spec). The Dnv
        # backend's six PADCFG addresses below are hardcoded to this one
        # board, not derived from a pin map or an lpc_id/ngpio pair.
        p2sb_unhide()
        self._fN = open("/dev/mem", "r+b"); self.mN = mmap.mmap(self._fN.fileno(), 0x1000, offset=self.NORTH)
        self._fS = open("/dev/mem", "r+b"); self.mS = mmap.mmap(self._fS.fileno(), 0x1000, offset=self.SOUTH)
        # (mmap, PADCFG_DW0 offset within the community)
        self.EN   = (self.mN, 0x418)   # display / backlight enable
        self.CLK  = (self.mN, 0x470)   # SPI clock
        self.MOSI = (self.mN, 0x480)   # SPI data
        self.RST  = (self.mN, 0x488)   # controller reset
        self.DC   = (self.mS, 0x580)   # data/command
        self.CS   = (self.mS, 0x5c8)   # chip select

    def _set(self, pad, val):
        m, o = pad
        v = struct.unpack_from("<I", m, o)[0]
        v &= ~(1 << 9)                  # GPIOTXDIS = 0 (enable output driver)
        v = (v & ~1) | (val & 1)        # GPIOTXSTATE = val
        struct.pack_into("<I", m, o, v)

    def set(self, sig, val):
        self._set(getattr(self, sig), val)

    def int_active(self):
        # MCU interrupt line = Denverton SOUTH GPIO community PADCFG_DW0 0xFDC50570
        # (pad 46), RXSTATE = bit 1, ACTIVE-LOW: idle high, pulled low on button
        # activity. Found empirically by sweeping every N+S SoC pad idle-vs-press
        # (NB: North 0x520 is a free-running decoy -- do not use it). Watching this
        # pad is a cheap /dev/mem read that never pokes the MCU, so we can touch
        # i2c (reg 0x04) ONLY when the MCU is awake with a press -- and never poll
        # a deep-slept MCU, which is what corrupts its button reporting. Read only.
        return ((struct.unpack_from("<I", self.mS, 0x570)[0]) >> 1) & 1 == 0

# --------------------------------------------------------------------------
# IchPortGpio: bit-bang SPI on the ICH/PCH I/O-port GPIO block (gpio_ich),
# used by the 528X/628X (C224 chipset). Register facts below are verified
# against mainline gpio-ich.c (LPC_LPT/ICH_V5, 76 lines), NOT against real
# 52x/62x hardware -- see docs/porting.md for what stage does that.
# --------------------------------------------------------------------------
def _ich_line_addr(gpiobase, line):
    """gpio_ich line number -> (io port, bit) for a byte-granular GP_LVL
    access. GP_LVL banks sit at GPIOBASE+0x0C (bank0), +0x38 (bank1),
    +0x48 (bank2); line n is bank n//32, bit n%32. Pure math, no I/O --
    kept as a free function so it's testable without opening any device."""
    bank, bit = divmod(line, 32)
    port = gpiobase + (0x0C, 0x38, 0x48)[bank] + (bit // 8)
    return port, bit % 8

def _parse_gpiobase(cfg_bytes):
    """Pull GPIOBASE out of a 00:1f.0 PCI config-space byte blob: dword at
    offset 0x48, masked to the I/O-port bits (0x0000ff80)."""
    if len(cfg_bytes) < 0x4C:
        raise RuntimeError("PCI config read too short to contain GPIOBASE (offset 0x48)")
    base = struct.unpack_from("<I", cfg_bytes, 0x48)[0] & 0x0000ff80
    if base == 0:
        raise RuntimeError("GPIOBASE is 0 -- LPC GPIO I/O space not enabled, check BIOS/ACPI settings")
    return base

def _check_lpc_id(actual, expected):
    """Pure check, no I/O: the LPC bridge's PCI device id must match the id
    this model's pin map was derived for. A pin map is meaningless (and
    dangerous to write) on a different chipset -- catch that BEFORE opening
    /dev/port, not after the first write."""
    if actual != expected:
        raise RuntimeError(
            "LPC device id 0x%04x does not match this model's expected "
            "0x%04x -- wrong pin map for this hardware, refusing to guess"
            % (actual, expected))

def _check_gpio_en(gc_byte):
    """Pure check, no I/O: bit 4 (GPIO_EN) of the LPC Configuration (GC)
    register at PCI config offset 0x4C must be set, or the gpio_ich I/O-port
    window this driver writes isn't even decoded by the chipset."""
    if not (gc_byte & 0x10):
        raise RuntimeError("GPIO decode disabled (LPC cfg 0x4C bit 4 clear)")

def _check_ngpio(pins, ngpio):
    """Pure check, no I/O: every configured gpio_ich line (BTN_INT included,
    when it isn't None) must be < ngpio, the line count this chipset's
    gpio_ich block actually implements -- otherwise _ich_line_addr would
    compute a bank/byte that doesn't exist on this hardware."""
    for name, line in pins.items():
        if line is not None and line >= ngpio:
            raise ValueError(
                "pin %s uses gpio_ich line %d but this model has only %d "
                "lines (ngpio)" % (name, line, ngpio))

class IchPortGpio(Gpio):
    """ICH/PCH I/O-port GPIO backend (gpio_ich), NOT the Denverton PADCFG_DW0
    the RN426 backend uses. Shared by every gpio_ich-based model (rnx26,
    rn316, ...); what differs per model is just the pin map passed in.

    This driver only ever touches GP_LVL (the output/level register) --
    never USE_SEL/IO_SEL/GP_RST_SEL or the blink registers. Bring-up tooling
    (docs/porting.md) is responsible for confirming those are already
    configured as GPIO output/input before this driver runs.

    CRITICAL INVARIANT: EN and RESET must never be driven low. On the RN426
    front-board MSP430, and assumed by extension on every other model that
    shares that MCU family, driving either line low wedges button-reporting,
    recoverable only by a full AC power-cycle. So this backend derives, from
    the pin map, every GP_LVL byte that contains EN or RST and unconditionally
    forces those bits high on EVERY write to that byte, no matter what was
    asked for -- see _derive_maps/_wr_byte. set("EN", 0) and set("RST", 0)
    are thereby no-ops. en_can_pulse=False also tells LCD.init() not to even
    attempt to lower EN.
    """
    en_can_pulse = False
    SIGS = ("MOSI", "CLK", "DC", "CS", "EN", "RST")   # the six output signals

    def __init__(self, spec):
        # spec is the model's whole MODELS[...] dict; this backend only
        # looks at "pins", "lpc_id" and "ngpio". Passing the whole dict
        # (rather than three positional args) keeps _build's construction
        # uniform across backends and leaves room for the spec to grow.
        pins = spec["pins"]
        # pins: {"MOSI","CLK","DC","CS","EN","RST","BTN_INT"} -> gpio_ich
        # line number, per model (see MODELS). BTN_INT may be None if no
        # interrupt line is known for this model (buttons unavailable).
        self.pins = pins
        self.has_int_line = pins["BTN_INT"] is not None
        self.ngpio = spec["ngpio"]

        # Identity check BEFORE opening /dev/port: refuse to bit-bang a
        # chipset this pin map wasn't derived for.
        with open("/sys/bus/pci/devices/0000:00:1f.0/device") as f:
            device = int(f.read().strip(), 16)
        _check_lpc_id(device, spec["lpc_id"])

        with open("/sys/bus/pci/devices/0000:00:1f.0/config", "rb") as f:
            cfg = f.read(0x4D)          # covers GPIOBASE (0x48) and GC (0x4C)
        if len(cfg) < 0x4D:
            raise RuntimeError("PCI config read too short to contain the GC register (offset 0x4C)")
        self.gpiobase = _parse_gpiobase(cfg)
        _check_gpio_en(cfg[0x4C])

        self._pf = open("/dev/port", "r+b", buffering=0)
        self._fd = self._pf.fileno()
        self._derive_maps()

    def _derive_maps(self):
        # Everything the choke point needs, derived once from self.pins:
        #   _sig:       signal name -> (port, bit) for the six outputs.
        #   _whitelist: the set of GP_LVL bytes those six outputs live in --
        #               the ONLY bytes _wr_byte will accept.
        #   _force:     port -> bitmask that must be ORed into every write
        #               to that port, built from EN's and RST's bits. This
        #               is what turns the CRITICAL INVARIANT above into
        #               "any byte containing EN or RST" instead of a
        #               hardcoded "bank0 byte0, bits 6|7".
        # BTN_INT is read-only and deliberately left out of _sig/_whitelist;
        # reads aren't restricted by the choke point either way.
        _check_ngpio(self.pins, self.ngpio)
        self._sig = {s: _ich_line_addr(self.gpiobase, self.pins[s]) for s in self.SIGS}
        self._whitelist = {port for port, _bit in self._sig.values()}
        self._force = {}
        for s in ("EN", "RST"):
            port, bit = self._sig[s]
            self._force[port] = self._force.get(port, 0) | (1 << bit)

    def _rd_byte(self, port):
        return os.pread(self._fd, 1, port)[0]

    def _rd_dword(self, port):
        # Not used by the driver itself (writes are byte-granular only);
        # kept for bring-up/diagnostic reads alongside tools/rn-probe.sh.
        return struct.unpack("<I", os.pread(self._fd, 4, port))[0]

    def _wr_byte(self, port, val):
        # Choke point: this is the ONLY place that writes /dev/port, and it
        # only accepts whitelisted bytes, with the EN/RESET force mask
        # applied right here -- so no future caller (set(), a bring-up
        # script, whatever) can write one of these bytes without it.
        if port not in self._whitelist:
            raise ValueError("refusing to write non-whitelisted ICH GPIO port 0x%x" % port)
        val |= self._force.get(port, 0)
        os.pwrite(self._fd, bytes([val & 0xFF]), port)

    def set(self, sig, val):
        port, bit = self._sig[sig]
        cur = self._rd_byte(port)
        if val & 1:
            cur |= (1 << bit)
        else:
            cur &= ~(1 << bit)
        self._wr_byte(port, cur & 0xFF)   # force mask applied inside _wr_byte

    def int_active(self):
        btn_int = self.pins["BTN_INT"]
        if btn_int is None:
            # No interrupt line known for this model -- buttons disabled,
            # this driver is display-only here.
            return False
        # Active-low: CONFIRMED on rn316 (this is the SX8635's NIRQ pad --
        # the stock driver's sx8635_get_nirq_state treats a raw 0 read as
        # active, and tools/sx8635-watch.py observed the line go 0->1 right
        # after an IrqSrc read on real hardware). Still UNCONFIRMED on
        # rnx26 -- check GPI_INV in the stage-A register dump
        # (docs/porting.md) before trusting this there.
        port, bit = _ich_line_addr(self.gpiobase, btn_int)
        return ((self._rd_byte(port) >> bit) & 1) == 0

# SSD1305 init (33 bytes). NOTE: contains no display-on; 0xAF is sent after.
INIT_SEQ = bytes.fromhex("aed571a81fd9222002a1c8da12d80081cfb0d300210483220003100040a6a4db18")

# --------------------------------------------------------------------------
# LCD: SSD1305 driving logic, backend-agnostic. Takes a Gpio and drives it;
# geometry (pages, columns) and the init byte sequence come from MODELS.
# --------------------------------------------------------------------------
class LCD:
    def __init__(self, gpio, geometry, init_seq):
        self.gpio = gpio
        self.pages, self.cols = geometry
        self.init_seq = init_seq
        from PIL import ImageFont
        self.f1 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 16)
        self.f2 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 13)

    def _spi(self, byte, dc):
        self.gpio.set("CS", 0); self.gpio.set("DC", dc)
        for k in range(7, -1, -1):                       # MSB first
            self.gpio.set("CLK", 0)
            self.gpio.set("MOSI", (byte >> k) & 1)
            self.gpio.set("CLK", 1)                       # latch on rising edge
        self.gpio.set("DC", 1); self.gpio.set("CS", 1)

    def cmd(self, b): self._spi(b, 0)
    def dat(self, b): self._spi(b, 1)

    def init(self):
        self.gpio.idle()
        if self.gpio.en_can_pulse:
            self.gpio.set("EN", 0)
        # IMPORTANT: do NOT pulse RST. Pin 31 (RST) is a *shared front-board reset*
        # that also resets the MSP430 into a non-button-reporting mode, which only a
        # full power-cycle recovers. The SSD1305 is already powered (the BIOS used
        # it), so the command sequence alone re-inits it -- we just hold RST high.
        for b in self.init_seq:
            self.cmd(b)
        self.gpio.set("EN", 1)
        self.cmd(0xAF)                                   # display ON

    def sleep(self):
        # Pixels OFF only. Do NOT drive EN (pin 17) low: like RST, pin 17 is a
        # shared front-board line and holding it low resets the MSP430 out of
        # button-reporting mode (recoverable only by a power cycle). 0xAE blanks
        # the pixels (kills burn-in); the backlight stays on so the MCU is safe.
        self.cmd(0xAE)

    def wake(self):
        self.cmd(0xAF)                                   # pixels ON (EN was never lowered, no re-init needed)

    def show(self, img):
        px = img.load()
        for page in range(self.pages):                   # pages x 8 rows tall
            self.cmd(0xB0 | page); self.cmd(0x00); self.cmd(0x10)
            for c in range(self.cols):
                byte = 0
                for r in range(8):
                    if px[c, page * 8 + r]:
                        byte |= (1 << r)
                self.dat(byte)

    def lines(self, l1, l2):
        from PIL import Image, ImageDraw
        img = Image.new("1", (self.cols, self.pages * 8), 0); d = ImageDraw.Draw(img)
        d.text((4, -2), l1, font=self.f1, fill=1)
        d.text((4, 17), l2, font=self.f2, fill=1)
        self.show(img)

# --------------------------------------------------------------------------
# Buttons: two independent front-panel protocols, picked by
# MODELS[...]["buttons"] (never derived from BTN_INT/has_int_line -- see
# _button_class). Both classes take the model's Gpio in __init__ and call
# gpio.int_active() themselves, and both expose events(now) -> list of
# action strings drawn from {"NEXT", "PREV", "OK"}, so run() is backend-
# agnostic: it does not know or care which protocol produced an action.
#
#   "msp430" -- Msp430Buttons: RN426/RNx26 front-board TI MSP430 over the
#     i801 SMBus. reg 0x04 = active-high button bitmap. READ ONLY.
#     IMPORTANT: do NOT write the MCU's reg 0x02 (LED/control). On this
#     firmware it also gates button scanning, and once disabled it only
#     recovers on a full power-cycle (a warm reboot is not enough). This
#     driver never writes the MCU.
#   "sx8635" -- Sx8635Buttons: RN316 front-board Semtech SX8635 touch-wheel
#     controller over the i801 SMBus. See that class's docstring.
# --------------------------------------------------------------------------
I2C_SLAVE, I2C_SMBUS = 0x0703, 0x0720
class _smbus_ioctl(ctypes.Structure):
    _fields_ = [("rw", ctypes.c_ubyte), ("cmd", ctypes.c_ubyte),
                ("size", ctypes.c_uint), ("data", ctypes.c_void_p)]

def find_i801_bus():
    """Find the i801 SMBus adapter by name -- the /dev/i2c-N number is NOT
    stable across reboots (iSMT and i801 can swap)."""
    for d in sorted(glob.glob("/sys/class/i2c-dev/i2c-*")):
        try:
            if "I801" in open(os.path.join(d, "name")).read():
                return int(d.rsplit("-", 1)[1])
        except OSError:
            pass
    return 1

class Msp430Buttons:
    """RN426/RNx26 front-board TI MSP430 over the i801 SMBus. See the
    module-level Buttons comment above for the reg 0x02 write hazard."""
    LEFT, RIGHT, UP, DOWN, CENTER = 0x01, 0x02, 0x04, 0x08, 0x10
    ADDR, REG = 0x1C, 0x04

    def __init__(self, gpio):
        self.gpio = gpio
        self.fd = os.open("/dev/i2c-%d" % find_i801_bus(), os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, self.ADDR)
        self.prev = 0
        # Debounce state for events(): one reg 0x04 read per interrupt-pad
        # assertion, re-armed only once the pad has been continuously
        # inactive for >= 50 ms (see docs/buttons-protocol.md).
        self._armed = True
        self._high_since = time.time()

    def _read(self, reg):
        buf = (ctypes.c_ubyte * 34)()
        a = _smbus_ioctl(1, reg, 2, ctypes.cast(buf, ctypes.c_void_p))
        fcntl.ioctl(self.fd, I2C_SMBUS, a)
        return buf[0]

    def pressed(self):
        """Bitmap of buttons newly pressed since the last call (rising
        edge). Kept for tools/find-int-pad.py compatibility; events() below
        is what run() uses."""
        try:
            v = self._read(self.REG)
        except OSError:
            return 0
        new = v & ~self.prev
        self.prev = v
        return new

    def events(self, now):
        """Watch the MCU interrupt pad via the Gpio backend -- a cheap read
        that does NOT touch i2c. We read the MCU (reg 0x04) ONLY when this
        pad signals a press, i.e. only when the MCU is awake, and only once
        per assertion (armed/re-armed below). We never poll a sleeping MCU,
        so its button reporting is never corrupted (the old i2c-poll loop
        did that). UP->PREV, DOWN->NEXT, CENTER->OK. LEFT/RIGHT emit their
        own "LEFT"/"RIGHT" actions: run() treats those (and any action it
        doesn't recognize) as activity-only -- they reset the idle timer
        and wake a sleeping display like any other action, but never move
        a page, matching the RN426's original behaviour (any nonzero
        bitmap counted as activity). Never writes reg 0x02 (see class
        comment)."""
        actions = []
        if self.gpio.int_active():
            self._high_since = None
            if self._armed:                     # one event per physical press
                self._armed = False
                try:
                    v = self._read(self.REG)
                except OSError:
                    v = 0
                if v:
                    if v & self.LEFT:   actions.append("LEFT")
                    if v & self.RIGHT:  actions.append("RIGHT")
                    if v & self.UP:     actions.append("PREV")
                    if v & self.DOWN:   actions.append("NEXT")
                    if v & self.CENTER: actions.append("OK")
                    if not actions:     actions.append("ACTIVITY")   # unknown bits still counted as activity, as before
        else:
            # pad idle (high). Re-arm once it has been stable-high briefly,
            # to debounce the MCU's pulse-train (one physical press -> one event).
            if self._high_since is None:
                self._high_since = now
            elif now - self._high_since >= 0.05:
                self._armed = True
        return actions

Buttons = Msp430Buttons   # alias kept for external users/backward compatibility
                           # (tools/find-int-pad.py does not import this -- it has its own reg04())

# --------------------------------------------------------------------------
# Pure wheel/button math for Sx8635Buttons -- no I/O, testable standalone.
# --------------------------------------------------------------------------
def _wheel_delta(pos, prev, wheel_range):
    """Wrap-aware signed delta from prev to pos on a ring of wheel_range
    positions (0..wheel_range-1 wrapping around). Result is in
    [-wheel_range//2, wheel_range//2)."""
    half = wheel_range // 2
    return ((pos - prev + half) % wheel_range) - half

def _wheel_emit(acc, detent):
    """Consume a signed raw-tick accumulator into whole-detent NEXT/PREV
    events, returning (events, remaining_acc). Clockwise = position
    DECREASING = negative acc = NEXT; counter-clockwise = positive acc =
    PREV (confirmed on real rn316 hardware, see MODELS comment)."""
    events = []
    while acc <= -detent:
        events.append("NEXT")
        acc += detent
    while acc >= detent:
        events.append("PREV")
        acc -= detent
    return events, acc

def _rising_actions(bitmap, prev_bitmap, keys):
    """Map newly-set bits in reg 0x02 (bitmap vs prev_bitmap) through
    `keys` ({bit-mask: action}). Empty `keys` (provisional, see MODELS) ==
    "any newly set bit = one OK", regardless of how many bits rose at
    once."""
    rising = bitmap & ~prev_bitmap
    if not rising:
        return []
    if not keys:
        return ["OK"]
    return [action for mask, action in keys.items() if rising & mask]

class Sx8635Buttons:
    """RN316 front-board Semtech SX8635 capacitive touch-wheel controller,
    i801 SMBus address 0x2b. STRICTLY READ-ONLY: the chip keeps its
    calibration in volatile SPM (reloaded from NVM/QSM at power-up); on the
    one RN316 tested SpmStat NvmValid=0 (factory QSM defaults) yet buttons
    AND the wheel both work read-only, so this class contains NO write
    helper at all -- there is no SPM load and no NVM write path to
    accidentally trigger. NETGEAR's own NVM burn sequence is 0xAC/0xAD
    (unlock key pair) then 0xA5/0x5A pulsed into 0x0E, and its soft reset
    is 0xB1; this driver writes none of that, ever.

    Never run this alongside tools/sx8635-watch.py: both read IrqSrc
    (reg 0x00), which clears on read, so two readers steal each other's
    events.

    Register facts (see .claude/advice/sx8635-re-report.md and the tester's
    real-hardware run): reading reg 0x00 (IrqSrc) clears NIRQ; bit 2 =
    buttons (read reg 0x02, a touch bitmap); bit 3 = wheel (position =
    (reg0x03<<8)|reg0x04, reg 0x01 bit 0x10 = wheel currently touched).
    Wheel bit->pad mapping in reg 0x02 is NOT yet known (mapping run
    pending) -- see MODELS["rn316"]["sx8635"]["keys"].
    """
    REG_IRQSRC, REG_CAPSTAT_MSB, REG_CAPSTAT_LSB = 0x00, 0x01, 0x02
    REG_POS_MSB, REG_POS_LSB = 0x03, 0x04
    WHEEL_TOUCHED = 0x10   # reg 0x01 bit 4
    IRQ_BUTTONS, IRQ_WHEEL = 0x04, 0x08   # reg 0x00 bits 2, 3
    SAFETY_INTERVAL = 1.0                 # seconds; read even with no NIRQ/no touch
    WHEEL_POLL_HZ_INTERVAL = 1.0 / 50     # poll >= 50 Hz while the wheel is touched

    def __init__(self, gpio, spec):
        self.gpio = gpio
        self.addr = spec["addr"]
        self.wheel_range = spec["wheel_range"]
        self.detent = spec["detent"]
        self.keys = spec["keys"]
        self.fd = os.open("/dev/i2c-%d" % find_i801_bus(), os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, self.addr)
        self.prev_bitmap = 0
        self.wheel_touched = False
        self.wheel_prev_pos = None
        self.wheel_acc = 0
        self.wheel_stale_since = None
        self.last_read = 0.0
        # First read: confirm the chip actually answers (raises OSError on
        # NAK/missing bus, which _build_buttons treats as "degrade, don't
        # exit" -- see there).
        self._read(self.REG_IRQSRC)

    def _read(self, reg):
        buf = (ctypes.c_ubyte * 34)()
        a = _smbus_ioctl(1, reg, 2, ctypes.cast(buf, ctypes.c_void_p))
        assert a.rw == 1   # read-byte-data direction only; this class never writes
        fcntl.ioctl(self.fd, I2C_SMBUS, a)
        return buf[0]

    def events(self, now):
        """Read when gpio.int_active(), or when the wheel was touched and
        we haven't polled it in WHEEL_POLL_HZ_INTERVAL (keeps polling at
        >= 50 Hz while touched, since NIRQ may not re-assert for continued
        motion), or every SAFETY_INTERVAL regardless (robustness if the
        int-line gating assumption is ever wrong -- buttons still work,
        just laggier).

        IMPORTANT: IrqSrc reading 0 does NOT mean "nothing happening" --
        the real hardware trace shows the chip returns IrqSrc == 0 between
        wheel events while a finger is still on the ring (our own >=50 Hz
        sub-polling outruns the chip's scan period). So an irq==0 read must
        NOT reset the wheel latch/prev-position/accumulator: as long as
        wheel_touched is set, the wheel branch below still runs regardless
        of what IrqSrc said, and reads the same unchanged position (a
        harmless zero-delta) until either a real move or the release
        shows up via reg 0x01 bit 0x10 clearing in _read_wheel. An earlier
        version reset on every irq==0 read, which made every position look
        like the first ever seen and the wheel never emitted anything on
        real hardware -- see .claude/advice/buttons-integration-review.md.
        """
        due = self.gpio.int_active() \
            or (self.wheel_touched and now - self.last_read >= self.WHEEL_POLL_HZ_INTERVAL) \
            or (now - self.last_read >= self.SAFETY_INTERVAL)
        if not due:
            return []
        self.last_read = now
        try:
            irq = self._read(self.REG_IRQSRC)
            actions = []
            if (irq & self.IRQ_WHEEL) or self.wheel_touched:
                actions += self._read_wheel()
            if irq & self.IRQ_BUTTONS:
                actions += self._read_buttons()
            if irq:
                self.wheel_stale_since = None
            elif self.wheel_touched:
                # Belt, not the primary release path (that's _read_wheel
                # seeing reg 0x01 bit 0x10 clear): if IrqSrc reads 0 on
                # every poll for a full 5s while still "latched touched",
                # something is wrong (a missed release, a wedged chip) --
                # drop the latch rather than sub-poll forever. Deliberately
                # NOT triggered by a single zero read (see docstring above).
                if self.wheel_stale_since is None:
                    self.wheel_stale_since = now
                elif now - self.wheel_stale_since >= 5.0:
                    self.wheel_touched = False
                    self.wheel_prev_pos = None
                    self.wheel_acc = 0
                    self.wheel_stale_since = None
            return actions
        except OSError:
            # Covers the IrqSrc read above and any follow-up read inside
            # _read_wheel/_read_buttons -- a NAK partway through a read
            # sequence (e.g. i2c-i801 EBUSY under ACPI contention) must not
            # kill the daemon, just skip this tick.
            return []

    def _read_buttons(self):
        bitmap = self._read(self.REG_CAPSTAT_LSB)
        if not self.keys:
            # Provisional (see MODELS/class doc): treat "touch began"
            # (bitmap was all-zero, now isn't) as one OK, not "any bit
            # rose". The OK-phase trace shows bleed bits rising 2-3x across
            # a single physical press (0x01 -> 0x03 -> 0x23) as the finger
            # settles on the pad, which would otherwise fire OK that many
            # times for one press. Once `keys` is filled in from the
            # mapping run, the mapped path should likewise decide on
            # "first rise from an all-zero bitmap" rather than "every
            # rising bit" -- to be confirmed against that run's data.
            rising = ["OK"] if (self.prev_bitmap == 0 and bitmap != 0) else []
        else:
            rising = _rising_actions(bitmap, self.prev_bitmap, self.keys)
        self.prev_bitmap = bitmap
        if not self.keys and self.wheel_touched:
            # Provisional: the tester's data shows reg 0x02 bit 0 sets
            # during almost any touch, including wheel touches. With an
            # empty (unmapped) keys table we can't tell a real button from
            # that noise, so suppress the "any bit = OK" fallback while the
            # wheel is latched touched. Once keys are filled in from a
            # mapping run, a real button's own mask still rises
            # independently and this suppression no longer applies to it.
            return []
        return rising

    def _read_wheel(self):
        cap_msb = self._read(self.REG_CAPSTAT_MSB)
        touched = bool(cap_msb & self.WHEEL_TOUCHED)
        actions = []
        if touched:
            msb = self._read(self.REG_POS_MSB)
            lsb = self._read(self.REG_POS_LSB)
            pos = (msb << 8) | lsb
            if pos >= self.wheel_range:
                pass   # glitch position (observed 0x3b at a wrap) -- ignore, don't update prev
            elif not self.wheel_touched or self.wheel_prev_pos is None:
                self.wheel_prev_pos = pos   # first valid position after a touch starts: just record it
            else:
                delta = _wheel_delta(pos, self.wheel_prev_pos, self.wheel_range)
                self.wheel_prev_pos = pos
                self.wheel_acc += delta
                events, self.wheel_acc = _wheel_emit(self.wheel_acc, self.detent)
                actions += events
            self.wheel_touched = True
        else:
            self.wheel_touched = False
            self.wheel_prev_pos = None
            self.wheel_acc = 0
        return actions

# --------------------------------------------------------------------------
# Info pages -- each returns (line1, line2)
# --------------------------------------------------------------------------
def _sh(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=4).strip()
    except Exception:
        return ""

def _ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "no-ip"

def page_host():
    return (socket.gethostname()[:12], _ip())

def page_pool():
    o = _sh("zpool list -H -o name,health,cap")
    if o:
        p = o.splitlines()[0].split()
        return ("Pool " + p[0][:7], "%s %s" % (p[1][:7], p[2]))
    return ("Pool", "n/a")

def page_temp():
    t = fan = ""
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            if open(hw + "/name").read().strip() == "coretemp":
                t = "CPU %dC" % max(int(open(f).read()) // 1000 for f in glob.glob(hw + "/temp*_input"))
        except Exception:
            pass
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            for f in sorted(glob.glob(hw + "/fan*_input")):
                r = int(open(f).read())
                if r > 0:
                    fan = "Fan %drpm" % r; break
            if fan:
                break
        except Exception:
            pass
    return (t or "CPU ?", fan or "Fan ?")

def page_uptime():
    up = float(open("/proc/uptime").read().split()[0])
    d, h, m = int(up // 86400), int((up % 86400) // 3600), int((up % 3600) // 60)
    la = open("/proc/loadavg").read().split()[0]
    return ("Up %dd %dh" % (d, h) if d else "Up %dh %dm" % (h, m), "Load " + la)

PAGES = [page_host, page_pool, page_temp, page_uptime]

# --------------------------------------------------------------------------
# Model table + selection. Adding a model means adding a row here plus,
# if it needs one, a new Gpio subclass above -- nothing else changes.
# --------------------------------------------------------------------------
MODELS = {
    "rn426": {
        "backend": DnvMmioGpio,
        "geometry": (4, 132),
        "init_seq": INIT_SEQ,
        "pins": None,   # Dnv backend ignores pins, see DnvMmioGpio.__init__
        "buttons": "msp430",
    },
    "rnx26": {
        # ReadyNAS 528X/628X (C224 chipset, gpio_ich). EXPERIMENTAL: this
        # reuses the RN426 init table and geometry as-is. The mux and
        # column-addressing bytes are UNVERIFIED against a real 52x/62x
        # SSD130x panel -- stage C of the bring-up either confirms this
        # table or replaces it. See docs/porting.md section 3.
        "backend": IchPortGpio,
        "geometry": (4, 132),
        "init_seq": INIT_SEQ,
        # gpio_ich line numbers, rnx26 config struct (docs/porting.md).
        "pins": {"MOSI": 54, "CLK": 1, "DC": 32, "CS": 50, "EN": 6, "RST": 7, "BTN_INT": 2},
        "lpc_id": 0x8c54,   # C224 "Lynx Point" LPC bridge
        "ngpio": 76,        # gpio-ich.c ICH_V9 line count for this PCH
        "buttons": "msp430",
    },
    "rn316": {
        # RN316 (Atom D2701, ICH10 LPC 8086:3a18, gpio_ich 61 lines). Pin map
        # taken from the stock firmware rn316 config struct (docs/porting.md),
        # confirmed on real hardware (docs, .claude/advice/sx8635-re-report.md).
        # Init table is reused from the RN426 and UNVERIFIED on this panel,
        # though display output has been confirmed working on one unit.
        # Buttons are the SX8635 captouch wheel at 0x2b, not an MSP430 --
        # see Sx8635Buttons. BTN_INT=2 is its NIRQ line, active-low,
        # confirmed on real hardware (see IchPortGpio.int_active).
        "backend": IchPortGpio,
        "geometry": (4, 132),
        "init_seq": INIT_SEQ,
        "pins": {"MOSI": 21, "CLK": 19, "DC": 16, "CS": 7, "EN": 32, "RST": 24, "BTN_INT": 2},
        "lpc_id": 0x3a18,   # ICH10 LPC bridge
        "ngpio": 61,        # gpio-ich.c ICH_V5/ICH10 line count
        "buttons": "sx8635",
        "sx8635": {
            "addr": 0x2b,
            "wheel_range": 0x1f,   # observed raw position range per full turn (one outlier
                                   # at 0x3b seen at a wrap -- Sx8635Buttons ignores pos >= this)
            "detent": 4,
            # reg 0x02 bit-mask -> action. EMPTY = provisional: a mapping run
            # to identify which bit is which physical pad is still pending;
            # until then, any newly-set bit in 0x02 is treated as "OK". Fill
            # in e.g. {0x01: "OK", 0x02: "BACKUP"} once that run reports back.
            "keys": {},
        },
    },
}

def detect_model(cpuinfo_reader=None, dmi_reader=None):
    """Pick a MODELS key: RN_MODEL env wins if set, else /proc/cpuinfo (Atom
    C3xxx -> rn426), else DMI product_name (ReadyNAS 528X/628X -> rnx26).
    Refuses (sys.exit) rather than guessing on unrecognized hardware: the Dnv
    backend writes hard-coded PADCFG addresses and the Ich backend writes
    hard-coded I/O ports, and on the wrong chipset either is a blind register
    poke. cpuinfo_reader/dmi_reader are injection points for tests only --
    each, if given, replaces the "read the real file" step with a callable
    returning its contents (or raising OSError to simulate a missing file)."""
    env = os.environ.get("RN_MODEL", "")
    if env:
        if env in MODELS:
            return env
        sys.exit("RN_MODEL=%s is not a known model (%s)" % (env, ", ".join(sorted(MODELS))))

    read_cpuinfo = cpuinfo_reader or (lambda: open("/proc/cpuinfo").read())
    try:
        cpu = read_cpuinfo()
    except OSError:
        cpu = ""
    m = re.search(r"^model name\s*:\s*(.*)$", cpu, re.M)
    cpu_name = m.group(1).strip() if m else "<unknown>"
    if re.search(r"\bC3\d{3}\b", cpu_name):
        return "rn426"

    read_dmi = dmi_reader or (lambda: open("/sys/class/dmi/id/product_name").read())
    try:
        product = read_dmi().strip()
    except OSError:
        product = ""
    if product in ("ReadyNAS 528X", "ReadyNAS 628X"):
        return "rnx26"
    if product == "ReadyNAS 316":
        return "rn316"

    sys.exit(
        "refusing to start: CPU is '%s', DMI product is '%s' -- neither matches\n"
        "a known ReadyNAS model (Atom C3000/Denverton for RN426/RN428, a\n"
        "528X/628X product name for the experimental gpio_ich path, or a\n"
        "ReadyNAS 316 product name for the experimental, display-only RN316\n"
        "gpio_ich path). This driver only supports those. See docs/porting.md\n"
        "for the porting story. Set RN_MODEL=rn426, RN_MODEL=rnx26 or\n"
        "RN_MODEL=rn316 to override if you know better."
        % (cpu_name, product or "<unknown>"))

def _build(model):
    spec = MODELS[model]
    if model == "rn316":
        print("WARNING: rn316 display confirmed working on one real unit; "
              "buttons are read-only (SX8635 touch-wheel, no SPM/NVM writes) "
              "and the button-to-pad mapping is still provisional -- see "
              "docs/porting.md", file=sys.stderr)
    elif model != "rn426":
        pins = spec["pins"] or {}
        note = " (display-only, no known button interrupt line)" if pins.get("BTN_INT") is None else ""
        print("WARNING: %s support is experimental and UNTESTED on real hardware%s "
              "-- see docs/porting.md" % (model, note), file=sys.stderr)
    gpio = spec["backend"](spec)
    lcd = LCD(gpio, spec["geometry"], spec["init_seq"])
    return gpio, lcd

BUTTON_CLASSES = {"msp430": Msp430Buttons, "sx8635": Sx8635Buttons}

def _button_class(spec):
    """Which button class this model's spec selects, or None -- a pure
    lookup with no I/O, so model wiring is testable without opening any
    device. The class comes ONLY from spec["buttons"]; never derived from
    BTN_INT/has_int_line (that's a separate gate, in _build_buttons) --
    otherwise merely restoring an interrupt-line pin number would silently
    change which button protocol runs."""
    return BUTTON_CLASSES.get(spec.get("buttons"))

def _build_buttons(gpio, model, spec):
    """Construct this model's button object, or None if the model has none,
    no interrupt line is known for this gpio, or construction fails.
    Degrade, don't exit: a hardware failure here (missing i2c bus, NAK from
    an absent or miswired chip) prints one warning and falls back to
    display-only auto-rotate instead of taking the whole panel down --
    EXCEPT on model "rn426", where the i2c bus and MSP430 are proven to
    exist: there, a construction OSError is re-raised so run() exits and
    systemd (Restart=always) restarts the daemon, which is the old
    behaviour (a late-loading i2c-i801 module recovers on the next start,
    and a persistent real fault stays visible instead of silently
    downgrading to auto-rotate on the one model that's supposed to always
    have buttons)."""
    cls = _button_class(spec)
    if cls is None or not gpio.has_int_line:
        return None
    try:
        if cls is Sx8635Buttons:
            return cls(gpio, spec["sx8635"])
        return cls(gpio)
    except OSError as e:
        if model == "rn426":
            raise
        print("WARNING: %s button init failed (%s) -- running display-only"
              % (spec["buttons"], e), file=sys.stderr)
        return None

def _rotate_seconds():
    """RN_ROTATE (seconds): on a no-buttons model, how often run() advances
    to the next page on its own. Default 10; 0 disables rotation entirely.
    Factored out so it's testable without poking os.environ inside run()."""
    return int(os.environ.get("RN_ROTATE", "10"))

def _apply_actions(actions, idx, last_show, activity, asleep, now):
    """Pure state transition for one events() batch -- no I/O (no lcd, no
    gpio), so it's testable without a display or a device. Mirrors the
    original run()'s behaviour:
      - if asleep, ANY action just wakes the display, discarding the REST
        of the batch (e.g. a wheel flick that produced several NEXTs while
        asleep must not also turn pages once woken);
      - otherwise NEXT/PREV move pages and OK forces a redraw, while
        LEFT/RIGHT and any action this function doesn't recognize are
        activity-only: they still reset the idle timer (and would wake a
        sleeping display, per above) but never move a page.
    Every action updates `activity`, matching "any action resets the idle
    timer" from the old MSP430-only loop.
    Returns (idx, last_show, activity, asleep, woke) -- `woke` tells the
    caller (run()) whether to call lcd.wake()."""
    woke = False
    for action in actions:
        activity = now
        if asleep:
            asleep = False
            last_show = 0
            woke = True
            break   # wake only -- discard the rest of this batch
        if action == "NEXT":
            idx = (idx + 1) % len(PAGES); last_show = 0
        elif action == "PREV":
            idx = (idx - 1) % len(PAGES); last_show = 0
        elif action == "OK":
            last_show = 0
        # else: LEFT/RIGHT/unknown -- activity-only, already handled above
    return idx, last_show, activity, asleep, woke

# --------------------------------------------------------------------------
def run(model):
    sleep_after = int(os.environ.get("RN_SLEEP", "90"))
    rotate_after = _rotate_seconds()
    gpio, lcd = _build(model)
    lcd.init()
    spec = MODELS[model]
    btn = _build_buttons(gpio, model, spec)
    idx = 0; last_show = 0.0; activity = time.time(); asleep = False
    last_rotate = time.time()
    while True:
        now = time.time()
        if btn is not None:
            # Backend-agnostic: btn.events() hides whichever protocol (MSP430
            # debounce, SX8635 IrqSrc/wheel state machine) produced these.
            actions = btn.events(now)
            if actions:
                idx, last_show, activity, asleep, woke = _apply_actions(
                    actions, idx, last_show, activity, asleep, now)
                if woke:
                    lcd.wake()
        elif rotate_after > 0 and now - last_rotate >= rotate_after:
            # No buttons on this model (btn is None): auto-advance pages
            # instead of sitting on page 0 forever.
            idx = (idx + 1) % len(PAGES); last_show = 0; last_rotate = now
        if not asleep:
            if sleep_after > 0 and now - activity > sleep_after:
                lcd.sleep(); asleep = True
            elif now - last_show >= 5:
                try:
                    c = PAGES[idx]()
                except Exception as e:
                    c = ("ERR", str(e)[:14])
                lcd.lines(c[0], c[1]); last_show = now
        time.sleep(0.005)                   # 200 Hz pad-watch; no i2c, never wedges the MCU

def main():
    model = detect_model()
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "sleep":
        gpio, lcd = _build(model)
        gpio.idle()
        lcd.sleep(); print("display asleep")
    elif mode == "wake":
        gpio, lcd = _build(model)
        lcd.init(); print("display awake")
    else:
        run(model)

if __name__ == "__main__":
    main()
