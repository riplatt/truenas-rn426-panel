#!/usr/bin/env python3
"""
sx8635-watch.py -- STRICTLY READ-ONLY diagnostic tool for the RN316's SX8635
capacitive touch-wheel controller (i2c address 0x2b on the i801 SMBus).

See .claude/advice/sx8635-buttons-design.md (Q6) for the design this
implements, and .claude/advice/sx8635-re-report.md for the register facts
(register numbers/bit meanings come straight from the stock sx8635.ko
disassembly plus the SX8636 datasheet; that doc is authoritative over any
paraphrase in comments here).

*** WHY THIS TOOL NEVER WRITES THE CHIP, EVER ***
The SX8635 has an NVM (permanent, burns up to 3 times) shadowed by a
volatile SPM (working RAM, reloaded from NVM/QSM on every power-up). Writing
the SPM is normal and reversible with a power cycle. Burning the NVM is
NOT: the sequence is unlock key 0x62 -> reg 0xAC, 0x9D -> reg 0xAD, then the
pulse 0xA5 -> reg 0x0E, 0x5A -> reg 0x0E, each its own I2C transaction. This
tool has no legitimate reason to write ANYTHING to this chip -- it exists to
observe, not configure -- so it goes further than "don't send that
sequence" and simply never issues a write ioctl to the chip at all, and
never uses a positional write syscall (the offset-write primitive some
other tools in this repo use for /dev/mem or /dev/port) anywhere, on any
fd. A single choke point (_smbus_read) is the only I/O path to the chip,
and it asserts the SMBus transfer direction is read every time it is
called. A module-level self-check (below) additionally scans this file's
own source for that positional-write syscall's name and refuses to import
if it finds it -- belt and suspenders for a mistake nobody should be able
to make by accident.
"""
import os
import sys
import glob
import time
import struct
import ctypes
import socket
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# rn426_panel guards its own fcntl import for Windows importability (see its
# header); importing it here does NOT require Pillow or fcntl at import
# time, only when its hardware classes are actually constructed. We only
# ever use the three pure/stdlib helpers below plus module-level constants.
from rn426_panel import find_i801_bus, _parse_gpiobase, _ich_line_addr, I2C_SLAVE, I2C_SMBUS, _smbus_ioctl

try:
    import fcntl                        # POSIX only, see rn426_panel.py's own guard
except ImportError:
    fcntl = None                        # lets this module import (e.g. for tests) on Windows

# Self-check: this file must never contain a call to the positional-write
# syscall (the "write to this exact byte offset" primitive rn426_panel.py's
# IchPortGpio._wr_byte uses on /dev/port, and DnvMmioGpio would need on
# /dev/mem). Built from two pieces so this very line doesn't itself contain
# the forbidden word as contiguous text.
_FORBIDDEN_SYSCALL = "p" + "write"
with open(__file__) as _self_fh:
    assert _FORBIDDEN_SYSCALL not in _self_fh.read(), (
        "sx8635-watch.py source contains a call to the forbidden positional-write "
        "syscall -- this tool must never write anywhere, aborting import")
del _self_fh

SX8635_ADDR = 0x2B

REG_IRQSRC        = 0x00
REG_CAPSTAT_MSB   = 0x01   # bit 4 = slider/wheel touched (gates 0x02 vs 0x03/0x04 below)
REG_CAPSTAT_LSB   = 0x02   # per-bit capacitive button touch bitmap
REG_SLIDER_MSB    = 0x03   # slider/wheel position, high byte
REG_SLIDER_LSB    = 0x04   # slider/wheel position, low byte
REG_GPISTAT       = 0x07
REG_SPMSTAT       = 0x08
REG_COMPOPMODE    = 0x09

# IrqSrc named bits, listed high-to-low to match the design doc's Table 24
# writeup. bit3 is "slider/wheel" on the SX8634/5 (reserved on the
# button-only SX8636 the datasheet was fetched from).
IRQ_BITS = [
    (6, "nvmburn"),
    (5, "spmwrite"),
    (4, "gpi"),
    (3, "wheel"),
    (2, "buttons"),
    (1, "comp"),
    (0, "opmode"),
]

I2C_SMBUS_READ = 1          # rw sense: read (rn426_panel.Buttons._read uses the same value)
I2C_SMBUS_BYTE_DATA = 2     # transfer size code for a single-byte register read


