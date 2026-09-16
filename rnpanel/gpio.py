import mmap, struct, sys, os

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
