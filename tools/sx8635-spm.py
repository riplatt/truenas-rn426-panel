#!/usr/bin/env python3
"""
sx8635-spm.py -- the only tool in this repo that writes to the RN316's
SX8635 touch-wheel controller. See rnpanel/sx8635_spm.py's module docstring
for exactly what it can and cannot write, and why; this file is a thin CLI
around that module, plus a read-only `dump` of the full 128-byte SPM.

Subcommands:
  dump             -- read and decode all 16 SPM blocks (128 bytes),
                       read-only, via its own separate whitelist. Always
                       closes its window afterwards, even on error.
  capmode [--yes]  -- without --yes: print what would change, write
                       nothing, exit 0. With --yes: apply the CapMode
                       change (rnpanel.sx8635_spm.apply_capmode), print
                       before/after block-1 bytes and the result, then a
                       full read-only dump of the chip and next steps.
                       Refuses to write over a chip state it doesn't
                       recognise (only the factory "qsm" sentinel is ever
                       written over) -- see apply_capmode's docstring.
  close            -- recover() + force the SPM window closed. Use this
                       if a previous run of this tool (or a crash) left
                       it open.

Same guardrails as tools/sx8635-watch.py: refuses to run unless DMI
product_name is "ReadyNAS 316" (--force overrides), refuses if
rn426-panel.service is active (both this tool and the daemon read IrqSrc,
which clears on read, so they'd steal each other's events), exits
gracefully off Linux, and tees its report to /tmp/sx8635-spm-<host>.txt.

Every exit path -- success, a refused precondition, an unexpected
exception, Ctrl-C, or SIGTERM -- closes the chip fd, makes a best-effort
attempt to leave the SPM window closed, and writes the report file. Nothing
here retries a failed write or resets the chip; see
rnpanel/sx8635_spm.py's module docstring.
"""
import glob
import os
import signal
import sys
import socket
import subprocess
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# rnpanel.i2c/rnpanel.sx8635_spm guard their own fcntl import for Windows
# importability -- see rnpanel/i2c.py's header. Importing them here does not
# require fcntl at import time, only when a real chip fd is opened below.
from rnpanel.i2c import fcntl, I2C_SLAVE
from rnpanel import sx8635_spm as spm

SX8635_ADDR = 0x2B

CAPMODE_NAMES = {0: "Disabled", 1: "Button", 2: "Reserved", 3: "Wheel"}

# --------------------------------------------------------------------------
# Report buffering, same shape as tools/sx8635-watch.py.
# --------------------------------------------------------------------------
OUT_LINES = []


def out(s=""):
    print(s)
    OUT_LINES.append(s)


def write_report():
    hostname = socket.gethostname() or "unknown"
    default_path = "/tmp/sx8635-spm-%s.txt" % hostname
    out_path = os.environ.get("SX_SPM_OUT", default_path)
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(OUT_LINES) + "\n")
        sys.stderr.write("\n(report also written to %s)\n" % out_path)
    except OSError as e:
        sys.stderr.write("\n(could not write report file %s: %s)\n" % (out_path, e))
    return out_path


def die(msg):
    out()
    out("ABORT: %s" % msg)
    write_report()
    sys.exit(1)


# --------------------------------------------------------------------------
# Preconditions -- identical policy to tools/sx8635-watch.py's
# check_preconditions, duplicated here rather than imported so this tool
# has no dependency on that (hyphenated-filename) module.
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


def resolve_i801_bus():
    """Find the i801 SMBus adapter by name, or return None. Deliberately NOT
    rnpanel.i2c.find_i801_bus(): that helper falls back to bus 1 when no
    adapter named I801 exists (fine for the daemon, which only ever probes
    an address that either answers or doesn't), but this tool WRITES the
    chip at whatever address answers -- silently guessing bus 1 under
    --force could aim a write at an unrelated bus (e.g. a DDC/monitor bus)."""
    for d in sorted(glob.glob("/sys/class/i2c-dev/i2c-*")):
        try:
            if "I801" in open(os.path.join(d, "name")).read():
                return int(d.rsplit("-", 1)[1])
        except OSError:
            pass
    return None


