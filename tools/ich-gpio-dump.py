#!/usr/bin/env python3
"""
ich-gpio-dump.py -- stage A survey for porting this driver to the ReadyNAS
RN52x/62x family (see issues #4 and #5).

Those boards use a C224 "Lynx Point" PCH (gpio_ich), not the Denverton SoC
this driver's LCD path writes -- see rn426_panel.py's header and
docs/porting.md. Before anyone writes a single register on a 52x/62x box,
someone needs to see what the firmware already left in the gpio_ich block.
That is all this tool does.

*** STRICTLY READ-ONLY ***
This script performs NO writes anywhere: no /dev/port writes, no PCI config
writes (CF8/CFC), no sysfs writes, no /dev/mem at all. It only opens sysfs
files for reading and reads individual bytes from /dev/port. Reading GP_LVL
(the pin level register) is side-effect-free -- unlike some MCU registers
elsewhere in this driver's history, reading a GPIO level pad does not
disturb anything. It loads no kernel module; gpio_ich, if present, is left
alone.

Run as root (needed for /sys PCI config and /dev/port):
    sudo python3 tools/ich-gpio-dump.py

Output is printed to stdout AND tee'd to a file (default
/tmp/ich-gpio-dump-<hostname>.txt, override with env ICH_DUMP_OUT). Paste
the whole thing, plus the DSDT this tool tells you to attach, into the
GitHub issue tracking the 52x/62x port.

Env:
    ICH_DUMP_OUT  -- output file path (default /tmp/ich-gpio-dump-<host>.txt)
    ICH_DUMP_SECS -- seconds to sample GP_LVL for the activity trace (default 60)
"""
import os, sys, socket, struct, time

LPC_DEV = "/sys/bus/pci/devices/0000:00:1f.0"
EXPECT_VENDOR = 0x8086
EXPECT_DEVICE = 0x8c54  # C224 "Lynx Point" LPC bridge

# gpio_ich line -> (bank, bit). bank n covers lines [n*32, n*32+31].
LINES_OF_INTEREST = [
    (54, "MOSI"),
    (1,  "CLK"),
    (32, "DC"),
    (50, "CS"),
    (6,  "EN"),
    (7,  "RESET"),
    (2,  "BTN_INT"),
]
OLED_LINES = {"MOSI", "CLK", "DC", "CS", "EN", "RESET"}  # expected outputs
INPUT_LINES = {"BTN_INT"}                                # expected inputs

# per-bank register offsets from GPIOBASE (bank 0 / bank 1 / bank 2)
REG_USE_SEL = [0x00, 0x30, 0x40]
REG_IO_SEL  = [0x04, 0x34, 0x44]
REG_LVL     = [0x0C, 0x38, 0x48]
# bank-0-only extras
REG_GPO_BLINK   = 0x18
REG_GP_SER_BLINK = 0x1C
REG_GPI_INV     = 0x2C

OUT_LINES = []  # buffered output, tee'd to stdout + file at the end


def out(s=""):
    print(s)
    OUT_LINES.append(s)


def hr():
    out("-" * 70)


def section(title):
    out()
    out("=== %s ===" % title)


def die(msg):
    out()
    out("ABORT: %s" % msg)
    flush_and_exit(1)


def flush_and_exit(code):
    hostname = socket.gethostname() or "unknown"
    default_path = "/tmp/ich-gpio-dump-%s.txt" % hostname
    out_path = os.environ.get("ICH_DUMP_OUT", default_path)
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(OUT_LINES) + "\n")
        sys.stderr.write("\n(report also written to %s)\n" % out_path)
    except OSError as e:
        sys.stderr.write("\n(could not write report file %s: %s)\n" % (out_path, e))
    sys.exit(code)


def read_sysfs_hex(path):
    """Read a sysfs file like .../vendor which contains '0x8086\\n'."""
    with open(path) as f:
        return int(f.read().strip(), 16)


def read_config_dword(offset):
    """Read a little-endian dword from PCI config space via sysfs, NOT via
    /dev/port CF8/CFC -- sysfs config access is the read-only-safe path."""
    path = os.path.join(LPC_DEV, "config")
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(4)
    if len(data) != 4:
        raise OSError("short read of PCI config at offset 0x%x" % offset)
    return struct.unpack("<I", data)[0]


class PortReader:
    """Reads /dev/port one byte at a time and assembles little-endian dwords.
    Dword-sized reads on /dev/port are not portable across kernels, so this
    always does 4 single-byte pread() calls. Never writes."""

    def __init__(self):
        self.fd = os.open("/dev/port", os.O_RDONLY)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def read_dword(self, port):
        b = bytearray(4)
        for i in range(4):
            b[i] = os.pread(self.fd, 1, port + i)[0]
        return struct.unpack("<I", bytes(b))[0]


