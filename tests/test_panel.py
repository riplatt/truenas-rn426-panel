import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rn426_panel as m

# Pin maps mirroring MODELS["rnx26"]["pins"] / MODELS["rn316"]["pins"], kept
# as local literals (not imported from MODELS) so these tests independently
# pin down the numbers the brief specifies -- if MODELS ever drifts, the
# equivalence/derivation tests below should catch it.
RNX26_PINS = {"MOSI": 54, "CLK": 1, "DC": 32, "CS": 50, "EN": 6, "RST": 7, "BTN_INT": 2}
RN316_PINS = {"MOSI": 21, "CLK": 19, "DC": 16, "CS": 7, "EN": 32, "RST": 24, "BTN_INT": 2}
# Synthetic pin map for the "no known interrupt line" case -- RN316 no
# longer represents that (its BTN_INT is confirmed to be 2, see MODELS).
NO_INT_LINE_PINS = dict(RNX26_PINS, BTN_INT=None)


class TestIchLineAddr(unittest.TestCase):
    """line -> (port, bit) math for all 7 gpio_ich pins used by IchPortGpio,
    with gpiobase=0 so the expected port equals the bank-byte offset."""

    CASES = {
        "MOSI": (54, 0x3A, 6),
        "CLK":  (1,  0x0C, 1),
        "DC":   (32, 0x38, 0),
        "CS":   (50, 0x3A, 2),
        "EN":   (6,  0x0C, 6),
        "RST":  (7,  0x0C, 7),
        "BTN_INT": (2, 0x0C, 2),
    }

    def test_addresses(self):
        for name, (line, expect_port, expect_bit) in self.CASES.items():
            port, bit = m._ich_line_addr(0, line)
            self.assertEqual(port, expect_port, "%s port" % name)
            self.assertEqual(bit, expect_bit, "%s bit" % name)

    def test_gpiobase_offset(self):
        # a nonzero gpiobase should shift the port by exactly that amount
        port, bit = m._ich_line_addr(0x1000, RNX26_PINS["CS"])
        self.assertEqual(port, 0x1000 + 0x3A)
        self.assertEqual(bit, 2)

    def test_rnx26_pins_match_cases(self):
        for name, (line, _, _) in self.CASES.items():
            self.assertEqual(RNX26_PINS[name], line)


class TestGpiobaseParse(unittest.TestCase):
    def _cfg(self, dword):
        import struct
        blob = bytearray(0x4C)
        struct.pack_into("<I", blob, 0x48, dword)
        return bytes(blob)

    def test_masks_to_io_port_bits(self):
        # low 7 bits and high bits above the io-port window must be dropped
        base = m._parse_gpiobase(self._cfg(0x0000F5C1))
        self.assertEqual(base, 0x0000F5C1 & 0x0000FF80)
        self.assertEqual(base, 0xF580)

    def test_zero_raises(self):
        with self.assertRaises(RuntimeError):
            m._parse_gpiobase(self._cfg(0))

    def test_short_blob_raises(self):
        with self.assertRaises(RuntimeError):
            m._parse_gpiobase(b"\x00" * 4)


class FakeIchPortGpio(m.IchPortGpio):
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


class TestIchEnResetInvariant(unittest.TestCase):
    def setUp(self):
        self.gp = FakeIchPortGpio(RNX26_PINS)

    def _bank0_byte0(self):
        return self.gp.port.get(0x0C, 0)

    def test_en_low_request_is_forced_high(self):
        self.gp.set("EN", 0)
        self.assertEqual(self._bank0_byte0() & (1 << 6), 1 << 6)

    def test_rst_low_request_is_forced_high(self):
        self.gp.set("RST", 0)
        self.assertEqual(self._bank0_byte0() & (1 << 7), 1 << 7)

    def test_unrelated_write_to_same_byte_still_forces_both(self):
        # CLK lives in the same byte (bank0 byte0) as EN/RESET; any write to
        # that byte must leave both bits high regardless of what was asked.
        self.gp.set("CLK", 0)
        self.gp.set("CLK", 1)
        v = self._bank0_byte0()
        self.assertEqual(v & (1 << 6), 1 << 6)
        self.assertEqual(v & (1 << 7), 1 << 7)

    def test_other_bytes_unaffected(self):
        # DC (bank1 byte0) must not touch bank0 byte0 at all.
        self.gp.set("DC", 1)
        self.assertNotIn(0x0C, self.gp.port)

    def test_repeated_toggling_never_drops_en_or_reset(self):
        for _ in range(20):
            self.gp.set("EN", 0)
            self.gp.set("CLK", 1)
            self.gp.set("CLK", 0)
        v = self._bank0_byte0()
        self.assertEqual(v & 0xC0, 0xC0)


