import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import rnpanel.app as app
import rnpanel.sx8635 as sx8635
from rnpanel.models import MODELS
from fakes import FakeGpio


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
        self.assertEqual(app._rotate_seconds(), 10)

    def test_env_override(self):
        os.environ["RN_ROTATE"] = "3"
        self.assertEqual(app._rotate_seconds(), 3)

    def test_zero_disables(self):
        os.environ["RN_ROTATE"] = "0"
        self.assertEqual(app._rotate_seconds(), 0)


class TestButtonClassSelection(unittest.TestCase):
    """spec["buttons"] -- not BTN_INT/has_int_line -- picks the class, and
    it's checkable with no device opened."""

    def test_rn316_selects_sx8635(self):
        self.assertIs(app._button_class(MODELS["rn316"]), app.Sx8635Buttons)

    def test_rn426_selects_msp430(self):
        self.assertIs(app._button_class(MODELS["rn426"]), app.Msp430Buttons)

    def test_rnx26_selects_msp430(self):
        self.assertIs(app._button_class(MODELS["rnx26"]), app.Msp430Buttons)

    def test_no_buttons_key_selects_none(self):
        self.assertIsNone(app._button_class({}))
        self.assertIsNone(app._button_class({"buttons": None}))

    def test_sx8635_identity_consistent_across_modules(self):
        # Guards against the double-import trap: the class app.BUTTON_CLASSES
        # points at, the one rnpanel.sx8635 defines, and the one bound into
        # rnpanel.app's own namespace by its "from rnpanel.sx8635 import
        # Sx8635Buttons" must all be the exact same object. Only breaks if
        # rnpanel.sx8635 were ever imported both as "rnpanel.sx8635" and as
        # bare "sx8635" in the same process (e.g. rnpanel/ itself put on
        # sys.path) -- see the fresh-interpreter import check.
        self.assertIs(app.BUTTON_CLASSES["sx8635"], sx8635.Sx8635Buttons)
        self.assertIs(app.BUTTON_CLASSES["sx8635"], app.Sx8635Buttons)


class TestApplyActions(unittest.TestCase):
    def test_asleep_wakes_and_discards_batch(self):
        idx, last_show, activity, asleep, woke = app._apply_actions(
            ["NEXT", "NEXT"], idx=2, last_show=5.0, activity=0.0, asleep=True, now=10.0)
        self.assertTrue(woke)
        self.assertFalse(asleep)
        self.assertEqual(idx, 2)          # whole batch discarded, no page move
        self.assertEqual(last_show, 0)    # force a redraw after waking
        self.assertEqual(activity, 10.0)

    def test_awake_left_is_activity_only(self):
        idx, last_show, activity, asleep, woke = app._apply_actions(
            ["LEFT"], idx=1, last_show=5.0, activity=0.0, asleep=False, now=10.0)
        self.assertFalse(woke)
        self.assertFalse(asleep)
        self.assertEqual(idx, 1)          # no page move
        self.assertEqual(last_show, 5.0)  # no forced redraw
        self.assertEqual(activity, 10.0)  # but activity is updated

    def test_awake_next_and_prev_move_idx(self):
        idx, *_ = app._apply_actions(["NEXT"], idx=0, last_show=5.0, activity=0.0, asleep=False, now=1.0)
        self.assertEqual(idx, 1 % len(app.PAGES))
        idx2, *_ = app._apply_actions(["PREV"], idx=0, last_show=5.0, activity=0.0, asleep=False, now=1.0)
        self.assertEqual(idx2, (-1) % len(app.PAGES))

    def test_awake_ok_forces_redraw(self):
        _, last_show, *_ = app._apply_actions(["OK"], idx=0, last_show=5.0, activity=0.0, asleep=False, now=1.0)
        self.assertEqual(last_show, 0)


class TestBuildButtonsDegradeGate(unittest.TestCase):
    """Tests the model gate in _build_buttons without opening any device:
    a fake button class raises OSError on construction, standing in for a
    missing i2c bus or a NAK'd chip."""

    class _FakeFailingButtons:
        def __init__(self, gpio):
            raise OSError("no such device")

    def setUp(self):
        self._orig = dict(app.BUTTON_CLASSES)
        app.BUTTON_CLASSES["msp430"] = self._FakeFailingButtons

    def tearDown(self):
        app.BUTTON_CLASSES.clear()
        app.BUTTON_CLASSES.update(self._orig)

    def _gpio(self):
        gpio = FakeGpio(active=False)
        gpio.has_int_line = True
        return gpio

    def test_rn426_construction_error_reraises(self):
        with self.assertRaises(OSError):
            app._build_buttons(self._gpio(), "rn426", {"buttons": "msp430"})

    def test_other_model_construction_error_degrades(self):
        result = app._build_buttons(self._gpio(), "rnx26", {"buttons": "msp430"})
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
