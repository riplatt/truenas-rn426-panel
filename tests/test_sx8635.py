import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import rnpanel.sx8635 as sx8635
from rnpanel.sx8635 import Sx8635Buttons, _wheel_delta, _wheel_emit, _rising_actions
from fakes import SX8635_SPEC, _sx8635


class TestWheelDelta(unittest.TestCase):
    """Wrap-aware delta, R=0x1f (rn316's observed wheel_range)."""
    R = 0x1f

    def test_wrap_forward(self):
        self.assertEqual(_wheel_delta(0x00, 0x1e, self.R), 1)

    def test_wrap_backward(self):
        self.assertEqual(_wheel_delta(0x1e, 0x00, self.R), -1)

    def test_small_step_backward(self):
        self.assertEqual(_wheel_delta(0x00, 0x02, self.R), -2)


class TestWheelEmit(unittest.TestCase):
    def test_clockwise_run_emits_next_only(self):
        # A clockwise run (decreasing raw position) must emit only NEXT,
        # never PREV, at one event per detent.
        positions = [0x1e, 0x1b, 0x16, 0x14, 0x11, 0x0c]
        R, detent = 0x1f, 4
        acc = 0
        prev = positions[0]
        events = []
        for pos in positions[1:]:
            acc += _wheel_delta(pos, prev, R)
            prev = pos
            new_events, acc = _wheel_emit(acc, detent)
            events += new_events
        self.assertIn("NEXT", events)
        self.assertNotIn("PREV", events)
        self.assertEqual(events, ["NEXT"] * 4)


class TestRisingActions(unittest.TestCase):
    """`keys` is now an ordered list of (mask, action) pairs, and the
    rising-edge check is per-whole-mask (not per-bit), first match in list
    order wins, and bit 0 (COMMON_TOUCH_BIT) is masked out of both bitmap
    and prev_bitmap before any check."""

    def test_empty_keys_masked_zero_to_nonzero_emits_one_ok(self):
        # 0x07 masks down to 0x06 (nonzero) -- a real touch, not just bit 0.
        self.assertEqual(_rising_actions(0x07, 0x00, []), ["OK"])

    def test_empty_keys_only_common_touch_bit_emits_nothing(self):
        # bitmap is only bit 0 (a lone ring touch) -- masks down to 0, must
        # never satisfy the "nonzero" fallback.
        self.assertEqual(_rising_actions(0x01, 0x00, []), [])

    def test_no_rising_bits_emits_nothing(self):
        self.assertEqual(_rising_actions(0x03, 0x03, []), [])

    def test_mapped_keys_bleed_across_calls_fires_once_at_completion(self):
        # DOWN's mask (0x0c) bleeds in one bit at a time across reads, like
        # the real trace (0x01 -> 0x09 -> 0x0d): must not fire until the
        # whole mask is satisfied, and then exactly once.
        keys = [(0x0c, "NEXT")]
        self.assertEqual(_rising_actions(0x09, 0x01, keys), [])   # only bit 3 so far
        self.assertEqual(_rising_actions(0x0d, 0x09, keys), ["NEXT"])   # mask now complete
        self.assertEqual(_rising_actions(0x0d, 0x0d, keys), [])   # already was complete

    def test_mapped_keys_first_match_in_list_order_wins(self):
        # Both masks newly satisfied in the same single call -- only the
        # first one in list order fires.
        keys = [(0x02, "OK"), (0x0c, "NEXT")]
        self.assertEqual(_rising_actions(0x0e, 0x00, keys), ["OK"])
        keys_reordered = [(0x0c, "NEXT"), (0x02, "OK")]
        self.assertEqual(_rising_actions(0x0e, 0x00, keys_reordered), ["NEXT"])

    def test_mapped_keys_only_common_touch_bit_emits_nothing(self):
        # A bitmap of only bit 0 can never match a real (bit-0-free) mask.
        keys = [(0x02, "OK"), (0x0c, "NEXT"), (0x30, "RIGHT")]
        self.assertEqual(_rising_actions(0x01, 0x00, keys), [])