class TestIchChokePoint(unittest.TestCase):
    def test_rejects_non_whitelisted_port(self):
        gp = FakeIchPortGpio(RNX26_PINS)
        with self.assertRaises(ValueError):
            gp._wr_byte(0xFFFF, 0)

    def test_accepts_whitelisted_ports(self):
        gp = FakeIchPortGpio(RNX26_PINS)
        for port in (0x0C, 0x38, 0x3A):
            gp._wr_byte(port, 0x00)   # must not raise


class TestIchDerivationRnx26(unittest.TestCase):
    """The derived whitelist/force must reproduce exactly the old hardcoded
    "three whitelisted bytes, bank0 byte0 forces bits 6|7" behavior."""

    def test_whitelist_matches_old_hardcoded_set(self):
        gp = FakeIchPortGpio(RNX26_PINS, gpiobase=0x500)
        base = 0x500
        self.assertEqual(gp._whitelist, {base + 0x0C, base + 0x38, base + 0x3A})

    def test_force_matches_old_hardcoded_bits(self):
        gp = FakeIchPortGpio(RNX26_PINS, gpiobase=0x500)
        base = 0x500
        self.assertEqual(gp._force, {base + 0x0C: 0xC0})


class TestIchDerivationRn316(unittest.TestCase):
    """Hand-derived from _ich_line_addr with gpiobase=0x500:
      MOSI 21 -> bank0 bit21 -> byte 21//8=2 -> port 0x500+0x0C+2=0x50E, bit 5
      CLK  19 -> bank0 bit19 -> byte 19//8=2 -> port 0x50E, bit 3
      DC   16 -> bank0 bit16 -> byte 16//8=2 -> port 0x50E, bit 0
      CS    7 -> bank0 bit7  -> byte 7//8=0  -> port 0x500+0x0C=0x50C, bit 7
      EN   32 -> bank1 bit0  -> byte 0//8=0  -> port 0x500+0x38=0x538, bit 0
      RST  24 -> bank0 bit24 -> byte 24//8=3 -> port 0x500+0x0C+3=0x50F, bit 0
    So the whitelist is {0x50C, 0x50E, 0x50F, 0x538} and the force mask is
    bit 0 on both 0x50F (RST) and 0x538 (EN)."""

    def setUp(self):
        self.gp = FakeIchPortGpio(RN316_PINS, gpiobase=0x500, ngpio=61)

    def test_whitelist(self):
        self.assertEqual(self.gp._whitelist, {0x50C, 0x50E, 0x50F, 0x538})

    def test_force(self):
        self.assertEqual(self.gp._force, {0x50F: 0x01, 0x538: 0x01})

    def test_set_rst_low_leaves_forced_bit_high(self):
        self.gp.set("RST", 0)
        self.assertEqual(self.gp.port[0x50F] & 0x01, 0x01)

    def test_set_en_low_leaves_forced_bit_high(self):
        self.gp.set("EN", 0)
        self.assertEqual(self.gp.port[0x538] & 0x01, 0x01)


class TestBtnIntNone(unittest.TestCase):
    def test_int_active_false_when_no_interrupt_line(self):
        gp = FakeIchPortGpio(NO_INT_LINE_PINS, gpiobase=0x500)
        self.assertFalse(gp.int_active())

    def test_has_int_line_false_when_no_interrupt_line(self):
        gp = FakeIchPortGpio(NO_INT_LINE_PINS, gpiobase=0x500)
        self.assertFalse(gp.has_int_line)

    def test_has_int_line_true_when_interrupt_line_known(self):
        gp = FakeIchPortGpio(RNX26_PINS, gpiobase=0x500)
        self.assertTrue(gp.has_int_line)

    def test_rn316_has_int_line_true(self):
        # BTN_INT=2 is confirmed on real rn316 hardware -- see MODELS.
        gp = FakeIchPortGpio(RN316_PINS, gpiobase=0x500, ngpio=61)
        self.assertTrue(gp.has_int_line)


