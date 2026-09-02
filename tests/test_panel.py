import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rn426_panel as m


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
        port, bit = m._ich_line_addr(0x1000, m.IchPortGpio.CS)
        self.assertEqual(port, 0x1000 + 0x3A)
        self.assertEqual(bit, 2)

    def test_pins_match_class_attrs(self):
        for name, (line, _, _) in self.CASES.items():
            self.assertEqual(getattr(m.IchPortGpio, name), line)


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
    skipped, gpiobase is fixed at 0, and _rd_byte/_wr_byte are backed by a
    plain dict standing in for the three whitelisted I/O ports."""

    def __init__(self):
        self.gpiobase = 0
        self.port = {}

    def _rd_byte(self, port):
        return self.port.get(port, 0)

    def _wr_byte(self, port, val):
        if port not in self._whitelist():
            raise ValueError("not whitelisted: 0x%x" % port)
        self.port[port] = val & 0xFF


class TestIchEnResetInvariant(unittest.TestCase):
    def setUp(self):
        self.gp = FakeIchPortGpio()

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
        gp = FakeIchPortGpio()
        with self.assertRaises(ValueError):
            gp._wr_byte(0xFFFF, 0)

    def test_accepts_whitelisted_ports(self):
        gp = FakeIchPortGpio()
        for port in (0x0C, 0x38, 0x3A):
            gp._wr_byte(port, 0x00)   # must not raise


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
        gp = FakeIchPortGpio()
        gp.idle()
        # CS=1, RST=1, CLK=0, MOSI=0, DC=0
        self.assertEqual(gp._rd_byte(m._ich_line_addr(0, m.IchPortGpio.CS)[0]) >> m._ich_line_addr(0, m.IchPortGpio.CS)[1] & 1, 1)
        self.assertEqual(gp._rd_byte(m._ich_line_addr(0, m.IchPortGpio.RST)[0]) >> m._ich_line_addr(0, m.IchPortGpio.RST)[1] & 1, 1)
        self.assertEqual(gp._rd_byte(m._ich_line_addr(0, m.IchPortGpio.CLK)[0]) >> m._ich_line_addr(0, m.IchPortGpio.CLK)[1] & 1, 0)
        self.assertEqual(gp._rd_byte(m._ich_line_addr(0, m.IchPortGpio.MOSI)[0]) >> m._ich_line_addr(0, m.IchPortGpio.MOSI)[1] & 1, 0)
        self.assertEqual(gp._rd_byte(m._ich_line_addr(0, m.IchPortGpio.DC)[0]) >> m._ich_line_addr(0, m.IchPortGpio.DC)[1] & 1, 0)
        # EN untouched by idle() but must still read high since it shares
        # bank0 byte0 with CLK/RST, which idle() does write.
        self.assertEqual(gp._rd_byte(0x0C) & (1 << 6), 1 << 6)


if __name__ == "__main__":
    unittest.main()
