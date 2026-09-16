import os, ctypes
from rnpanel.i2c import fcntl, I2C_SLAVE, I2C_SMBUS, _smbus_ioctl, find_i801_bus

# --------------------------------------------------------------------------
# Pure wheel/button math for Sx8635Buttons -- no I/O, testable standalone.
# --------------------------------------------------------------------------
def _wheel_delta(pos, prev, wheel_range):
    """Wrap-aware signed delta from prev to pos on a ring of wheel_range
    positions (0..wheel_range-1 wrapping around). Result is in
    [-wheel_range//2, wheel_range//2)."""
    half = wheel_range // 2
    return ((pos - prev + half) % wheel_range) - half

def _wheel_emit(acc, detent):
    """Consume a signed raw-tick accumulator into whole-detent NEXT/PREV
    events, returning (events, remaining_acc). Clockwise = position
    DECREASING = negative acc = NEXT; counter-clockwise = positive acc =
    PREV (confirmed on real rn316 hardware, see MODELS comment)."""
    events = []
    while acc <= -detent:
        events.append("NEXT")
        acc += detent
    while acc >= detent:
        events.append("PREV")
        acc -= detent
    return events, acc

# reg 0x02 bit 0: set on almost any touch (pad or ring alike) -- never a
# button by itself. Always masked out before any rising-edge check below.
COMMON_TOUCH_BIT = 0x01

def _rising_actions(bitmap, prev_bitmap, keys):
    """Map reg 0x02 (bitmap vs prev_bitmap) through `keys`, an ORDERED LIST
    of (mask, action) pairs, and return at most one action.

    COMMON_TOUCH_BIT (bit 0) is masked out of both bitmap and prev_bitmap
    before anything else -- it sets on almost any touch (pad or ring) and
    must never itself be treated as a button, and must never make an
    otherwise-empty masked bitmap look "nonzero".

    If `keys` is empty (the provisional/unmapped-model shape), the
    fallback is "masked bitmap went from zero to nonzero" -> one ["OK"];
    otherwise no action.

    If `keys` is given, each (mask, action) pair is checked in order for a
    RISING EDGE ON THE WHOLE MASK: all of the mask's bits must be present
    in `bitmap` now and NOT all present in `prev_bitmap`. This is
    per-whole-mask, not per-bit, so a pad whose bits bleed in one at a time
    across reads (e.g. DOWN's 0x01 -> 0x09 -> 0x0d) fires exactly once, at
    the read where the mask first becomes fully satisfied. The first mask
    in list order that satisfies this wins and short-circuits the rest, so
    at most one action is returned per call, and pad bleed that happens to
    satisfy two masks in the same single read can't produce two actions.
    """
    bitmap &= ~COMMON_TOUCH_BIT
    prev_bitmap &= ~COMMON_TOUCH_BIT
    if not keys:
        return ["OK"] if (prev_bitmap == 0 and bitmap != 0) else []
    for mask, action in keys:
        if (bitmap & mask) == mask and (prev_bitmap & mask) != mask:
            return [action]
    return []

