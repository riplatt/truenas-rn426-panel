import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from rnpanel.msp430 import Msp430Buttons
from fakes import FakeGpio, FakeMsp430Buttons


class TestMsp430ButtonsEvents(unittest.TestCase):
    def test_one_read_per_assertion_and_rearm_after_50ms_inactive(self):
        gpio = FakeGpio(active=False)
        btn = FakeMsp430Buttons(gpio)

        # idle: no read, no action
        self.assertEqual(btn.events(0.0), [])
        self.assertEqual(btn.read_count, 0)

        # pad asserts: exactly one read, mapped action
        gpio.active = True
        btn.value = Msp430Buttons.UP
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
        btn.value = Msp430Buttons.DOWN
        self.assertEqual(btn.events(0.09), ["NEXT"])
        self.assertEqual(btn.read_count, 2)

    def test_center_maps_to_ok(self):
        btn = FakeMsp430Buttons(FakeGpio(active=True))
        btn.value = Msp430Buttons.CENTER
        self.assertEqual(btn.events(0.0), ["OK"])

    def test_left_right_emit_own_actions(self):
        # LEFT/RIGHT are activity-only (see run()/_apply_actions): they wake
        # a sleeping display and reset the idle timer like any action, but
        # never move a page. events() itself just reports them.
        btn = FakeMsp430Buttons(FakeGpio(active=True))
        btn.value = Msp430Buttons.LEFT | Msp430Buttons.RIGHT
        self.assertEqual(btn.events(0.0), ["LEFT", "RIGHT"])

    def test_unknown_nonzero_bits_still_count_as_activity(self):
        # The old run() treated ANY nonzero reg 0x04 as activity (reset idle
        # timer, wake if asleep), even bits outside the five buttons.
        btn = FakeMsp430Buttons(FakeGpio(active=True))
        btn.value = 0x40
        self.assertEqual(btn.events(0.0), ["ACTIVITY"])


if __name__ == "__main__":
    unittest.main()
