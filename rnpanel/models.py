import os, sys, re
from rnpanel.gpio import DnvMmioGpio, IchPortGpio
from rnpanel.lcd import INIT_SEQ

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
            # reg 0x02 bit-mask -> action, confirmed by a real-hardware
            # mapping run (see docs/porting.md for the full table). This is
            # an ORDERED LIST of (mask, action) pairs, not a dict, checked
            # in order -- first whole-mask match wins, so the check order is
            # explicit and self-describing rather than relying on dict
            # iteration order. UP and LEFT have no button bits at all on
            # this board: they show up purely as ring positions (~0x09 for
            # UP, ~0x1e for LEFT), so "page back" happens by turning the
            # ring counter-clockwise, not by pressing a button.
            "keys": [
                (0x02, "OK"),      # bit 1: centre OK pad -> refresh
                (0x0c, "NEXT"),    # bits 2|3: DOWN pad -> NEXT page (mirrors the RN426's DOWN->NEXT)
                (0x30, "RIGHT"),   # bits 4|5: RIGHT pad -> activity-only, like the RN426's LEFT/RIGHT
                                   # (run()/_apply_actions already treats an unrecognized
                                   # action as activity-only: resets idle, wakes if asleep,
                                   # never moves a page)
            ],
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
