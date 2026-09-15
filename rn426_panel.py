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
    has_buttons = True   # overridden False by backends/models with no known MCU interrupt line

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
        self.has_buttons = pins["BTN_INT"] is not None
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
        # Active-low assumed by analogy with the Dnv backend's interrupt
        # pad; NOT yet confirmed on real gpio_ich hardware -- check
        # GPI_INV in the stage-A register dump (docs/porting.md) before
        # trusting this.
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
# Buttons: front-board MSP430 microcontroller on the Intel i801 SMBus.
#   reg 0x04 = active-high button bitmap. READ ONLY.
#   IMPORTANT: do NOT write the MCU's reg 0x02 (LED/control). On this firmware
#   it also gates button scanning, and once disabled it only recovers on a full
#   power-cycle (a warm reboot is not enough). This driver never writes the MCU.
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

class Buttons:
    LEFT, RIGHT, UP, DOWN, CENTER = 0x01, 0x02, 0x04, 0x08, 0x10
    ADDR, REG = 0x1C, 0x04
    def __init__(self):
        self.fd = os.open("/dev/i2c-%d" % find_i801_bus(), os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, self.ADDR)
        self.prev = 0
    def _read(self, reg):
        buf = (ctypes.c_ubyte * 34)()
        a = _smbus_ioctl(1, reg, 2, ctypes.cast(buf, ctypes.c_void_p))
        fcntl.ioctl(self.fd, I2C_SMBUS, a)
        return buf[0]
    def pressed(self):
        """Bitmap of buttons newly pressed since the last call (rising edge)."""
        try:
            v = self._read(self.REG)
        except OSError:
            return 0
        new = v & ~self.prev
        self.prev = v
        return new

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
    },
    "rn316": {
        # RN316 (Atom D2701, ICH10 LPC 8086:3a18, gpio_ich 61 lines). Pin map
        # taken from the stock firmware rn316 config struct (docs/porting.md).
        # Init table is reused from the RN426 and UNVERIFIED on this panel.
        # Buttons are unknown on this generation -- the probe found no MSP430
        # at 0x1c, possibly an SX8635 captouch instead -- so BTN_INT is None
        # and this driver is display-only here. EN/RST wedge risk is
        # unconfirmed on this front board but treated as real anyway.
        "backend": IchPortGpio,
        "geometry": (4, 132),
        "init_seq": INIT_SEQ,
        "pins": {"MOSI": 21, "CLK": 19, "DC": 16, "CS": 7, "EN": 32, "RST": 24, "BTN_INT": None},
        "lpc_id": 0x3a18,   # ICH10 LPC bridge
        "ngpio": 61,        # gpio-ich.c ICH_V5/ICH10 line count
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
    if model != "rn426":
        pins = spec["pins"] or {}
        note = " (display-only, no known button interrupt line)" if pins.get("BTN_INT") is None else ""
        print("WARNING: %s support is experimental and UNTESTED on real hardware%s "
              "-- see docs/porting.md" % (model, note), file=sys.stderr)
    gpio = spec["backend"](spec)
    lcd = LCD(gpio, spec["geometry"], spec["init_seq"])
    return gpio, lcd

def _rotate_seconds():
    """RN_ROTATE (seconds): on a no-buttons model, how often run() advances
    to the next page on its own. Default 10; 0 disables rotation entirely.
    Factored out so it's testable without poking os.environ inside run()."""
    return int(os.environ.get("RN_ROTATE", "10"))

# --------------------------------------------------------------------------
def run(model):
    sleep_after = int(os.environ.get("RN_SLEEP", "90"))
    rotate_after = _rotate_seconds()
    gpio, lcd = _build(model)
    lcd.init()
    # Some models (e.g. rn316) have no known MCU interrupt line -- skip
    # Buttons entirely rather than opening an i2c device for a button
    # protocol we haven't confirmed even exists on that front board.
    btn = Buttons() if gpio.has_buttons else None
    idx = 0; last_show = 0.0; activity = time.time(); asleep = False
    armed = True; high_since = time.time(); last_rotate = time.time()
    while True:
        now = time.time()
        if btn is not None:
            # Watch the MCU interrupt pad via the Gpio backend -- a cheap read
            # that does NOT touch i2c. We read the MCU (reg 0x04) ONLY when
            # this pad signals a press, i.e. only when the MCU is awake. We
            # never poll a sleeping MCU, so its button reporting is never
            # corrupted (the old i2c-poll loop did that).
            if gpio.int_active():
                high_since = None
                if armed:                       # one event per physical press
                    armed = False
                    try:
                        v = btn._read(Buttons.REG)
                    except OSError:
                        v = 0
                    if v:
                        activity = now
                        if asleep:
                            lcd.wake(); asleep = False; last_show = 0
                        else:
                            if v & Buttons.UP:     idx = (idx - 1) % len(PAGES); last_show = 0
                            if v & Buttons.DOWN:   idx = (idx + 1) % len(PAGES); last_show = 0
                            if v & Buttons.CENTER: last_show = 0
            else:
                # pad idle (high). Re-arm once it has been stable-high briefly,
                # to debounce the MCU's pulse-train (one physical press -> one event).
                if high_since is None:
                    high_since = now
                elif now - high_since >= 0.05:
                    armed = True
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
