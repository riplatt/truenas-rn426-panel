import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from rnpanel.gpio import _ich_line_addr, _parse_gpiobase, _check_lpc_id, _check_gpio_en, _check_ngpio
from fakes import RNX26_PINS, RN316_PINS, NO_INT_LINE_PINS, FakeIchPortGpio


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
            port, bit = _ich_line_addr(0, line)
            self.assertEqual(port, expect_port, "%s port" % name)
            self.assertEqual(bit, expect_bit, "%s bit" % name)

    def test_gpiobase_offset(self):
        # a nonzero gpiobase should shift the port by exactly that amount
        port, bit = _ich_line_addr(0x1000, RNX26_PINS["CS"])
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
        base = _parse_gpiobase(self._cfg(0x0000F5C1))
        self.assertEqual(base, 0x0000F5C1 & 0x0000FF80)
        self.assertEqual(base, 0xF580)

    def test_zero_raises(self):
        with self.assertRaises(RuntimeError):
            _parse_gpiobase(self._cfg(0))

    def test_short_blob_raises(self):
        with self.assertRaises(RuntimeError):
            _parse_gpiobase(b"\x00" * 4)


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
        _check_lpc_id(0x8c54, 0x8c54)
        _check_lpc_id(0x3a18, 0x3a18)

    def test_mismatch_raises(self):
        with self.assertRaises(RuntimeError):
            _check_lpc_id(0x3a18, 0x8c54)


class TestGpioEnCheck(unittest.TestCase):
    def test_bit_set_does_not_raise(self):
        _check_gpio_en(0x10)   # bit 4 only
        _check_gpio_en(0xFF)   # bit 4 among others

    def test_bit_clear_raises(self):
        with self.assertRaises(RuntimeError):
            _check_gpio_en(0x00)

    def test_bit_clear_among_others_still_raises(self):
        with self.assertRaises(RuntimeError):
            _check_gpio_en(0xEF)   # every bit but bit 4


class TestNgpioCheck(unittest.TestCase):
    def test_rnx26_lines_all_under_76(self):
        _check_ngpio(RNX26_PINS, 76)   # must not raise

    def test_rn316_lines_all_under_61(self):
        _check_ngpio(RN316_PINS, 61)   # must not raise

    def test_line_equal_to_ngpio_raises(self):
        # a line number equal to ngpio is out of range (lines are 0..ngpio-1)
        pins = dict(RNX26_PINS)
        pins["CS"] = 76
        with self.assertRaises(ValueError):
            _check_ngpio(pins, 76)

    def test_btn_int_over_ngpio_raises(self):
        pins = dict(RNX26_PINS)
        pins["BTN_INT"] = 76
        with self.assertRaises(ValueError):
            _check_ngpio(pins, 76)

    def test_construction_raises_when_a_line_is_out_of_range(self):
        # _derive_maps (called from __init__) must enforce this too, not
        # just the free function in isolation.
        pins = dict(RNX26_PINS)
        pins["CS"] = 100
        with self.assertRaises(ValueError):
            FakeIchPortGpio(pins, ngpio=76)


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


if __name__ == "__main__":
    unittest.main()
