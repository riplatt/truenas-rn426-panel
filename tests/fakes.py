"""Shared test doubles for the rnpanel test suite. Not a test module itself
(no test_ prefix), so unittest discover won't try to collect it -- it's
imported by the test_*.py files, each of which puts both the repo root and
this directory (tests/) on sys.path before importing it.
"""
from rnpanel.gpio import IchPortGpio
from rnpanel.msp430 import Msp430Buttons
from rnpanel.sx8635 import Sx8635Buttons
from rnpanel.models import MODELS

# Pin maps mirroring MODELS["rnx26"]["pins"] / MODELS["rn316"]["pins"], kept
# as local literals (not imported from MODELS) so these tests independently
# pin down the numbers the brief specifies -- if MODELS ever drifts, the
# equivalence/derivation tests below should catch it.
RNX26_PINS = {"MOSI": 54, "CLK": 1, "DC": 32, "CS": 50, "EN": 6, "RST": 7, "BTN_INT": 2}
RN316_PINS = {"MOSI": 21, "CLK": 19, "DC": 16, "CS": 7, "EN": 32, "RST": 24, "BTN_INT": 2}
# Synthetic pin map for the "no known interrupt line" case -- RN316 no
# longer represents that (its BTN_INT is confirmed to be 2, see MODELS).
NO_INT_LINE_PINS = dict(RNX26_PINS, BTN_INT=None)


class FakeIchPortGpio(IchPortGpio):
    """An IchPortGpio built without touching any real device: __init__ is
    skipped, gpiobase/pins/ngpio are fixed by the caller, and _rd_byte/
    _wr_byte are backed by a plain dict standing in for the whitelisted I/O
    ports. Reuses the real _derive_maps() (which itself calls _check_ngpio)
    so these tests exercise production logic, not a reimplementation of it.
    ngpio defaults to 76 (rnx26's line count) so existing RNX26_PINS-based
    tests don't need to pass it explicitly."""

    def __init__(self, pins, gpiobase=0, ngpio=76):
        self.pins = pins
        self.has_int_line = pins["BTN_INT"] is not None
        self.gpiobase = gpiobase
        self.ngpio = ngpio
        self.port = {}
        self._derive_maps()

    def _rd_byte(self, port):
        return self.port.get(port, 0)

    def _wr_byte(self, port, val):
        if port not in self._whitelist:
            raise ValueError("not whitelisted: 0x%x" % port)
        val |= self._force.get(port, 0)
        self.port[port] = val & 0xFF


class FakeGpio:
    """Stands in for a Gpio backend: int_active() is whatever the test set."""
    def __init__(self, active=False):
        self.active = active

    def int_active(self):
        return self.active


class FakeMsp430Buttons(Msp430Buttons):
    """Msp430Buttons with the real __init__ (which opens /dev/i2c-N) skipped.
    events() itself is unmodified production code; only _read is faked, via
    a settable `value` returned on every read (plus a call counter so tests
    can assert exactly one read happens per interrupt-pad assertion)."""

    def __init__(self, gpio):
        self.gpio = gpio
        self.prev = 0
        self._armed = True
        self._high_since = 0.0
        self.value = 0
        self.read_count = 0

    def _read(self, reg):
        self.read_count += 1
        return self.value


SX8635_SPEC = MODELS["rn316"]["sx8635"]


class FakeSx8635Buttons(Sx8635Buttons):
    """Sx8635Buttons with the real __init__ (which opens /dev/i2c-N and does
    a live first read) skipped. Tests load `queue` with the register bytes
    a read sequence should return, in call order; _read pops from it."""

    def __init__(self, gpio, spec):
        self.gpio = gpio
        self.addr = spec["addr"]
        self.wheel_range = spec["wheel_range"]
        self.detent = spec["detent"]
        self.keys = spec["keys"]
        self.prev_bitmap = 0
        self.wheel_touched = False
        self.wheel_prev_pos = None
        self.wheel_acc = 0
        self.wheel_stale_since = None
        self.last_read = 0.0
        self.queue = []

    def _read(self, reg):
        return self.queue.pop(0)


def _sx8635(keys=None, gpio_active=True):
    spec = dict(SX8635_SPEC)
    spec["keys"] = [] if keys is None else keys
    return FakeSx8635Buttons(FakeGpio(active=gpio_active), spec)
