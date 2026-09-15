import importlib.util
import os
import sys
import unittest

# sx8635-watch.py has a hyphen in its filename, so it can't be a plain
# `import tools.sx8635_watch` -- load it by path instead. This also proves
# the module imports cleanly on Windows (no fcntl, no Pillow) as required.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOL_PATH = os.path.join(_REPO_ROOT, "tools", "sx8635-watch.py")
sys.path.insert(0, _REPO_ROOT)

_spec = importlib.util.spec_from_file_location("sx8635_watch", _TOOL_PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class TestComputeVerdict(unittest.TestCase):
    """One case per row of the Q6 decision table."""

    def test_read_only_works(self):
        summary = {
            "nvm_valid": True, "nvm_count": 4,
            "button_bits_seen": True, "wheel_activity_seen": True,
            "nirq_ever_low": True, "nirq_zero_to_one_after_read": 5,
            "irq_nonzero_count": 12, "attempted_reads": 500, "oserror_count": 0,
        }
        self.assertEqual(m.compute_verdict(summary), "read-only works")

    def test_needs_spm_load(self):
        summary = {
            "nvm_valid": False, "nvm_count": 0,
            "button_bits_seen": False, "wheel_activity_seen": False,
            "nirq_ever_low": True, "nirq_zero_to_one_after_read": 1,
            "irq_nonzero_count": 1, "attempted_reads": 500, "oserror_count": 0,
        }
        self.assertEqual(m.compute_verdict(summary), "needs SPM load")

    def test_read_only_works_on_qsm_defaults_if_touch_responds(self):
        # NVM never burned, but buttons and wheel both produced activity:
        # the config is good enough, so no SPM write should be recommended.
        summary = {
            "nvm_valid": False, "nvm_count": 0,
            "button_bits_seen": True, "wheel_activity_seen": True,
            "nirq_ever_low": True, "nirq_zero_to_one_after_read": 3,
            "irq_nonzero_count": 9, "attempted_reads": 500, "oserror_count": 0,
        }
        self.assertEqual(m.compute_verdict(summary), "read-only works")

    def test_gating_or_chip_check_line_2(self):
        summary = {
            "nvm_valid": None, "nvm_count": None,
            "button_bits_seen": False, "wheel_activity_seen": False,
            "nirq_ever_low": False, "nirq_zero_to_one_after_read": 0,
            "irq_nonzero_count": 0, "attempted_reads": 500, "oserror_count": 0,
        }
        self.assertEqual(m.compute_verdict(summary), "gating-or-chip check line 2")

    def test_chip_absent(self):
        summary = {
            "nvm_valid": None, "nvm_count": None,
            "button_bits_seen": False, "wheel_activity_seen": False,
            "nirq_ever_low": False, "nirq_zero_to_one_after_read": 0,
            "irq_nonzero_count": 0, "attempted_reads": 50, "oserror_count": 50,
        }
        self.assertEqual(m.compute_verdict(summary), "chip absent")

    def test_chip_absent_takes_priority_over_read_only_works(self):
        # every read errored AND the (stale/zeroed) evidence fields look like
        # a working chip -- OSError-on-everything must still win.
        summary = {
            "nvm_valid": True, "nvm_count": 4,
            "button_bits_seen": True, "wheel_activity_seen": True,
            "nirq_ever_low": True, "nirq_zero_to_one_after_read": 5,
            "irq_nonzero_count": 12, "attempted_reads": 10, "oserror_count": 10,
        }
        self.assertEqual(m.compute_verdict(summary), "chip absent")


class TestDecodeIrqsrc(unittest.TestCase):
    CASES = [
        (0x40, ["nvmburn"]),
        (0x20, ["spmwrite"]),
        (0x10, ["gpi"]),
        (0x08, ["wheel"]),
        (0x04, ["buttons"]),
        (0x02, ["comp"]),
        (0x01, ["opmode"]),
        (0x00, []),
        (0x0C, ["wheel", "buttons"]),          # combination
        (0x7F, ["nvmburn", "spmwrite", "gpi", "wheel", "buttons", "comp", "opmode"]),  # all bits
    ]

    def test_each_bit_and_combinations(self):
        for val, expect in self.CASES:
            self.assertEqual(m.decode_irqsrc(val), expect, "0x%02x" % val)

    def test_format_irq_no_brackets_when_zero(self):
        self.assertEqual(m.format_irq(0x00), "0x00")

    def test_format_irq_brackets_named_bits(self):
        self.assertEqual(m.format_irq(0x08), "0x08[wheel]")
        self.assertEqual(m.format_irq(0x0C), "0x0c[wheel,buttons]")


class TestDecodeSpmstat(unittest.TestCase):
    def test_never_burned_qsm(self):
        self.assertEqual(m.decode_spmstat(0x00), {"nvm_valid": False, "nvm_count": 0})

    def test_nvm_valid_count_one(self):
        self.assertEqual(m.decode_spmstat(0x09), {"nvm_valid": True, "nvm_count": 1})

    def test_nvm_valid_count_max(self):
        # NvmCount saturates at "4 = more than three times, QSM is used" per
        # the design doc's reading of Table 27 -- decode is just bits 2:0.
        self.assertEqual(m.decode_spmstat(0x0C), {"nvm_valid": True, "nvm_count": 4})

    def test_count_without_valid_bit(self):
        self.assertEqual(m.decode_spmstat(0x03), {"nvm_valid": False, "nvm_count": 3})


class TestDecodeCompOpMode(unittest.TestCase):
    def test_active_no_comp(self):
        self.assertEqual(m.decode_compopmode(0x00), {"mode": "Active", "comp": False})

    def test_doze(self):
        self.assertEqual(m.decode_compopmode(0x01), {"mode": "Doze", "comp": False})

    def test_sleep(self):
        self.assertEqual(m.decode_compopmode(0x02), {"mode": "Sleep", "comp": False})

    def test_reserved_mode_bits(self):
        self.assertEqual(m.decode_compopmode(0x03)["mode"], "Reserved(3)")

    def test_comp_flag_with_active(self):
        self.assertEqual(m.decode_compopmode(0x04), {"mode": "Active", "comp": True})

    def test_comp_flag_with_doze(self):
        self.assertEqual(m.decode_compopmode(0x05), {"mode": "Doze", "comp": True})


if __name__ == "__main__":
    unittest.main()