class Sx8635Buttons:
    """RN316 front-board Semtech SX8635 capacitive touch-wheel controller,
    i801 SMBus address 0x2b. STRICTLY READ-ONLY: the chip keeps its
    calibration in volatile SPM (reloaded from NVM/QSM at power-up); on the
    one RN316 tested SpmStat NvmValid=0 (factory QSM defaults) yet buttons
    AND the wheel both work read-only, so this class contains NO write
    helper at all -- there is no SPM load and no NVM write path to
    accidentally trigger. NETGEAR's own NVM burn sequence is 0xAC/0xAD
    (unlock key pair) then 0xA5/0x5A pulsed into 0x0E, and its soft reset
    is 0xB1; this driver writes none of that, ever.

    Never run this alongside tools/sx8635-watch.py: both read IrqSrc
    (reg 0x00), which clears on read, so two readers steal each other's
    events.

    Register facts (see .claude/advice/sx8635-re-report.md and the tester's
    real-hardware run): reading reg 0x00 (IrqSrc) clears NIRQ; bit 2 =
    buttons (read reg 0x02, a touch bitmap); bit 3 = wheel (position =
    (reg0x03<<8)|reg0x04, reg 0x01 bit 0x10 = wheel currently touched).
    The reg 0x02 bit->pad mapping is now confirmed by a real-hardware
    mapping run: bit 0 = common "something touched" bit (never a button by
    itself), bit 1 = OK, bits 2|3 = DOWN, bits 4|5 = RIGHT. See
    docs/porting.md for the full table and MODELS["rn316"]["sx8635"]["keys"]
    for the mapping this class actually uses.
    """
    REG_IRQSRC, REG_CAPSTAT_MSB, REG_CAPSTAT_LSB = 0x00, 0x01, 0x02
    REG_POS_MSB, REG_POS_LSB = 0x03, 0x04
    WHEEL_TOUCHED = 0x10   # reg 0x01 bit 4
    IRQ_BUTTONS, IRQ_WHEEL = 0x04, 0x08   # reg 0x00 bits 2, 3
    SAFETY_INTERVAL = 1.0                 # seconds; read even with no NIRQ/no touch
    WHEEL_POLL_HZ_INTERVAL = 1.0 / 50     # poll >= 50 Hz while the wheel is touched

    def __init__(self, gpio, spec):
        self.gpio = gpio
        self.addr = spec["addr"]
        self.wheel_range = spec["wheel_range"]
        self.detent = spec["detent"]
        self.keys = spec["keys"]
        self.fd = os.open("/dev/i2c-%d" % find_i801_bus(), os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, self.addr)
        self.prev_bitmap = 0
        self.wheel_touched = False
        self.wheel_prev_pos = None
        self.wheel_acc = 0
        self.wheel_stale_since = None
        self.last_read = 0.0
        # First read: confirm the chip actually answers (raises OSError on
        # NAK/missing bus, which _build_buttons treats as "degrade, don't
        # exit" -- see there).
        self._read(self.REG_IRQSRC)

    def _read(self, reg):
        buf = (ctypes.c_ubyte * 34)()
        a = _smbus_ioctl(1, reg, 2, ctypes.cast(buf, ctypes.c_void_p))
        assert a.rw == 1   # read-byte-data direction only; this class never writes
        fcntl.ioctl(self.fd, I2C_SMBUS, a)
        return buf[0]

    def events(self, now):
        """Read when gpio.int_active(), or when the wheel was touched and
        we haven't polled it in WHEEL_POLL_HZ_INTERVAL (keeps polling at
        >= 50 Hz while touched, since NIRQ may not re-assert for continued
        motion), or every SAFETY_INTERVAL regardless (robustness if the
        int-line gating assumption is ever wrong -- buttons still work,
        just laggier).

        IMPORTANT: IrqSrc reading 0 does NOT mean "nothing happening" --
        the real hardware trace shows the chip returns IrqSrc == 0 between
        wheel events while a finger is still on the ring (our own >=50 Hz
        sub-polling outruns the chip's scan period). So an irq==0 read must
        NOT reset the wheel latch/prev-position/accumulator: as long as
        wheel_touched is set, the wheel branch below still runs regardless
        of what IrqSrc said, and reads the same unchanged position (a
        harmless zero-delta) until either a real move or the release
        shows up via reg 0x01 bit 0x10 clearing in _read_wheel. An earlier
        version reset on every irq==0 read, which made every position look
        like the first ever seen and the wheel never emitted anything on
        real hardware -- see .claude/advice/buttons-integration-review.md.
        """
        due = self.gpio.int_active() \
            or (self.wheel_touched and now - self.last_read >= self.WHEEL_POLL_HZ_INTERVAL) \
            or (now - self.last_read >= self.SAFETY_INTERVAL)
        if not due:
            return []
        self.last_read = now
        try:
            irq = self._read(self.REG_IRQSRC)
            actions = []
            if (irq & self.IRQ_WHEEL) or self.wheel_touched:
                actions += self._read_wheel()
            if irq & self.IRQ_BUTTONS:
                actions += self._read_buttons()
            if irq:
                self.wheel_stale_since = None
            elif self.wheel_touched:
                # Belt, not the primary release path (that's _read_wheel
                # seeing reg 0x01 bit 0x10 clear): if IrqSrc reads 0 on
                # every poll for a full 5s while still "latched touched",
                # something is wrong (a missed release, a wedged chip) --
                # drop the latch rather than sub-poll forever. Deliberately
                # NOT triggered by a single zero read (see docstring above).
                if self.wheel_stale_since is None:
                    self.wheel_stale_since = now
                elif now - self.wheel_stale_since >= 5.0:
                    self.wheel_touched = False
                    self.wheel_prev_pos = None
                    self.wheel_acc = 0
                    self.wheel_stale_since = None
            return actions
        except OSError:
            # Covers the IrqSrc read above and any follow-up read inside
            # _read_wheel/_read_buttons -- a NAK partway through a read
            # sequence (e.g. i2c-i801 EBUSY under ACPI contention) must not
            # kill the daemon, just skip this tick.
            return []

    def _read_buttons(self):
        bitmap = self._read(self.REG_CAPSTAT_LSB)
        rising = _rising_actions(bitmap, self.prev_bitmap, self.keys)
        self.prev_bitmap = bitmap
        return rising

    def _read_wheel(self):
        cap_msb = self._read(self.REG_CAPSTAT_MSB)
        touched = bool(cap_msb & self.WHEEL_TOUCHED)
        actions = []
        if touched:
            msb = self._read(self.REG_POS_MSB)
            lsb = self._read(self.REG_POS_LSB)
            pos = (msb << 8) | lsb
            if pos >= self.wheel_range:
                pass   # glitch position (observed 0x3b at a wrap) -- ignore, don't update prev
            elif not self.wheel_touched or self.wheel_prev_pos is None:
                self.wheel_prev_pos = pos   # first valid position after a touch starts: just record it
            else:
                delta = _wheel_delta(pos, self.wheel_prev_pos, self.wheel_range)
                self.wheel_prev_pos = pos
                self.wheel_acc += delta
                events, self.wheel_acc = _wheel_emit(self.wheel_acc, self.detent)
                actions += events
            self.wheel_touched = True
        else:
            self.wheel_touched = False
            self.wheel_prev_pos = None
            self.wheel_acc = 0
        return actions
