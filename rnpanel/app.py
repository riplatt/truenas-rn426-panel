import os, sys, time
from rnpanel.models import MODELS, detect_model
from rnpanel.lcd import LCD
from rnpanel.msp430 import Msp430Buttons
from rnpanel.sx8635 import Sx8635Buttons
from rnpanel.pages import PAGES
from rnpanel.i2c import fcntl, I2C_SLAVE, find_i801_bus
from rnpanel import sx8635_spm

def _build(model):
    spec = MODELS[model]
    if model == "rn316":
        print("WARNING: rn316 display confirmed working on one real unit; "
              "buttons are read-only (SX8635 touch-wheel, no SPM/NVM writes), "
              "button-to-pad mapping confirmed on real hardware -- see "
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

def _open_sx8635_fd(addr):
    """Open /dev/i2c-N and bind it to the SX8635's address -- the same two
    calls Sx8635Buttons.__init__ makes. Used only for the one-shot SPM
    sentinel read at startup (_choose_sx8635_layout); the fd is closed again
    right after, before Sx8635Buttons opens its own for the daemon's whole
    run."""
    fd = os.open("/dev/i2c-%d" % find_i801_bus(), os.O_RDWR)
    fcntl.ioctl(fd, I2C_SLAVE, addr)
    return fd


def _choose_sx8635_layout(addr, spm=sx8635_spm, open_fd=_open_sx8635_fd, close_fd=os.close):
    """Pick which of MODELS["rn316"]["sx8635"]["layouts"] to run, by reading
    SPM block 1 through `spm` (rnpanel.sx8635_spm; overridable for tests).
    Lives here, not in rnpanel/sx8635.py, so that module stays completely
    write-free and its no-write source scans keep meaning what they say --
    Sx8635Buttons itself never imports or calls sx8635_spm.

    recover() and read_block1() only ever write the whitelisted SPM
    window-select registers (0x0D/0x0E) to open and close the read window --
    that's the one write this daemon ever issues, and it's accepted by
    design (see sx8635_spm.py's module docstring). Nothing here ever writes
    the chip's actual configuration.

    RN_SX8635_SPM=0 skips opening the device entirely -- no window read, no
    write of any kind -- and forces the factory ("qsm") layout. Any OSError
    (missing bus, NAK'd/absent chip) also forces qsm, as does an
    unrecognized CapMode (logged as a WARN). Exactly one line is printed to
    stderr per call (i.e. once per daemon start), always naming the layout
    chosen. Returns the layout name, "netgear" or "qsm"."""
    if os.environ.get("RN_SX8635_SPM") == "0":
        print("SX8635 SPM read skipped (RN_SX8635_SPM=0) -> layout qsm (forced)",
              file=sys.stderr)
        return "qsm"
    try:
        fd = open_fd(addr)
        try:
            spm.recover(fd)
            block = spm.read_block1(fd)
        finally:
            close_fd(fd)
    except OSError as e:
        print("SX8635 SPM block 1 read failed (%s) -> layout qsm" % e, file=sys.stderr)
        return "qsm"
    hexdump = " ".join("%02x" % b for b in block)
    name = spm.layout_of(block)
    if name == "netgear":
        print("SX8635 SPM block 1 = %s -> layout netgear (read only, nothing written)"
              % hexdump, file=sys.stderr)
        return "netgear"
    if name == "qsm":
        print("SX8635 SPM block 1 = %s -> layout qsm (read only, nothing written)"
              % hexdump, file=sys.stderr)
        return "qsm"
    print("WARN: SX8635 SPM block 1 = %s -> unrecognized CapMode, forcing layout qsm "
          "(read only, nothing written)" % hexdump, file=sys.stderr)
    return "qsm"


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
            sx = spec["sx8635"]
            layout_name = _choose_sx8635_layout(sx["addr"])
            btn_spec = dict(sx["layouts"][layout_name])
            btn_spec["addr"] = sx["addr"]
            return cls(gpio, btn_spec)
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