class TestLpcIdCheck(unittest.TestCase):
    """Pure function, no device files -- same pattern as TestGpiobaseParse."""

    def test_match_does_not_raise(self):
        m._check_lpc_id(0x8c54, 0x8c54)
        m._check_lpc_id(0x3a18, 0x3a18)

    def test_mismatch_raises(self):
        with self.assertRaises(RuntimeError):
            m._check_lpc_id(0x3a18, 0x8c54)


class TestGpioEnCheck(unittest.TestCase):
    def test_bit_set_does_not_raise(self):
        m._check_gpio_en(0x10)   # bit 4 only
        m._check_gpio_en(0xFF)   # bit 4 among others

    def test_bit_clear_raises(self):
        with self.assertRaises(RuntimeError):
            m._check_gpio_en(0x00)

    def test_bit_clear_among_others_still_raises(self):
        with self.assertRaises(RuntimeError):
            m._check_gpio_en(0xEF)   # every bit but bit 4


class TestNgpioCheck(unittest.TestCase):
    def test_rnx26_lines_all_under_76(self):
        m._check_ngpio(RNX26_PINS, 76)   # must not raise

    def test_rn316_lines_all_under_61(self):
        m._check_ngpio(RN316_PINS, 61)   # must not raise

    def test_line_equal_to_ngpio_raises(self):
        # a line number equal to ngpio is out of range (lines are 0..ngpio-1)
        pins = dict(RNX26_PINS)
        pins["CS"] = 76
        with self.assertRaises(ValueError):
            m._check_ngpio(pins, 76)

    def test_btn_int_over_ngpio_raises(self):
        pins = dict(RNX26_PINS)
        pins["BTN_INT"] = 76
        with self.assertRaises(ValueError):
            m._check_ngpio(pins, 76)

    def test_construction_raises_when_a_line_is_out_of_range(self):
        # _derive_maps (called from __init__) must enforce this too, not
        # just the free function in isolation.
        pins = dict(RNX26_PINS)
        pins["CS"] = 100
        with self.assertRaises(ValueError):
            FakeIchPortGpio(pins, ngpio=76)