# --------------------------------------------------------------------------
# Pure decode/logic functions -- no I/O, importable and unit-testable on
# Windows. Keep it that way: nothing here touches a device or the OS.
# --------------------------------------------------------------------------
def decode_irqsrc(v):
    """IrqSrc byte -> list of set bit names, high bit first."""
    return [name for bit, name in IRQ_BITS if v & (1 << bit)]


def format_irq(v):
    names = decode_irqsrc(v)
    return "0x%02x[%s]" % (v, ",".join(names)) if names else "0x%02x" % v


def decode_spmstat(v):
    """SpmStat (0x08): bit3 NvmValid, bits2:0 NvmCount (Table 27)."""
    return {"nvm_valid": bool(v & 0x08), "nvm_count": v & 0x07}


def decode_compopmode(v):
    """CompOpMode (0x09): bits1:0 mode, bit2 compensation flag (Table 28).
    Bits 7:3 are write-only-zero and read back as "varying values" per the
    datasheet footnote, so they are ignored here rather than decoded."""
    mode = {0: "Active", 1: "Doze", 2: "Sleep"}.get(v & 0x03, "Reserved(%d)" % (v & 0x03))
    return {"mode": mode, "comp": bool(v & 0x04)}


def compute_verdict(summary):
    """Pure decision-table lookup (design doc Q6), NO I/O. summary keys:
    nvm_valid, nvm_count, button_bits_seen, wheel_activity_seen,
    nirq_ever_low, nirq_zero_to_one_after_read, irq_nonzero_count,
    attempted_reads, oserror_count. Order matters: "every read errored" and
    "nothing ever moved" both trump the button/wheel evidence check."""
    attempted = summary.get("attempted_reads", 0)
    oserr = summary.get("oserror_count", 0)
    if attempted > 0 and oserr >= attempted:
        return "chip absent"
    if not summary.get("nirq_ever_low") and summary.get("irq_nonzero_count", 0) == 0:
        return "gating-or-chip check line 2"
    # Working touch evidence is the real test, not NvmValid: if buttons and the
    # wheel both respond, the config in the chip is good enough whatever
    # SpmStat says, and no write is needed.
    if summary.get("button_bits_seen") and summary.get("wheel_activity_seen"):
        return "read-only works"
    return "needs SPM load"


# --------------------------------------------------------------------------
# The ONLY I/O path to the chip. Every chip access in this file goes through
# this one function, and it asserts the SMBus transfer direction is read.
# --------------------------------------------------------------------------
def _smbus_read(fd, reg):
    buf = (ctypes.c_ubyte * 34)()
    xfer = _smbus_ioctl(I2C_SMBUS_READ, reg, I2C_SMBUS_BYTE_DATA, ctypes.cast(buf, ctypes.c_void_p))
    assert xfer.rw == I2C_SMBUS_READ, "refusing a non-read SMBus transfer to the SX8635"
    fcntl.ioctl(fd, I2C_SMBUS, xfer)
    return buf[0]


def _read_nirq(port_fd, port, bit):
    return (os.pread(port_fd, 1, port)[0] >> bit) & 1


# --------------------------------------------------------------------------
# Report buffering, same shape as tools/ich-gpio-dump.py.
# --------------------------------------------------------------------------
OUT_LINES = []


def out(s=""):
    print(s)
    OUT_LINES.append(s)


def die(msg):
    out()
    out("ABORT: %s" % msg)
    write_report()
    sys.exit(1)


def write_report():
    hostname = socket.gethostname() or "unknown"
    default_path = "/tmp/sx8635-watch-%s.txt" % hostname
    out_path = os.environ.get("SX_WATCH_OUT", default_path)
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(OUT_LINES) + "\n")
        sys.stderr.write("\n(report also written to %s)\n" % out_path)
    except OSError as e:
        sys.stderr.write("\n(could not write report file %s: %s)\n" % (out_path, e))
    return out_path


def countdown(label):
    print()
    print("--- next: %s ---" % label)
    for n in (3, 2, 1):
        print(n, end=" ", flush=True)
        time.sleep(1)
    print("GO")