def open_chip():
    bus = resolve_i801_bus()
    if bus is None:
        die("no i801 SMBus adapter found under /sys/class/i2c-dev -- refusing to guess a "
            "bus number for a tool that writes the chip")
    try:
        fd = os.open("/dev/i2c-%d" % bus, os.O_RDWR)
        fcntl.ioctl(fd, I2C_SLAVE, SX8635_ADDR)
    except OSError as e:
        die("could not open /dev/i2c-%d and address 0x%02x: %s" % (bus, SX8635_ADDR, e))
        return None   # unreachable, die() exits; keeps linters happy
    return fd


# --------------------------------------------------------------------------
# dump: its own read-only window walk over all 16 SPM blocks, via
# rnpanel.sx8635_spm's SEPARATE dump whitelist (_wr_dump/DUMP_WHITELIST).
# This never shares a code path with apply_capmode().
# --------------------------------------------------------------------------
def read_all_spm(fd):
    """Read all 128 SPM bytes (16 blocks of 8), read-only. Always closes
    the window in finally, even on an exception mid-read."""
    spm._wr_dump(fd, spm.REG_SPMCFG, spm.SPM_READ_OPEN)
    try:
        data = []
        for base in range(0x00, 0x80, 8):
            spm._wr_dump(fd, spm.REG_SPMBASE, base)
            for i in range(8):
                data.append(spm._rd(fd, i))
        return data
    finally:
        spm._wr_dump(fd, spm.REG_SPMCFG, spm.SPM_CLOSED)


def decode_capmode_byte(byte, hi_pin):
    """byte packs two-bits-per-pin for four CAP pins, hi_pin down to
    hi_pin-3, matching the datasheet's CapMode11_8/7_4/3_0 pin ordering."""
    pins = []
    for shift, offset in ((6, 0), (4, 1), (2, 2), (0, 3)):
        pin = hi_pin - offset
        val = (byte >> shift) & 0x3
        pins.append((pin, CAPMODE_NAMES[val]))
    return pins


def format_capmode(label, byte, hi_pin):
    parts = ["CAP%d=%s" % (pin, name) for pin, name in decode_capmode_byte(byte, hi_pin)]
    return "%-12s 0x%02x: %s" % (label, byte, ", ".join(parts))


def print_spm_dump(data):
    out("\n=== SPM dump (128 bytes) ===")
    for row in range(0, 128, 8):
        out("0x%02x: " % row + " ".join("%02x" % b for b in data[row:row + 8]))

    block1 = tuple(data[0x08:0x10])
    out("\nlayout: %s" % spm.layout_of(block1))

    out("\n=== Key fields ===")
    out("I2CAddress        0x04 = 0x%02x" % data[0x04])
    out("ActiveScanPeriod  0x05 = 0x%02x" % data[0x05])
    out("DozeScanPeriod    0x06 = 0x%02x" % data[0x06])
    out("CapModeMisc       0x09 = 0x%02x" % data[0x09])
    out(format_capmode("CapMode11_8", data[0x0A], 11))
    out(format_capmode("CapMode7_4", data[0x0B], 7))
    out(format_capmode("CapMode3_0", data[0x0C], 3))
    out("CapSensitivity0_1..4_5  0x0D-0x0F = " + " ".join("0x%02x" % b for b in data[0x0D:0x10]))
    out("CapThresh0..11          0x13-0x1E = " + " ".join("0x%02x" % b for b in data[0x13:0x1F]))
    out("WhlNorm           0x2B/0x2C = 0x%02x%02x" % (data[0x2B], data[0x2C]))
    out("WhlRotateThresh   0x30 = 0x%02x" % data[0x30])
    out("CapProxEnable     0x70 = 0x%02x" % data[0x70])


def print_spmstat(fd):
    spmstat = spm._rd(fd, spm.REG_SPMSTAT)
    nvm_valid = bool(spmstat & 0x08)
    nvm_count = spmstat & 0x07
    out("\nSpmStat  0x08 = 0x%02x : NvmValid=%d NvmCount=%d" % (spmstat, nvm_valid, nvm_count))


def cmd_dump(fd):
    data = read_all_spm(fd)
    print_spm_dump(data)
    print_spmstat(fd)
    return 0