def main():
    hr()
    out("ich-gpio-dump.py report -- paste this whole thing into the GitHub issue")
    out("Generated on: %s" % (socket.gethostname() or "unknown"))
    hr()

    if os.name != "posix" or not os.path.isdir("/sys"):
        die("this tool needs Linux sysfs (/sys); it can't run on this OS. "
            "Run it on the ReadyNAS itself (or a Linux box with the hardware).")

    if os.geteuid() != 0:
        die("this needs root (sysfs PCI config + /dev/port reads). "
            "Re-run as: sudo python3 tools/ich-gpio-dump.py")

    # --- 1. LPC bridge identity -------------------------------------------------
    section("1. LPC bridge identity")
    if not os.path.isdir(LPC_DEV):
        die("%s does not exist -- no PCI device at 00:1f.0 on this system. "
            "This tool only applies to Intel PCH-based boards (RN52x/62x)." % LPC_DEV)
    try:
        vendor = read_sysfs_hex(os.path.join(LPC_DEV, "vendor"))
        device = read_sysfs_hex(os.path.join(LPC_DEV, "device"))
    except OSError as e:
        die("could not read vendor/device from %s: %s" % (LPC_DEV, e))
    out("vendor:device = 0x%04x:0x%04x" % (vendor, device))
    if vendor != EXPECT_VENDOR or device != EXPECT_DEVICE:
        out("NOTE: expected 0x%04x:0x%04x (C224 'Lynx Point'). This board's LPC "
            "bridge is different -- this dump may not apply to it. Continuing "
            "anyway so you can still capture what's here." % (EXPECT_VENDOR, EXPECT_DEVICE))
    else:
        out("matches expected C224 'Lynx Point' LPC bridge.")

    # --- 2. GPIOBASE --------------------------------------------------------
    section("2. GPIOBASE (PCI config offset 0x48, sysfs read only)")
    try:
        raw = read_config_dword(0x48)
    except OSError as e:
        die("could not read PCI config offset 0x48: %s" % e)
    gpiobase = raw & 0x0000ff80
    out("raw dword @0x48 = 0x%08x" % raw)
    out("GPIOBASE (masked 0x0000ff80) = 0x%04x" % gpiobase)
    if gpiobase == 0:
        die("GPIOBASE is 0 -- firmware hasn't programmed a GPIO I/O base on "
            "this board, so there is nothing to dump. No writes have been "
            "made by this tool.")

    # --- 3. Register dump via /dev/port reads -------------------------------
    section("3. Register dump (/dev/port reads only, byte-at-a-time)")
    try:
        port = PortReader()
    except OSError as e:
        die("could not open /dev/port for reading: %s" % e)

    regs = {}  # (bank, name) -> value or None

    def safe_read(bank, offset, name):
        addr = gpiobase + offset
        try:
            val = port.read_dword(addr)
            out("bank %d  %-16s (port 0x%04x) = 0x%08x" % (bank, name, addr, val))
            regs[(bank, name)] = val
        except OSError as e:
            out("bank %d  %-16s (port 0x%04x) = READ FAILED: %s" % (bank, name, addr, e))
            regs[(bank, name)] = None

    for bank in (0, 1, 2):
        safe_read(bank, REG_USE_SEL[bank], "GPIO_USE_SEL")
        safe_read(bank, REG_IO_SEL[bank], "GP_IO_SEL")
        safe_read(bank, REG_LVL[bank], "GP_LVL")
    safe_read(0, REG_GPO_BLINK, "GPO_BLINK")
    safe_read(0, REG_GP_SER_BLINK, "GP_SER_BLINK")
    safe_read(0, REG_GPI_INV, "GPI_INV")

    # --- 4. Decode pins of interest ------------------------------------------
    section("4. Pins of interest")
    all_match = True
    blink_val = regs.get((0, "GPO_BLINK"))
    for line, name in LINES_OF_INTEREST:
        bank, bit = line // 32, line % 32
        use_sel = regs.get((bank, "GPIO_USE_SEL"))
        io_sel = regs.get((bank, "GP_IO_SEL"))
        lvl = regs.get((bank, "GP_LVL"))

        def bitval(reg):
            return None if reg is None else (reg >> bit) & 1

        u, i, l = bitval(use_sel), bitval(io_sel), bitval(lvl)
        blink_str = ""
        if bank == 0 and blink_val is not None:
            blink_str = "  BLINK=%d" % ((blink_val >> bit) & 1)

        def fmt(v):
            return "?" if v is None else str(v)

        out("line %2d (%-8s) bank=%d bit=%2d  USE_SEL=%s IO_SEL=%s LVL=%s%s"
            % (line, name, bank, bit, fmt(u), fmt(i), fmt(l), blink_str))

        expect_use_sel = 1
        if name in OLED_LINES:
            expect_io_sel = 0  # output
        elif name in INPUT_LINES:
            expect_io_sel = 1  # input
        else:
            expect_io_sel = None
        if u != expect_use_sel or (expect_io_sel is not None and i != expect_io_sel):
            all_match = False

    section("Verdict")
    out("Expected picture: USE_SEL=1 (GPIO, not native) on all seven lines; "
        "MOSI/CLK/DC/CS/EN/RESET as outputs (IO_SEL=0); BTN_INT as an input (IO_SEL=1).")
    if all_match:
        out("MATCH: the register picture matches expectations for this pin map.")
    else:
        out("MISMATCH: the register picture does NOT match expectations. "
            "The pin map above may be wrong for this board -- do not proceed "
            "to any write-capable stage until this is sorted out.")

    # --- 5. Time-series sample of GP_LVL -------------------------------------
    secs = float(os.environ.get("ICH_DUMP_SECS", "60"))
    section("5. Activity trace: sampling GP_LVL at 10 Hz for %.0fs" % secs)
    out("(reads only -- reading GP_LVL has no side effects)")
    changed = [set(), set(), set()]  # changed bit positions per bank
    last = [None, None, None]
    n_samples = 0
    t_end = time.time() + secs
    while time.time() < t_end:
        for bank in (0, 1, 2):
            addr = gpiobase + REG_LVL[bank]
            try:
                val = port.read_dword(addr)
            except OSError:
                continue
            if last[bank] is not None and val != last[bank]:
                diff = val ^ last[bank]
                for bit in range(32):
                    if diff & (1 << bit):
                        changed[bank].add(bit)
            last[bank] = val
        n_samples += 1
        time.sleep(0.1)

    port.close()

    out("samples taken: %d" % n_samples)
    any_activity = False
    for bank in (0, 1, 2):
        if changed[bank]:
            any_activity = True
            lines = sorted(bank * 32 + b for b in changed[bank])
            out("bank %d: ACTIVITY on lines %s" % (bank, lines))
        else:
            out("bank %d: QUIET (no bit changed)" % bank)

    watch_bits = {
        0: set(range(0, 8)),
        1: set(range(0, 8)) | set(range(16, 24)),
    }
    flagged = []
    for bank, bits in watch_bits.items():
        hit = changed[bank] & bits
        if hit:
            flagged.append("bank %d bits %s" % (bank, sorted(hit)))
    if flagged:
        out("FLAG: activity on bytes a future driver would read-modify-write: "
            + "; ".join(flagged))
        out("(if you pressed a button during this trace, line 2 changing is expected)")
    else:
        out("No activity flagged on the bytes a future driver would read-modify-write.")

    if not any_activity:
        out("Overall: QUIET for the whole sample window.")

    # --- 6. DSDT ---------------------------------------------------------------
    section("6. DSDT (for the issue -- not parsed here)")
    dsdt_path = "/sys/firmware/acpi/tables/DSDT"
    if os.path.exists(dsdt_path):
        try:
            size = os.path.getsize(dsdt_path)
            out("found %s (%d bytes)." % (dsdt_path, size))
        except OSError as e:
            out("found %s but could not stat it: %s" % (dsdt_path, e))
        out("Please attach this file to the GitHub issue too (it's binary, "
            "so paste it as a file, not inline text). As root:")
        out("    sudo cp %s /tmp/dsdt-$(hostname).dat" % dsdt_path)
        out("then attach /tmp/dsdt-<hostname>.dat to the issue. Root is needed "
            "to read it; a normal user copy will fail with a permission error.")
    else:
        out("%s not found on this system -- skip this step." % dsdt_path)

    # --- 7. Footer ---------------------------------------------------------------
    section("7. Done")
    out("This tool wrote nothing: no /dev/port writes, no PCI config writes, "
        "no sysfs writes, no kernel modules loaded.")
    out("Please paste the WHOLE output above (or attach the report file) plus "
        "the DSDT file into the GitHub issue tracking the 52x/62x port.")

    flush_and_exit(0)


if __name__ == "__main__":
    main()