# --------------------------------------------------------------------------
# Preconditions. Two readers of IrqSrc (this tool and the running daemon)
# steal events from each other since reading IrqSrc clears it -- refuse to
# run alongside rn426-panel.service rather than produce a misleading trace.
# --------------------------------------------------------------------------
def check_preconditions(force):
    if not force:
        try:
            product = open("/sys/class/dmi/id/product_name").read().strip()
        except OSError:
            product = ""
        if product != "ReadyNAS 316":
            die("DMI product_name is '%s', not 'ReadyNAS 316' -- this tool is scoped to "
                "that board's SX8635 wiring. Pass --force if you know better."
                % (product or "<unreadable>"))
    try:
        r = subprocess.run(["systemctl", "is-active", "rn426-panel"],
                            capture_output=True, text=True, timeout=5)
        state = r.stdout.strip()
    except Exception:
        state = ""
    if state == "active":
        die("rn426-panel.service is active. IrqSrc (reg 0x00) is read-to-clear, so this "
            "tool and the running daemon would steal each other's events -- stop the "
            "service first: systemctl stop rn426-panel")


# --------------------------------------------------------------------------
# Startup block -- printed once, before any loop.
# --------------------------------------------------------------------------
def startup_block(i2c_fd, port_fd, nirq_port, nirq_bit, overall):
    section = lambda t: out("\n=== %s ===" % t)

    section("Startup register snapshot")
    spmstat = _smbus_read(i2c_fd, REG_SPMSTAT)
    overall["attempted_reads"] += 1
    sd = decode_spmstat(spmstat)
    overall["nvm_valid"] = sd["nvm_valid"]
    overall["nvm_count"] = sd["nvm_count"]
    out("SpmStat  0x08 = 0x%02x : NvmValid=%d NvmCount=%d"
        % (spmstat, sd["nvm_valid"], sd["nvm_count"]))

    compop = _smbus_read(i2c_fd, REG_COMPOPMODE)
    overall["attempted_reads"] += 1
    cd = decode_compopmode(compop)
    out("CompOpMode 0x09 = 0x%02x : mode=%s comp=%d" % (compop, cd["mode"], cd["comp"]))

    gpistat = _smbus_read(i2c_fd, REG_GPISTAT)
    overall["attempted_reads"] += 1
    out("GpiStat  0x07 = 0x%02x" % gpistat)

    nirq_before = _read_nirq(port_fd, nirq_port, nirq_bit)
    out("NIRQ (gpio_ich line 2) before IrqSrc read = %d" % nirq_before)
    if nirq_before == 0:
        overall["nirq_ever_low"] = True

    irqsrc = _smbus_read(i2c_fd, REG_IRQSRC)
    overall["attempted_reads"] += 1
    if irqsrc:
        overall["irq_nonzero_count"] += 1
    out("IrqSrc   0x00 = %s" % format_irq(irqsrc))

    nirq_after = _read_nirq(port_fd, nirq_port, nirq_bit)
    out("NIRQ after IrqSrc read = %d   (expect 0 -> 1)" % nirq_after)
    if nirq_before == 0 and nirq_after == 1:
        overall["nirq_zero_to_one_after_read"] += 1

    r1 = _smbus_read(i2c_fd, REG_CAPSTAT_MSB)
    r2 = _smbus_read(i2c_fd, REG_CAPSTAT_LSB)
    r3 = _smbus_read(i2c_fd, REG_SLIDER_MSB)
    r4 = _smbus_read(i2c_fd, REG_SLIDER_LSB)
    overall["attempted_reads"] += 4
    out("regs 0x01..0x04 = 0x%02x 0x%02x 0x%02x 0x%02x" % (r1, r2, r3, r4))
    return r1, r2, r3, r4


# --------------------------------------------------------------------------
# Phased loop.
# --------------------------------------------------------------------------
# Active-phase length is overridable (a slow tester needs longer to do a
# full wheel rotation); idle phases stay fixed at 10s regardless of that
# env var -- their only job is to give a quiet baseline and exercise the
# 1s heartbeat a few times, which a longer or shorter idle doesn't change,
# so there is nothing for a tester to usefully lengthen there.
IDLE_SECS = 10
ACTIVE_SECS = int(os.environ.get("SX_WATCH_PHASE_SECS", "8"))

PHASES = [
    {"name": "IDLE1", "prompt": "IDLE -- hands off the panel entirely", "secs": IDLE_SECS},
    {"name": "OK",    "prompt": "Touch and hold the OK button for about 1s, then release", "secs": ACTIVE_SECS},
    {"name": "OTHER", "prompt": "Touch any OTHER front pad (not OK)", "secs": ACTIVE_SECS},
    {"name": "CW",    "prompt": "Rotate the wheel slowly CLOCKWISE one full turn", "secs": ACTIVE_SECS},
    {"name": "CCW",   "prompt": "Rotate the wheel slowly COUNTER-CLOCKWISE one full turn", "secs": ACTIVE_SECS},
    {"name": "IDLE2", "prompt": "IDLE -- hands off the panel entirely", "secs": IDLE_SECS},
]