class TestSx8635Wheel(unittest.TestCase):
    def test_glitch_position_ignored(self):
        btn = _sx8635()
        # irq(bit3=wheel), capstat_msb(touched), pos_msb, pos_lsb -> valid
        # first position, just recorded (no delta on the first touch read)
        btn.queue = [0x08, 0x10, 0x00, 0x10]
        self.assertEqual(btn.events(0.0), [])
        self.assertEqual(btn.wheel_prev_pos, 0x10)

        # next read: position 0x3b >= wheel_range (0x1f) -- the observed
        # wrap-glitch. Must be ignored: no event, prev position unchanged.
        btn.queue = [0x08, 0x10, 0x00, 0x3b]
        self.assertEqual(btn.events(0.0), [])
        self.assertEqual(btn.wheel_prev_pos, 0x10)

    def test_wheel_untouched_resets_accumulator(self):
        btn = _sx8635()
        btn.queue = [0x08, 0x10, 0x00, 0x10]
        btn.events(0.0)
        self.assertTrue(btn.wheel_touched)

        # capstat_msb with the touched bit clear -- wheel released
        btn.queue = [0x08, 0x00]
        self.assertEqual(btn.events(0.0), [])
        self.assertFalse(btn.wheel_touched)
        self.assertIsNone(btn.wheel_prev_pos)


