#!/usr/bin/env python3
"""
RN426 front-panel driver for TrueNAS SCALE running on NETGEAR ReadyNAS RN426
(and RN526/RN626, which share the "RN526&RN626X Front Board V20").

It drives the 128x32 SSD1305 graphic LCD by bit-banging SPI over the Intel
Denverton (Atom C3000) SoC GPIO pads via /dev/mem, and reads the 5-way
navigation buttons from the front-board MSP430 microcontroller over the Intel
i801 SMBus. No kernel module required.

See docs/ for the full reverse-engineering writeup and protocol details.

Usage:  rn426_panel.py [run|sleep|wake]
Env:    RN_SLEEP = idle seconds before the display sleeps (default 90; 0 = never)

Requires: python3-pil (Pillow) and the DejaVu fonts (both ship with TrueNAS SCALE),
          i2c-dev + i2c-i801 kernel modules, and root (for /dev/mem, /dev/port, i2c).
"""
import mmap, struct, time, socket, subprocess, os, fcntl, ctypes, glob, sys

# --------------------------------------------------------------------------
# P2SB unhide -- pure Python PCI-config write via /dev/port (no helper binary).
# The Denverton GPIO community registers live behind the P2SB/SBREG window,
# which the BIOS hides. Clearing bit 0 of 00:1f.1 cfg reg 0xE1 reveals it.
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
# LCD: SSD1305 128x32, bit-bang SPI on Denverton PADCFG_DW0 registers.
#   North GPIO community base 0xFDC20000, South 0xFDC50000 (PADBAR 0x400).
#   Drive a pad = clear bit 9 (GPIOTXDIS -> output enable) + set bit 0 (TX value).
#   The firmware leaves these pads in GPIO mode, so only those two bits matter.
# --------------------------------------------------------------------------
NORTH, SOUTH = 0xFDC20000, 0xFDC50000
# SSD1305 init (33 bytes). NOTE: contains no display-on; 0xAF is sent after.
INIT_SEQ = bytes.fromhex("aed571a81fd9222002a1c8da12d80081cfb0d300210483220003100040a6a4db18")

class LCD:
    def __init__(self):
        self._fN = open("/dev/mem", "r+b"); self.mN = mmap.mmap(self._fN.fileno(), 0x1000, offset=NORTH)
        self._fS = open("/dev/mem", "r+b"); self.mS = mmap.mmap(self._fS.fileno(), 0x1000, offset=SOUTH)
        # (mmap, PADCFG_DW0 offset within the community)
        self.EN   = (self.mN, 0x418)   # display / backlight enable
        self.CLK  = (self.mN, 0x470)   # SPI clock
        self.MOSI = (self.mN, 0x480)   # SPI data
        self.RST  = (self.mN, 0x488)   # controller reset
        self.DC   = (self.mS, 0x580)   # data/command
        self.CS   = (self.mS, 0x5c8)   # chip select
        from PIL import ImageFont
        self.f1 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 16)
        self.f2 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 13)

    def _set(self, pad, val):
        m, o = pad
        v = struct.unpack_from("<I", m, o)[0]
        v &= ~(1 << 9)                  # GPIOTXDIS = 0 (enable output driver)
        v = (v & ~1) | (val & 1)        # GPIOTXSTATE = val
        struct.pack_into("<I", m, o, v)

    def _spi(self, byte, dc):
        self._set(self.CS, 0); self._set(self.DC, dc)
        for k in range(7, -1, -1):                       # MSB first
            self._set(self.CLK, 0)
            self._set(self.MOSI, (byte >> k) & 1)
            self._set(self.CLK, 1)                       # latch on rising edge
        self._set(self.DC, 1); self._set(self.CS, 1)

    def cmd(self, b): self._spi(b, 0)
    def dat(self, b): self._spi(b, 1)

    def init(self):
        for p, v in [(self.CS, 1), (self.RST, 1), (self.CLK, 0), (self.MOSI, 0), (self.DC, 0)]:
            self._set(p, v)
        self._set(self.EN, 0)
        # IMPORTANT: do NOT pulse RST. Pin 31 (RST) is a *shared front-board reset*
        # that also resets the MSP430 into a non-button-reporting mode, which only a
        # full power-cycle recovers. The SSD1305 is already powered (the BIOS used
        # it), so the command sequence alone re-inits it -- we just hold RST high.
        for b in INIT_SEQ:
            self.cmd(b)
        self._set(self.EN, 1)
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
        for page in range(4):                            # 4 pages x 8 rows = 32 px tall
            self.cmd(0xB0 | page); self.cmd(0x00); self.cmd(0x10)
            for c in range(132):
                byte = 0
                for r in range(8):
                    if px[c, page * 8 + r]:
                        byte |= (1 << r)
                self.dat(byte)

    def lines(self, l1, l2):
        from PIL import Image, ImageDraw
        img = Image.new("1", (132, 32), 0); d = ImageDraw.Draw(img)
        d.text((4, -2), l1, font=self.f1, fill=1)
        d.text((4, 17), l2, font=self.f2, fill=1)
        self.show(img)

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
def run():
    sleep_after = int(os.environ.get("RN_SLEEP", "90"))
    p2sb_unhide()
    lcd = LCD(); lcd.init()
    btn = Buttons()
    idx = 0; last_show = 0.0; activity = time.time(); asleep = False
    armed = True; high_since = time.time()
    while True:
        now = time.time()
        # Watch the MCU interrupt pad via /dev/mem -- a cheap memory read that does
        # NOT touch i2c. We read the MCU (reg 0x04) ONLY when this pad signals a
        # press, i.e. only when the MCU is awake. We never poll a sleeping MCU, so
        # its button reporting is never corrupted (the old i2c-poll loop did that).
        if lcd.int_active():
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
            # pad idle (high). Re-arm once it has been stable-high briefly, to
            # debounce the MCU's pulse-train (one physical press -> one event).
            if high_since is None:
                high_since = now
            elif now - high_since >= 0.05:
                armed = True
        if not asleep:
            if sleep_after > 0 and now - activity > sleep_after:
                lcd.sleep(); asleep = True
            elif now - last_show >= 5:
                try:
                    c = PAGES[idx]()
                except Exception as e:
                    c = ("ERR", str(e)[:14])
                lcd.lines(c[0], c[1]); last_show = now
        time.sleep(0.005)                   # 200 Hz pad-watch; /dev/mem only, no i2c, never wedges the MCU

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "sleep":
        p2sb_unhide(); lcd = LCD()
        for p, v in [(lcd.CS, 1), (lcd.RST, 1), (lcd.CLK, 0), (lcd.MOSI, 0), (lcd.DC, 0)]:
            lcd._set(p, v)
        lcd.sleep(); print("display asleep")
    elif mode == "wake":
        p2sb_unhide(); lcd = LCD(); lcd.init(); print("display awake")
    else:
        run()

if __name__ == "__main__":
    main()
