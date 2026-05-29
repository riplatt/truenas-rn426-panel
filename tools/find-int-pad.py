#!/usr/bin/env python3
"""
find-int-pad.py -- locate the front-panel button INTERRUPT line on a ReadyNAS.

The front-board MCU (TI MSP430) raises an interrupt on a host SoC GPIO pad when a
button is pressed. Watching that pad (a cheap /dev/mem read) lets the driver read
the MCU over i2c ONLY when a button is actually pressed -- which is the right way
to do it, because *polling* the MCU over i2c while it has gone to sleep corrupts
its button scanning (see docs/buttons-protocol.md).

On the RN426/RN526/RN626X that pad is South community 0xFDC50570. If you have a
DIFFERENT board, run this to find yours: it samples every Denverton North+South
SoC GPIO pad's input level (PADCFG_DW0 RXSTATE, bit 1) in two phases and reports
the pad that is STABLE while idle but TOGGLES on presses.

  *** Beware free-running decoys ***  Some pads toggle on their own (e.g. RN426
  North 0xFDC20520). They look like the INT line if you only sample during
  presses -- that's why this tool requires an IDLE phase too.

Usage (run as root on the box, with the panel daemon STOPPED):
    sudo systemctl stop rn426-panel        # don't let the LCD bit-bang pads
    sudo python3 find-int-pad.py [idle_secs] [press_secs]

Protocol: do nothing for the idle phase, then tap ALL buttons rapidly during the
press phase. The candidate it prints (community + offset) is your INT pad.

NOTE: the North/South community base addresses below are for the Intel Denverton
(Atom C3000) SoC used by the RN426 family. Other SoCs use different bases; adjust
COMMUNITIES accordingly (these are the same windows the LCD driver pokes).
"""
import mmap, struct, time, sys, glob, os, fcntl, ctypes

COMMUNITIES = [("North", 0xFDC20000), ("South", 0xFDC50000)]
PADBAR = 0x400                       # first PADCFG_DW0 within a community
RXSTATE_BIT = 1                      # PADCFG_DW0 bit 1 = input level
IDLE_SECS  = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
PRESS_SECS = float(sys.argv[2]) if len(sys.argv) > 2 else 9.0
TICK = 0.004

# --- P2SB unhide: the GPIO community registers sit behind a window the BIOS
#     hides; clearing bit 0 of PCI 00:1f.1 reg 0xE1 reveals it (pure /dev/port).
def p2sb_unhide():
    addr = 0x80000000 | (0 << 16) | (0x1F << 11) | (1 << 8) | (0xE1 & 0xFC)
    with open("/dev/port", "r+b", 0) as p:
        p.seek(0xCF8); p.write(struct.pack("<I", addr))
        p.seek(0xCFC + (0xE1 & 3)); p.write(bytes([0x00]))

# --- optional: read MCU reg 0x04 to confirm the MCU is actually reporting -----
I2C_SLAVE, I2C_SMBUS = 0x0703, 0x0720
class _sm(ctypes.Structure):
    _fields_ = [("rw", ctypes.c_ubyte), ("cmd", ctypes.c_ubyte),
                ("size", ctypes.c_uint), ("data", ctypes.c_void_p)]
_i2c_fd = None
def i2c_open():
    global _i2c_fd
    for d in sorted(glob.glob("/sys/class/i2c-dev/i2c-*")):
        try:
            if "I801" in open(d + "/name").read():
                _i2c_fd = os.open("/dev/i2c-%s" % d.rsplit("-", 1)[1], os.O_RDWR)
                fcntl.ioctl(_i2c_fd, I2C_SLAVE, 0x1C)
                return True
        except OSError:
            pass
    return False
def reg04():
    buf = (ctypes.c_ubyte * 34)()
    fcntl.ioctl(_i2c_fd, I2C_SMBUS, _sm(1, 0x04, 2, ctypes.cast(buf, ctypes.c_void_p)))
    return buf[0]

