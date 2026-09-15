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


class TestBitmapUnionBits(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(m.bitmap_union_bits([]), set())

    def test_single_value(self):
        self.assertEqual(m.bitmap_union_bits([0x05]), {0, 2})

    def test_union_across_values(self):
        # 0x01 -> bit0, 0x08 -> bit3, 0x00 contributes nothing
        self.assertEqual(m.bitmap_union_bits([0x01, 0x08, 0x00]), {0, 3})

    def test_all_bits(self):
        self.assertEqual(m.bitmap_union_bits([0xFF]), set(range(8)))


class TestComputeBitsUniqueToPhase(unittest.TestCase):
    def test_pad_bit_not_in_baseline(self):
        # bit0 is noise present everywhere (IDLE and OK); UP's extra bit3
        # is unique to UP; RIGHT shares bit3 with UP so it's NOT unique.
        phase_bitmaps = {
            "IDLE1": [0x00, 0x01],
            "OK": [0x01, 0x03],
            "UP": [0x01, 0x09],      # bit0 (noise) + bit3 (new)
            "RIGHT": [0x01, 0x09],   # same bit3 as UP -- not unique to either
        }
        result = m.compute_bits_unique_to_phase(
            phase_bitmaps, ["IDLE1", "OK"], ["UP", "RIGHT"])
        self.assertEqual(result, {"UP": [3], "RIGHT": [3]})

    def test_bit_unique_to_single_phase(self):
        phase_bitmaps = {
            "IDLE1": [0x00],
            "OK": [0x01],
            "UP": [0x01, 0x05],      # bit2 new
            "DOWN": [0x01],          # nothing new
        }
        result = m.compute_bits_unique_to_phase(
            phase_bitmaps, ["IDLE1", "OK"], ["UP", "DOWN"])
        self.assertEqual(result, {"UP": [2], "DOWN": []})

    def test_missing_phase_treated_as_empty(self):
        result = m.compute_bits_unique_to_phase({}, ["IDLE1"], ["UP"])
        self.assertEqual(result, {"UP": []})


class TestDetectWheelDirection(unittest.TestCase):
    def test_no_movement_single_value(self):
        self.assertEqual(m.detect_wheel_direction([0x10, 0x10, 0x10]), "none")

    def test_empty(self):
        self.assertEqual(m.detect_wheel_direction([]), "none")

    def test_plain_increasing(self):
        self.assertEqual(m.detect_wheel_direction([0x00, 0x02, 0x05, 0x09]), "increasing")

    def test_plain_decreasing(self):
        self.assertEqual(m.detect_wheel_direction([0x09, 0x05, 0x02, 0x00]), "decreasing")

    def test_decreasing_through_a_wrap(self):
        # 0x1e,0x1b,...,0x02,0x00,0x1e -- counting down, wraps 0x00 -> 0x1e
        positions = [0x1e, 0x1b, 0x18, 0x02, 0x00, 0x1e]
        self.assertEqual(m.detect_wheel_direction(positions), "decreasing")

    def test_increasing_through_a_wrap(self):
        # 0x1a,0x1d,0x1e,0x00,0x02 -- counting up, wraps 0x1e -> 0x00
        positions = [0x1a, 0x1d, 0x1e, 0x00, 0x02]
        self.assertEqual(m.detect_wheel_direction(positions), "increasing")

    def test_ambiguous_equal_and_opposite(self):
        # one step up, one step down of equal magnitude, no wrap involved
        self.assertEqual(m.detect_wheel_direction([0x05, 0x08, 0x05]), "ambiguous")


if __name__ == "__main__":
    unittest.main()