class TestSx8635Buttons(unittest.TestCase):
    def test_rising_bit_ok_when_wheel_not_touched(self):
        # bit 0 alone (0x01) is a ring touch, never a button, so the
        # bitmap here must carry a real non-bit-0 bit (0x03 = bit 0 + bit
        # 1) to exercise the empty-keys "any real touch = one OK" fallback.
        btn = _sx8635()
        btn.queue = [0x04, 0x03]   # irq(bit2=buttons), capstat_lsb
        self.assertEqual(btn.events(0.0), ["OK"])

    def test_ok_suppressed_while_wheel_latched(self):
        btn = _sx8635()
        # latch the wheel touched first
        btn.queue = [0x08, 0x10, 0x00, 0x05]
        btn.events(0.0)
        self.assertTrue(btn.wheel_touched)

        # capstat_lsb here is 0x01 -- bit 0 only. Bit 0 (COMMON_TOUCH_BIT) is
        # masked out before the empty-keys fallback ever looks at the
        # bitmap, so this reads as "masked bitmap == 0" and never fires,
        # regardless of the wheel latch. (Wheel latching is exercised above
        # only to show it has no bearing on this outcome any more -- the
        # old wheel-latch-based suppression this test used to describe has
        # been deleted; masking bit 0 makes it unnecessary.)
        btn.queue = [0x0C, 0x10, 0x00, 0x06, 0x01]
        self.assertEqual(btn.events(0.0), [])

    def test_mapped_key_not_suppressed_by_wheel_latch(self):
        # a real, mapped pad (OK, mask 0x02) should still fire even while
        # the wheel is latched touched -- the trace data shows mapped pads
        # report reg 0x01 == 0x00 (no ring touch) during their own presses,
        # so they're physically independent of the ring and must never be
        # suppressed by a wheel latch.
        btn = _sx8635(keys=[(0x02, "OK")])
        btn.queue = [0x08, 0x10, 0x00, 0x05]
        btn.events(0.0)
        self.assertTrue(btn.wheel_touched)

        # wheel position unchanged (0x05) so the wheel branch contributes
        # no NEXT/PREV of its own; capstat_lsb=0x03 (bit 0 + bit 1) rises
        # OK's mask (0x02) from 0.
        btn.queue = [0x0C, 0x10, 0x00, 0x05, 0x03]
        self.assertEqual(btn.events(0.0), ["OK"])

    def test_only_common_touch_bit_yields_no_action_with_real_keys_wheel_not_touched(self):
        # A lone ring touch (bitmap == 0x01) must never fire a mapped
        # action, wheel latch or not.
        btn = _sx8635(keys=SX8635_SPEC["keys"])
        btn.queue = [0x04, 0x01]
        self.assertEqual(btn.events(0.0), [])

    def test_only_common_touch_bit_yields_no_action_with_real_keys_wheel_touched(self):
        btn = _sx8635(keys=SX8635_SPEC["keys"])
        btn.queue = [0x08, 0x10, 0x00, 0x05]   # latch the wheel touched
        btn.events(0.0)
        self.assertTrue(btn.wheel_touched)

        btn.queue = [0x0C, 0x10, 0x00, 0x05, 0x01]
        self.assertEqual(btn.events(0.0), [])

    def test_ok_press_real_trace(self):
        # Real-hardware trace for one OK press: 0x01 -> 0x03 -> 0x23 -> 0x00.
        btn = _sx8635(keys=SX8635_SPEC["keys"])
        results = []
        for bitmap in (0x01, 0x03, 0x23, 0x00):
            btn.queue = [0x04, bitmap]   # irq=buttons only
            results += btn.events(0.0)
        self.assertEqual(results, ["OK"])

    def test_down_press_real_trace(self):
        # Real-hardware trace for one DOWN press: 0x01 -> 0x09 -> 0x0d -> 0x00.
        # Activity-only: it wakes the display but never turns a page.
        btn = _sx8635(keys=SX8635_SPEC["keys"])
        results = []
        for bitmap in (0x01, 0x09, 0x0d, 0x00):
            btn.queue = [0x04, bitmap]
            results += btn.events(0.0)
        self.assertEqual(results, ["DOWN"])

    def test_slide_through_bottom_never_turns_a_page_from_buttons(self):
        # A slide across the bottom of the ring crosses CAP2-5, which the
        # factory layout reports as button bits 2-5. The per-read order below
        # is RECONSTRUCTED from the electrode geometry (the watcher traces
        # don't record reg 0x02 per read): CCW enters CAP2 first, CW enters
        # CAP5 first. Neither direction may emit NEXT or PREV from the
        # button path; only the wheel turns pages.
        ccw = [0x01, 0x05, 0x0d, 0x09, 0x19, 0x11, 0x31, 0x21, 0x01, 0x00]
        for name, seq in (("ccw", ccw), ("cw", list(reversed(ccw)))):
            btn = _sx8635(keys=SX8635_SPEC["keys"])
            results = []
            for bitmap in seq:
                btn.queue = [0x04, bitmap]
                results += btn.events(0.0)
            self.assertNotIn("NEXT", results, name)
            self.assertNotIn("PREV", results, name)

    def test_right_press_real_trace(self):
        # Real-hardware trace for one RIGHT press: 0x01 -> 0x21 -> 0x31 -> 0x00.
        btn = _sx8635(keys=SX8635_SPEC["keys"])
        results = []
        for bitmap in (0x01, 0x21, 0x31, 0x00):
            btn.queue = [0x04, bitmap]
            results += btn.events(0.0)
        self.assertEqual(results, ["RIGHT"])

    def test_irqsrc_zero_returns_no_events(self):
        btn = _sx8635()
        btn.queue = [0x00]
        self.assertEqual(btn.events(0.0), [])

    def test_safety_read_fires_after_one_second_even_if_gpio_idle(self):
        btn = _sx8635(gpio_active=False)
        btn.queue = [0x00]
        self.assertEqual(btn.events(1.5), [])   # 1.5s since last_read=0.0

    def test_no_read_when_not_due(self):
        btn = _sx8635(gpio_active=False)
        btn.last_read = 1.0
        self.assertEqual(btn.events(1.2), [])   # not due yet, queue untouched
        self.assertEqual(btn.queue, [])

    def test_zero_irqsrc_while_latched_keeps_latch_and_accumulator(self):
        # The must-fix: an irq==0 read must NOT reset the wheel latch, the
        # previous position, or the accumulator -- the real trace shows the
        # chip returns IrqSrc==0 between wheel events while still touched.
        btn = _sx8635(gpio_active=True)
        btn.queue = [0x08, 0x10, 0x00, 0x10]   # touch begins, first pos recorded
        btn.events(0.0)
        self.assertTrue(btn.wheel_touched)
        self.assertEqual(btn.wheel_prev_pos, 0x10)

        btn.queue = [0x08, 0x10, 0x00, 0x0e]   # a real move, partial detent
        btn.events(0.01)
        acc_before = btn.wheel_acc
        prev_before = btn.wheel_prev_pos
        self.assertNotEqual(acc_before, 0)

        # IrqSrc reads 0 -- wheel_touched is already set so the wheel branch
        # still runs (per the fixed events() shape) and re-reads the same,
        # unchanged position: zero delta, nothing reset.
        btn.queue = [0x00, 0x10, 0x00, 0x0e]
        events = btn.events(0.02)
        self.assertEqual(events, [])
        self.assertTrue(btn.wheel_touched)
        self.assertEqual(btn.wheel_prev_pos, prev_before)
        self.assertEqual(btn.wheel_acc, acc_before)

    def test_subpoll_throttle_no_read_before_interval(self):
        btn = _sx8635(gpio_active=True)
        btn.queue = [0x08, 0x10, 0x00, 0x10]
        btn.events(0.0)
        self.assertTrue(btn.wheel_touched)

        btn.gpio.active = False     # NIRQ not asserting any more
        btn.last_read = 0.0
        # WHEEL_POLL_HZ_INTERVAL is 1/50 = 0.02s; 0.01s hasn't elapsed yet,
        # so no read should happen at all (an empty queue would raise
        # IndexError on any attempted read, failing this test loudly).
        btn.queue = []
        self.assertEqual(btn.events(0.01), [])

    def test_idle_read_rate_one_read_per_second(self):
        btn = _sx8635(gpio_active=False)
        reads = []
        def counting_read(reg):
            reads.append(reg)
            return 0x00
        btn._read = counting_read
        # 200 calls spanning exactly 1s, gpio idle throughout, IrqSrc == 0:
        # only the 1s SAFETY_INTERVAL should trigger a read, and only once.
        for i in range(200):
            btn.events(i / 199.0)
        self.assertEqual(len(reads), 1)

    def test_one_ok_per_press_with_empty_keys(self):
        btn = _sx8635(gpio_active=True)
        results = []
        for bitmap in (0x01, 0x03, 0x23, 0x00):
            btn.queue = [0x04, bitmap]   # irq=buttons only
            results += btn.events(0.0)
        self.assertEqual(results, ["OK"])

    def test_oserror_on_followup_read_returns_empty_not_raises(self):
        btn = _sx8635(gpio_active=True)
        calls = [0]
        def flaky_read(reg):
            calls[0] += 1
            if calls[0] == 1:
                return 0x04   # irq: buttons bit set -> triggers a follow-up read
            raise OSError("i2c NAK")
        btn._read = flaky_read
        self.assertEqual(btn.events(0.0), [])