def new_phase_summary():
    return {
        "irq_counts": {name: 0 for _bit, name in IRQ_BITS},
        "reg02_bitmaps": set(),
        "pos_values": [],
        "nirq_low_samples": 0,
        "clean_deassert": 0,
        "oserror_count": 0,
    }


def run_phase(i2c_fd, port_fd, nirq_port, nirq_bit, phase, state, overall):
    name = phase["name"]
    psum = new_phase_summary()
    t_end = time.perf_counter() + phase["secs"]

    while time.perf_counter() < t_end:
        loop_t = time.perf_counter()
        try:
            nirq_now = _read_nirq(port_fd, nirq_port, nirq_bit)
        except OSError:
            state["oserror_count"] += 1
            psum["oserror_count"] += 1
            overall["oserror_count"] += 1
            time.sleep(0.005)
            continue

        if nirq_now == 0:
            psum["nirq_low_samples"] += 1
            overall["nirq_ever_low"] = True

        # Read trigger: NIRQ currently low, OR the 50Hz wheel-in-motion
        # sub-loop is active (bit4 of 0x01 was set last block read), OR the
        # 1s heartbeat has elapsed. All three share one "last read" clock.
        do_read = nirq_now == 0
        if state["sub_loop"] and (loop_t - state["last_read_t"]) >= (1.0 / 50):
            do_read = True
        if (loop_t - state["last_read_t"]) >= 1.0:
            do_read = True

        if do_read:
            overall["attempted_reads"] += 1
            try:
                irq = _smbus_read(i2c_fd, REG_IRQSRC)
                nirq_after = _read_nirq(port_fd, nirq_port, nirq_bit)
                r1 = _smbus_read(i2c_fd, REG_CAPSTAT_MSB)
                r2 = _smbus_read(i2c_fd, REG_CAPSTAT_LSB)
                r3 = _smbus_read(i2c_fd, REG_SLIDER_MSB)
                r4 = _smbus_read(i2c_fd, REG_SLIDER_LSB)
            except OSError:
                state["oserror_count"] += 1
                psum["oserror_count"] += 1
                overall["oserror_count"] += 1
                time.sleep(0.005)
                continue

            state["last_read_t"] = loop_t
            state["sub_loop"] = bool(r1 & 0x10)

            if nirq_now == 0 and nirq_after == 1:
                psum["clean_deassert"] += 1
                overall["nirq_zero_to_one_after_read"] += 1

            if irq:
                overall["irq_nonzero_count"] += 1
                for bit, bname in IRQ_BITS:
                    if irq & (1 << bit):
                        psum["irq_counts"][bname] += 1

            psum["reg02_bitmaps"].add(r2)
            pos = (r3 << 8) | r4
            psum["pos_values"].append(pos)
            moved = state["last_pos"] is not None and pos != state["last_pos"]
            state["last_pos"] = pos

            if name in ("OK", "OTHER") and (irq & 0x04) and r2 != 0:
                overall["button_bits_seen"] = True
            if name in ("CW", "CCW") and (irq & 0x08) and (r1 & 0x10) and moved:
                overall["wheel_activity_seen"] = True

            changed = (r1 != state["last_r1"] or r2 != state["last_r2"]
                       or r3 != state["last_r3"] or r4 != state["last_r4"])
            if irq != 0 or changed:
                t = loop_t - state["t0"]
                out("t=%7.3f phase=%-5s nirq=%d->%d irq=%s msb=0x%02x lsb=0x%02x pos=0x%04x"
                    % (t, name, nirq_now, nirq_after, format_irq(irq), r3, r4, pos))

            state["last_r1"], state["last_r2"] = r1, r2
            state["last_r3"], state["last_r4"] = r3, r4

        elapsed = time.perf_counter() - loop_t
        time.sleep(max(0.0, 0.005 - elapsed))

    return psum