def main():
    if os.geteuid() != 0:
        sys.exit("Run as root (needs /dev/mem, /dev/port, /dev/i2c).")
    p2sb_unhide()
    maps = []
    for name, base in COMMUNITIES:
        f = open("/dev/mem", "r+b")
        maps.append((name, mmap.mmap(f.fileno(), 0x1000, offset=base)))

    pads = []
    for name, m in maps:
        for off in range(PADBAR, 0x1000, 8):
            dw0 = struct.unpack_from("<I", m, off)[0]
            if dw0 not in (0x00000000, 0xFFFFFFFF):     # skip unimplemented pads
                pads.append({"name": name, "off": off, "m": m,
                             "idle": {"vals": set(), "tr": 0, "last": None},
                             "press": {"vals": set(), "tr": 0, "last": None}})
    print("Enumerated %d candidate pads across %d communities." % (len(pads), len(maps)))
    print("PROTOCOL: do NOTHING for %ds, then TAP ALL buttons rapidly for %ds.\n"
          % (int(IDLE_SECS), int(PRESS_SECS)))

    have_i2c = i2c_open()
    reg_vals, reg_nz = set(), 0

    def sample(phase, dur, read_btn=False):
        nonlocal reg_nz
        t_end = time.time() + dur
        tick = 0
        while time.time() < t_end:
            for p in pads:
                v = (struct.unpack_from("<I", p["m"], p["off"])[0] >> RXSTATE_BIT) & 1
                s = p[phase]
                s["vals"].add(v)
                if s["last"] is not None and v != s["last"]:
                    s["tr"] += 1
                s["last"] = v
            if read_btn and tick % 6 == 0:
                b = reg04(); reg_vals.add(b); reg_nz += (b > 0)
            tick += 1
            time.sleep(TICK)

    print(">>> IDLE: do not touch anything ...")
    sample("idle", IDLE_SECS)
    print(">>> PRESS: tap all buttons rapidly NOW ...")
    sample("press", PRESS_SECS, read_btn=have_i2c)
    if have_i2c:
        print("\nMCU reg 0x04 during press: values=%s  -> %s"
              % (sorted(reg_vals), "MCU reporting OK" if reg_nz else
                 "MCU NOT reporting (wedged? do a cold power-cycle) or taps missed"))

    cands = sorted([p for p in pads if p["idle"]["tr"] == 0 and p["press"]["tr"] > 0],
                   key=lambda p: p["press"]["tr"], reverse=True)
    print("\n=== CANDIDATE INT PAD(S): stable when idle, toggling on presses ===")
    if cands:
        for p in cands:
            base = dict(COMMUNITIES)[p["name"]]
            print("  *** %s community  PADCFG 0x%08X  (base 0x%08X + 0x%03X, pad %d)"
                  "  idle=stable%s  press_transitions=%d"
                  % (p["name"], base + p["off"], base, p["off"], (p["off"] - PADBAR) // 8,
                     sorted(p["idle"]["vals"]), p["press"]["tr"]))
        print("\nUse the top candidate as your INT pad (active-low if idle value is [1]).")
    else:
        print("  none -- INT may be a brief pulse or in another community.")
        print("  Most-active-on-press pads (for manual inspection):")
        for p in sorted(pads, key=lambda p: p["press"]["tr"], reverse=True)[:10]:
            print("  %s+0x%03X  idle_tr=%d  press_tr=%d" % (p["name"], p["off"], p["idle"]["tr"], p["press"]["tr"]))

    fr = sorted([p for p in pads if p["idle"]["tr"] > 0 and p["press"]["tr"] > 0],
                key=lambda p: p["idle"]["tr"], reverse=True)
    if fr:
        print("\n=== free-running pads (toggle even when idle -- DECOYS, ignore) ===")
        for p in fr[:8]:
            print("  %s+0x%03X  idle_tr=%d  press_tr=%d" % (p["name"], p["off"], p["idle"]["tr"], p["press"]["tr"]))

if __name__ == "__main__":
    main()