class TestSx8635TraceReplay(unittest.TestCase):
    """Replay the tester's real-hardware trace
    (scratchpad/sx-watch.txt, quoted in .claude/advice/buttons-integration-
    review.md) through the actual events() state machine, including the
    daemon's own zero-IrqSrc sub-poll reads that land between real chip
    events while the wheel is latched touched. This is the regression the
    must-fix guards against: with the old irq==0 latch-reset, this produces
    zero NEXT/PREV total; after the fix it must reproduce the tester's
    per-turn tick counts (hand-derived from the trace's deduplicated wheel
    positions, R=0x1f, detent=4)."""

    def _replay(self, positions):
        btn = _sx8635(gpio_active=True)
        next_count = prev_count = 0
        t = 0.0
        last_pos = positions[0]
        for pos in positions:
            t += 0.005
            btn.queue = [0x08, 0x10, 0x00, pos]   # a "real" chip wheel event
            ev = btn.events(t)
            next_count += ev.count("NEXT"); prev_count += ev.count("PREV")
            if pos < btn.wheel_range:
                last_pos = pos   # glitches (pos >= wheel_range) don't update prev
            t += 0.005
            # the daemon's own sub-poll landing in the gap before the next
            # real event: IrqSrc reads 0, position hasn't moved yet -- must
            # be a no-op, not a latch reset.
            btn.queue = [0x00, 0x10, 0x00, last_pos]
            ev = btn.events(t)
            next_count += ev.count("NEXT"); prev_count += ev.count("PREV")
        t += 0.005
        btn.queue = [0x08, 0x00]   # release: wheel-touched bit clears
        btn.events(t)
        self.assertFalse(btn.wheel_touched)
        return next_count, prev_count

    def test_cw_turn_emits_seven_next_no_prev(self):
        # Deduplicated CW-phase positions from the trace, including the
        # observed 0x3b glitch at the wrap and the final rest position.
        # Hand-derived ticks: -31 over the run = 7 detents of 4, remainder
        # -3 -- 7 NEXT, 0 PREV, the glitch contributing nothing.
        positions = [0x00, 0x1e, 0x1b, 0x16, 0x14, 0x11, 0x0c, 0x09, 0x06, 0x02, 0x00, 0x3b, 0x00]
        next_count, prev_count = self._replay(positions)
        self.assertEqual(next_count, 7)
        self.assertEqual(prev_count, 0)

    def test_ccw_turn_emits_twelve_prev_no_next(self):
        # Deduplicated CCW-phase positions from the trace (consecutive
        # repeats collapsed; the leading 0x0a pre-touch position is kept --
        # it's the first-position "just record it" baseline, contributing
        # no delta, and the hand-derived total below matches with it in).
        # Hand-derived ticks (wrap-aware, R=0x1f):
        #   0x0a->10 +6, 10->13 +3, 13->14 +1, 14->16 +2, 16->1a +4,
        #   1a->1d +3, 1d->1e +1, 1e->00 +1, 00->02 +2, 02->06 +4,
        #   06->0a +4, 0a->10 +6, 10->13 +3, 13->14 +1, 14->17 +3,
        #   17->19 +2, 19->1c +3, 1c->1e +2
        #   sum = 51 = 12 detents of 4, remainder +3 -- 12 PREV, 0 NEXT.
        positions = [0x0a, 0x10, 0x13, 0x14, 0x16, 0x1a, 0x1d, 0x1e,
                     0x00, 0x02, 0x06, 0x0a, 0x10, 0x13, 0x14, 0x17,
                     0x19, 0x1c, 0x1e]
        next_count, prev_count = self._replay(positions)
        self.assertEqual(next_count, 0)
        self.assertEqual(prev_count, 12)