def cmd_capmode(fd, yes):
    if not yes:
        out("Would read SPM block 1 (SPM 0x08-0x0F). If CapMode (0x0A-0x0C) is not")
        out("already 0x%02x 0x%02x 0x%02x, and the block otherwise decodes as the factory"
            % spm.CAPMODE)
        out("QSM layout, would write those three bytes (leaving the other five bytes in")
        out("the block exactly as read), wait for the SPM-write-done interrupt or a short")
        out("timeout, verify by re-reading, and trigger sensor compensation. A chip that")
        out("doesn't decode as either the factory or the target layout is left untouched.")
        out("Pass --yes to actually do it. Nothing was written.")
        return 0

    spm.recover(fd)
    before = spm.read_block1(fd)
    out("before: block1 = " + " ".join("%02x" % b for b in before))
    out("before layout: %s" % spm.layout_of(before))

    result, after = spm.apply_capmode(fd)
    out("result: %s" % result)
    out("block1 (after) = " + " ".join("%02x" % b for b in after))
    out("layout (after): %s" % spm.layout_of(after))

    if result == "unexpected":
        out("\nRefusing to write: the sentinel read didn't decode as either the factory")
        out("QSM layout or the target netgear layout, so this isn't a chip state this")
        out("tool knows how to write over safely. Nothing was written. The eight bytes")
        out("above are exactly what was read.")
        return 1

    if result == "verify-failed":
        out("\nWARNING: verify failed -- the chip was NOT confirmed to have the new")
        out("CapMode written. Staying on whatever layout after-bytes show above.")
        out("This tool does NOT retry and does NOT reset the chip -- see")
        out("rnpanel/sx8635_spm.py's module docstring for why.")
    elif result == "write-error":
        out("\nWARNING: the block write raised an I/O error. block1 above is a fresh")
        out("re-read taken after the error, not a retried write -- it may or may not")
        out("reflect the intended change. This tool does NOT retry.")

    out("\n=== full SPM dump after capmode ===")
    data = read_all_spm(fd)
    print_spm_dump(data)
    print_spmstat(fd)

    out("\nNext steps:")
    out("  - run tools/sx8635-watch.py --tap to confirm the new layout responds")
    out("  - SPM is volatile: after a cold power cycle, re-run 'capmode --yes' again --")
    out("    the daemon does not yet re-apply this itself.")
    return 0 if result == "applied" else 1


def cmd_close(fd):
    spm.recover(fd)
    spm._wr(fd, spm.REG_SPMCFG, spm.SPM_CLOSED)
    out("SPM window closed (recover + explicit close).")
    return 0


COMMANDS = {"dump": lambda fd, yes: cmd_dump(fd),
            "capmode": lambda fd, yes: cmd_capmode(fd, yes),
            "close": lambda fd, yes: cmd_close(fd)}


def main():
    if os.name != "posix" or not os.path.isdir("/sys"):
        print("sx8635-spm.py requires Linux (needs /sys, /dev/i2c-*). Exiting.")
        return 1

    args = sys.argv[1:]
    force = "--force" in args
    yes = "--yes" in args
    positional = [a for a in args if not a.startswith("--")]

    if len(positional) != 1 or positional[0] not in COMMANDS:
        print("usage: sx8635-spm.py {dump|capmode|close} [--yes] [--force]")
        return 1
    cmd = positional[0]

    check_preconditions(force)

    out("sx8635-spm.py -- SX8635 SPM tool (%s)" % cmd)
    out("Generated on: %s" % (socket.gethostname() or "unknown"))

    # A SIGTERM during the command below would otherwise skip every finally
    # block on this and the module's side (Python only unwinds `finally` for
    # KeyboardInterrupt/SystemExit, not an unhandled signal) -- turn it into
    # a SystemExit so the except clause below still runs.
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(1))

    fd = open_chip()
    rc = 1
    try:
        rc = COMMANDS[cmd](fd, yes)
    except BaseException as e:   # includes KeyboardInterrupt and SystemExit
        out("\nERROR: %s: %s" % (type(e).__name__, e))
        try:
            spm.recover(fd)
            out("SPM window: closed (recover)")
        except OSError as e2:
            out("SPM window: could not confirm closed (%s) -- run: sx8635-spm.py close" % e2)
        if not isinstance(e, (KeyboardInterrupt, SystemExit)):
            traceback.print_exc()
        rc = 1
    finally:
        os.close(fd)
        write_report()

    return rc


if __name__ == "__main__":
    sys.exit(main())