class TestRotateSeconds(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("RN_ROTATE")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("RN_ROTATE", None)
        else:
            os.environ["RN_ROTATE"] = self._old

    def test_default_is_10(self):
        os.environ.pop("RN_ROTATE", None)
        self.assertEqual(m._rotate_seconds(), 10)

    def test_env_override(self):
        os.environ["RN_ROTATE"] = "3"
        self.assertEqual(m._rotate_seconds(), 3)

    def test_zero_disables(self):
        os.environ["RN_ROTATE"] = "0"
        self.assertEqual(m._rotate_seconds(), 0)


class TestDetectModel(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.get("RN_MODEL")

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("RN_MODEL", None)
        else:
            os.environ["RN_MODEL"] = self._old_env

    def test_env_override_rn426(self):
        os.environ["RN_MODEL"] = "rn426"
        self.assertEqual(m.detect_model(), "rn426")

    def test_env_override_rnx26(self):
        os.environ["RN_MODEL"] = "rnx26"
        self.assertEqual(m.detect_model(), "rnx26")

    def test_env_override_rn316(self):
        os.environ["RN_MODEL"] = "rn316"
        self.assertEqual(m.detect_model(), "rn316")

    def test_env_override_unknown_exits(self):
        os.environ["RN_MODEL"] = "bogus"
        with self.assertRaises(SystemExit):
            m.detect_model()

    def test_cpuinfo_denverton_selects_rn426(self):
        os.environ.pop("RN_MODEL", None)
        cpuinfo = lambda: "model name\t: Intel(R) Atom(TM) CPU C3538 @ 2.10GHz\n"
        self.assertEqual(m.detect_model(cpuinfo_reader=cpuinfo), "rn426")

    def test_dmi_528x_selects_rnx26(self):
        os.environ.pop("RN_MODEL", None)
        cpuinfo = lambda: (_ for _ in ()).throw(OSError())
        dmi = lambda: "ReadyNAS 528X\n"
        self.assertEqual(m.detect_model(cpuinfo_reader=cpuinfo, dmi_reader=dmi), "rnx26")

    def test_dmi_628x_selects_rnx26(self):
        os.environ.pop("RN_MODEL", None)
        cpuinfo = lambda: (_ for _ in ()).throw(OSError())
        dmi = lambda: "ReadyNAS 628X\n"
        self.assertEqual(m.detect_model(cpuinfo_reader=cpuinfo, dmi_reader=dmi), "rnx26")

    def test_dmi_316_selects_rn316(self):
        os.environ.pop("RN_MODEL", None)
        cpuinfo = lambda: (_ for _ in ()).throw(OSError())
        dmi = lambda: "ReadyNAS 316\n"
        self.assertEqual(m.detect_model(cpuinfo_reader=cpuinfo, dmi_reader=dmi), "rn316")

    def test_unknown_hardware_exits(self):
        os.environ.pop("RN_MODEL", None)
        cpuinfo = lambda: "model name\t: Some Other CPU\n"
        dmi = lambda: "Some Other Box\n"
        with self.assertRaises(SystemExit):
            m.detect_model(cpuinfo_reader=cpuinfo, dmi_reader=dmi)

    def test_missing_files_exit_not_crash(self):
        os.environ.pop("RN_MODEL", None)
        cpuinfo = lambda: (_ for _ in ()).throw(OSError())
        dmi = lambda: (_ for _ in ()).throw(OSError())
        with self.assertRaises(SystemExit):
            m.detect_model(cpuinfo_reader=cpuinfo, dmi_reader=dmi)


class TestGpioIdle(unittest.TestCase):
    def test_idle_drives_expected_levels(self):
        gp = FakeIchPortGpio(RNX26_PINS)
        gp.idle()
        # CS=1, RST=1, CLK=0, MOSI=0, DC=0
        for sig, expect in (("CS", 1), ("RST", 1), ("CLK", 0), ("MOSI", 0), ("DC", 0)):
            port, bit = gp._sig[sig]
            self.assertEqual((gp._rd_byte(port) >> bit) & 1, expect, sig)
        # EN untouched by idle() but must still read high since it shares
        # bank0 byte0 with CLK/RST, which idle() does write.
        self.assertEqual(gp._rd_byte(0x0C) & (1 << 6), 1 << 6)


# --------------------------------------------------------------------------
# Buttons: spec selection, Msp430Buttons debounce, Sx8635Buttons pure math
# and event flow. All below construct the button classes without opening
# any real i2c device (fakes skip __init__), per the same pattern as
# FakeIchPortGpio above.
# --------------------------------------------------------------------------
class FakeGpio:
    """Stands in for a Gpio backend: int_active() is whatever the test set."""
    def __init__(self, active=False):
        self.active = active

    def int_active(self):
        return self.active


class TestButtonClassSelection(unittest.TestCase):
    """spec["buttons"] -- not BTN_INT/has_int_line -- picks the class, and
    it's checkable with no device opened."""

    def test_rn316_selects_sx8635(self):
        self.assertIs(m._button_class(m.MODELS["rn316"]), m.Sx8635Buttons)

    def test_rn426_selects_msp430(self):
        self.assertIs(m._button_class(m.MODELS["rn426"]), m.Msp430Buttons)

    def test_rnx26_selects_msp430(self):
        self.assertIs(m._button_class(m.MODELS["rnx26"]), m.Msp430Buttons)

    def test_no_buttons_key_selects_none(self):
        self.assertIsNone(m._button_class({}))
        self.assertIsNone(m._button_class({"buttons": None}))


class FakeMsp430Buttons(m.Msp430Buttons):
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


class TestMsp430ButtonsEvents(unittest.TestCase):
    def test_one_read_per_assertion_and_rearm_after_50ms_inactive(self):
        gpio = FakeGpio(active=False)
        btn = FakeMsp430Buttons(gpio)

        # idle: no read, no action
        self.assertEqual(btn.events(0.0), [])
        self.assertEqual(btn.read_count, 0)

        # pad asserts: exactly one read, mapped action
        gpio.active = True
        btn.value = m.Msp430Buttons.UP
        self.assertEqual(btn.events(0.0), ["PREV"])
        self.assertEqual(btn.read_count, 1)

        # still asserted (the MCU's pulse train): no second read, no action,
        # until the pad has gone idle and re-armed
        self.assertEqual(btn.events(0.01), [])
        self.assertEqual(btn.read_count, 1)

        # pad drops but hasn't been idle 50ms yet: still not re-armed
        gpio.active = False
        self.assertEqual(btn.events(0.02), [])
        self.assertEqual(btn.events(0.06), [])   # 0.06-0.02=0.04s < 0.05s
        self.assertEqual(btn.read_count, 1)

        # idle >= 50ms: re-armed (but no assertion yet, so still no read)
        self.assertEqual(btn.events(0.08), [])   # 0.08-0.02=0.06s >= 0.05s
        self.assertEqual(btn.read_count, 1)

        # new assertion: reads again, one event per physical press
        gpio.active = True
        btn.value = m.Msp430Buttons.DOWN
        self.assertEqual(btn.events(0.09), ["NEXT"])
        self.assertEqual(btn.read_count, 2)

    def test_center_maps_to_ok(self):
        btn = FakeMsp430Buttons(FakeGpio(active=True))
        btn.value = m.Msp430Buttons.CENTER
        self.assertEqual(btn.events(0.0), ["OK"])

    def test_left_right_emit_own_actions(self):
        # LEFT/RIGHT are activity-only (see run()/_apply_actions): they wake
        # a sleeping display and reset the idle timer like any action, but
        # never move a page. events() itself just reports them.
        btn = FakeMsp430Buttons(FakeGpio(active=True))
        btn.value = m.Msp430Buttons.LEFT | m.Msp430Buttons.RIGHT
        self.assertEqual(btn.events(0.0), ["LEFT", "RIGHT"])

    def test_unknown_nonzero_bits_still_count_as_activity(self):
        # The old run() treated ANY nonzero reg 0x04 as activity (reset idle
        # timer, wake if asleep), even bits outside the five buttons.
        btn = FakeMsp430Buttons(FakeGpio(active=True))
        btn.value = 0x40
        self.assertEqual(btn.events(0.0), ["ACTIVITY"])


class TestWheelDelta(unittest.TestCase):
    """Wrap-aware delta, R=0x1f (rn316's observed wheel_range)."""
    R = 0x1f

    def test_wrap_forward(self):
        self.assertEqual(m._wheel_delta(0x00, 0x1e, self.R), 1)

    def test_wrap_backward(self):
        self.assertEqual(m._wheel_delta(0x1e, 0x00, self.R), -1)

    def test_small_step_backward(self):
        self.assertEqual(m._wheel_delta(0x00, 0x02, self.R), -2)


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
            acc += m._wheel_delta(pos, prev, R)
            prev = pos
            new_events, acc = m._wheel_emit(acc, detent)
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
        self.assertEqual(m._rising_actions(0x07, 0x00, []), ["OK"])

    def test_empty_keys_only_common_touch_bit_emits_nothing(self):
        # bitmap is only bit 0 (a lone ring touch) -- masks down to 0, must
        # never satisfy the "nonzero" fallback.
        self.assertEqual(m._rising_actions(0x01, 0x00, []), [])

    def test_no_rising_bits_emits_nothing(self):
        self.assertEqual(m._rising_actions(0x03, 0x03, []), [])

    def test_mapped_keys_bleed_across_calls_fires_once_at_completion(self):
        # DOWN's mask (0x0c) bleeds in one bit at a time across reads, like
        # the real trace (0x01 -> 0x09 -> 0x0d): must not fire until the
        # whole mask is satisfied, and then exactly once.
        keys = [(0x0c, "NEXT")]
        self.assertEqual(m._rising_actions(0x09, 0x01, keys), [])   # only bit 3 so far
        self.assertEqual(m._rising_actions(0x0d, 0x09, keys), ["NEXT"])   # mask now complete
        self.assertEqual(m._rising_actions(0x0d, 0x0d, keys), [])   # already was complete

    def test_mapped_keys_first_match_in_list_order_wins(self):
        # Both masks newly satisfied in the same single call -- only the
        # first one in list order fires.
        keys = [(0x02, "OK"), (0x0c, "NEXT")]
        self.assertEqual(m._rising_actions(0x0e, 0x00, keys), ["OK"])
        keys_reordered = [(0x0c, "NEXT"), (0x02, "OK")]
        self.assertEqual(m._rising_actions(0x0e, 0x00, keys_reordered), ["NEXT"])

    def test_mapped_keys_only_common_touch_bit_emits_nothing(self):
        # A bitmap of only bit 0 can never match a real (bit-0-free) mask.
        keys = [(0x02, "OK"), (0x0c, "NEXT"), (0x30, "RIGHT")]
        self.assertEqual(m._rising_actions(0x01, 0x00, keys), [])


SX8635_SPEC = m.MODELS["rn316"]["sx8635"]


class FakeSx8635Buttons(m.Sx8635Buttons):
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
        btn = _sx8635(keys=SX8635_SPEC["keys"])
        results = []
        for bitmap in (0x01, 0x09, 0x0d, 0x00):
            btn.queue = [0x04, bitmap]
            results += btn.events(0.0)
        self.assertEqual(results, ["NEXT"])

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


class TestApplyActions(unittest.TestCase):
    def test_asleep_wakes_and_discards_batch(self):
        idx, last_show, activity, asleep, woke = m._apply_actions(
            ["NEXT", "NEXT"], idx=2, last_show=5.0, activity=0.0, asleep=True, now=10.0)
        self.assertTrue(woke)
        self.assertFalse(asleep)
        self.assertEqual(idx, 2)          # whole batch discarded, no page move
        self.assertEqual(last_show, 0)    # force a redraw after waking
        self.assertEqual(activity, 10.0)

    def test_awake_left_is_activity_only(self):
        idx, last_show, activity, asleep, woke = m._apply_actions(
            ["LEFT"], idx=1, last_show=5.0, activity=0.0, asleep=False, now=10.0)
        self.assertFalse(woke)
        self.assertFalse(asleep)
        self.assertEqual(idx, 1)          # no page move
        self.assertEqual(last_show, 5.0)  # no forced redraw
        self.assertEqual(activity, 10.0)  # but activity is updated

    def test_awake_next_and_prev_move_idx(self):
        idx, *_ = m._apply_actions(["NEXT"], idx=0, last_show=5.0, activity=0.0, asleep=False, now=1.0)
        self.assertEqual(idx, 1 % len(m.PAGES))
        idx2, *_ = m._apply_actions(["PREV"], idx=0, last_show=5.0, activity=0.0, asleep=False, now=1.0)
        self.assertEqual(idx2, (-1) % len(m.PAGES))

    def test_awake_ok_forces_redraw(self):
        _, last_show, *_ = m._apply_actions(["OK"], idx=0, last_show=5.0, activity=0.0, asleep=False, now=1.0)
        self.assertEqual(last_show, 0)


class TestBuildButtonsDegradeGate(unittest.TestCase):
    """Tests the model gate in _build_buttons without opening any device:
    a fake button class raises OSError on construction, standing in for a
    missing i2c bus or a NAK'd chip."""

    class _FakeFailingButtons:
        def __init__(self, gpio):
            raise OSError("no such device")

    def setUp(self):
        self._orig = dict(m.BUTTON_CLASSES)
        m.BUTTON_CLASSES["msp430"] = self._FakeFailingButtons

    def tearDown(self):
        m.BUTTON_CLASSES.clear()
        m.BUTTON_CLASSES.update(self._orig)

    def _gpio(self):
        gpio = FakeGpio(active=False)
        gpio.has_int_line = True
        return gpio

    def test_rn426_construction_error_reraises(self):
        with self.assertRaises(OSError):
            m._build_buttons(self._gpio(), "rn426", {"buttons": "msp430"})

    def test_other_model_construction_error_degrades(self):
        result = m._build_buttons(self._gpio(), "rnx26", {"buttons": "msp430"})
        self.assertIsNone(result)


class TestSx8635NoWritePath(unittest.TestCase):
    def test_source_has_no_write_call(self):
        import inspect
        src = inspect.getsource(m.Sx8635Buttons)
        self.assertNotIn("pwrite", src)
        self.assertNotIn("_smbus_ioctl(0", src)
        self.assertEqual(src.count("fcntl.ioctl(self.fd, I2C_SMBUS"), 1)


if __name__ == "__main__":
    unittest.main()