class TestSx8635NoWritePath(unittest.TestCase):
    def test_source_has_no_write_call(self):
        import inspect
        src = inspect.getsource(Sx8635Buttons)
        self.assertNotIn("pwrite", src)
        self.assertNotIn("_smbus_ioctl(0", src)
        self.assertEqual(src.count("fcntl.ioctl(self.fd, I2C_SMBUS"), 1)

    def test_module_source_has_no_write_call(self):
        # Module-level scan (not just the class body): the whole
        # rnpanel/sx8635.py file must never contain a positional-write
        # syscall or a write-direction SMBus transfer, and must contain
        # exactly one SMBus-transfer ioctl call (the read in _read()).
        # (__init__'s own fcntl.ioctl(self.fd, I2C_SLAVE, ...) binds the
        # i2c address to the fd -- a normal, non-register-access ioctl, not
        # a chip read or write -- so it's deliberately not counted here;
        # this checks there is exactly one place doing an I2C_SMBUS
        # register transfer, matching the narrower class-level check above.)
        with open(sx8635.__file__) as f:
            src = f.read()
        self.assertNotIn("pwrite", src)
        self.assertNotIn("_smbus_ioctl(0", src)
        self.assertEqual(src.count("fcntl.ioctl(self.fd, I2C_SMBUS"), 1)


if __name__ == "__main__":
    unittest.main()