def print_phase_summary(phase, psum):
    out()
    out("-- phase %s summary --" % phase["name"])
    counted = {k: v for k, v in psum["irq_counts"].items() if v}
    out("  IrqSrc events by bit: %s" % (counted if counted else "none"))
    out("  distinct reg 0x02 bitmaps seen: %s"
        % (sorted("0x%02x" % v for v in psum["reg02_bitmaps"]) if psum["reg02_bitmaps"] else "none"))
    pos = psum["pos_values"]
    if pos:
        # Simple min/max/last-first-sign, not wrap-aware delta tracking --
        # see the final report for why (documented choice, not an oversight).
        sign = "+" if pos[-1] > pos[0] else ("-" if pos[-1] < pos[0] else "0")
        out("  slider/wheel pos: min=0x%04x max=0x%04x distinct=%d net_sign=%s"
            % (min(pos), max(pos), len(set(pos)), sign))
    else:
        out("  slider/wheel pos: no reads in this phase")
    out("  NIRQ low samples: %d" % psum["nirq_low_samples"])
    out("  NIRQ 0->1 immediately after an IrqSrc read: %d" % psum["clean_deassert"])
    out("  OSError count: %d" % psum["oserror_count"])


# --------------------------------------------------------------------------
def main():
    if os.name != "posix" or not os.path.isdir("/sys"):
        print("sx8635-watch.py requires Linux (needs /sys, /dev/i2c-*, /dev/port). Exiting.")
        return 1

    force = "--force" in sys.argv[1:]
    check_preconditions(force)

    out("sx8635-watch.py -- read-only SX8635 diagnostic")
    out("Generated on: %s" % (socket.gethostname() or "unknown"))

    bus = find_i801_bus()
    try:
        # O_RDWR here is the i2c-dev/SMBus ioctl protocol's requirement for a
        # read/write-capable fd -- the fd itself is never used for a plain
        # write() or any write ioctl, only I2C_SMBUS transfers with rw=READ
        # (see _smbus_read, the only function that touches this fd).
        i2c_fd = os.open("/dev/i2c-%d" % bus, os.O_RDWR)
        fcntl.ioctl(i2c_fd, I2C_SLAVE, SX8635_ADDR)
    except OSError as e:
        die("could not open /dev/i2c-%d and address 0x%02x: %s" % (bus, SX8635_ADDR, e))

    try:
        with open("/sys/bus/pci/devices/0000:00:1f.0/config", "rb") as f:
            cfg = f.read(0x4D)
        gpiobase = _parse_gpiobase(cfg)
    except OSError as e:
        die("could not read PCI config for GPIOBASE: %s" % e)
    nirq_port, nirq_bit = _ich_line_addr(gpiobase, 2)   # NIRQ = gpio_ich line 2

    try:
        port_fd = os.open("/dev/port", os.O_RDONLY)   # read-only: never writes /dev/port
    except OSError as e:
        die("could not open /dev/port read-only: %s" % e)

    overall = {
        "nvm_valid": None, "nvm_count": None,
        "button_bits_seen": False, "wheel_activity_seen": False,
        "nirq_ever_low": False, "nirq_zero_to_one_after_read": 0,
        "irq_nonzero_count": 0, "attempted_reads": 0, "oserror_count": 0,
    }

    try:
        startup_block(i2c_fd, port_fd, nirq_port, nirq_bit, overall)
    except OSError as e:
        die("chip read failed during startup: %s -- see the 'chip absent' verdict row" % e)

    out("\n=== Phased interactive trace, ~60s total ===")
    state = {
        "last_read_t": 0.0, "sub_loop": False, "oserror_count": 0,
        "last_r1": None, "last_r2": None, "last_r3": None, "last_r4": None,
        "last_pos": None, "t0": time.perf_counter(),
    }
    for phase in PHASES:
        countdown(phase["prompt"])
        out("\n>>> phase %s (%ds): %s" % (phase["name"], phase["secs"], phase["prompt"]))
        psum = run_phase(i2c_fd, port_fd, nirq_port, nirq_bit, phase, state, overall)
        print_phase_summary(phase, psum)

    out("\n=== Overall ===")
    out("attempted reads: %d   OSError count: %d" % (overall["attempted_reads"], overall["oserror_count"]))
    verdict = compute_verdict(overall)
    out("VERDICT: %s" % verdict)

    out_path = write_report()
    print()
    print("Please paste the contents of %s (or attach it) into the issue," % out_path)
    print("and say which physical pad you touched during the OK and OTHER phases --")
    print("this tool only knows which phase was running, not which pad is which.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
