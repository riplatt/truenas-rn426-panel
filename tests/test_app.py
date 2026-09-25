import contextlib
import io
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import rnpanel.app as app
import rnpanel.sx8635 as sx8635
import rnpanel.sx8635_spm as sx8635_spm
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


class _FakeSpm(object):
    """Stands in for rnpanel.sx8635_spm in _choose_sx8635_layout tests:
    recover()/read_block1() are pure call recorders (no real I/O, no real
    writes -- fd is never a real file descriptor here), and layout_of is the
    real, pure classification function (no I/O either), so these tests
    exercise the actual netgear/qsm/unknown decision, not a reimplementation
    of it."""
    layout_of = staticmethod(sx8635_spm.layout_of)

    def __init__(self, block=None, raise_on_read=False):
        self.block = block
        self.raise_on_read = raise_on_read
        self.recover_calls = []
        self.read_calls = []

    def recover(self, fd):
        self.recover_calls.append(fd)

    def read_block1(self, fd):
        self.read_calls.append(fd)
        if self.raise_on_read:
            raise OSError("simulated NAK")
        return self.block


NETGEAR_BLOCK = (0x00, 0x04, 0x0F, 0xFF, 0xF5, 0x75, 0x55, 0x55)
QSM_BLOCK = (0x00, 0x04, 0xFF, 0xF5, 0x55, 0x77, 0x77, 0x77)
UNKNOWN_BLOCK = (0x00, 0x04, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66)


class TestChooseSx8635Layout(unittest.TestCase):
    """_choose_sx8635_layout picks netgear/qsm from a (faked) SPM block 1
    read, forces qsm on any failure or on RN_SX8635_SPM=0, and logs exactly
    one stderr line every time -- see app.py's docstring and
    .claude/advice/rn316-postwrite-review.md section D ("Gating")."""

    def setUp(self):
        self._old_env = os.environ.get("RN_SX8635_SPM")

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("RN_SX8635_SPM", None)
        else:
            os.environ["RN_SX8635_SPM"] = self._old_env

    def _run(self, spm, env=None):
        if env is None:
            os.environ.pop("RN_SX8635_SPM", None)
        else:
            os.environ["RN_SX8635_SPM"] = env
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            name = app._choose_sx8635_layout(
                0x2b, spm=spm, open_fd=lambda addr: 999, close_fd=lambda fd: None)
        return name, buf.getvalue()

    def test_netgear_block_selects_netgear(self):
        spm = _FakeSpm(NETGEAR_BLOCK)
        name, out = self._run(spm)
        self.assertEqual(name, "netgear")
        self.assertEqual(len(out.rstrip("\n").split("\n")), 1)
        self.assertIn("netgear", out)
        self.assertEqual(spm.recover_calls, [999])
        self.assertEqual(spm.read_calls, [999])

    def test_qsm_block_selects_qsm(self):
        spm = _FakeSpm(QSM_BLOCK)
        name, out = self._run(spm)
        self.assertEqual(name, "qsm")
        self.assertEqual(len(out.rstrip("\n").split("\n")), 1)
        self.assertNotIn("WARN", out)

    def test_unknown_block_selects_qsm_with_warn(self):
        spm = _FakeSpm(UNKNOWN_BLOCK)
        name, out = self._run(spm)
        self.assertEqual(name, "qsm")
        self.assertEqual(len(out.rstrip("\n").split("\n")), 1)
        self.assertIn("WARN", out)

    def test_oserror_on_read_selects_qsm(self):
        spm = _FakeSpm(raise_on_read=True)
        name, out = self._run(spm)
        self.assertEqual(name, "qsm")
        self.assertEqual(len(out.rstrip("\n").split("\n")), 1)
        self.assertEqual(spm.recover_calls, [999])

    def test_env_zero_forces_qsm_without_opening_device(self):
        spm = _FakeSpm(NETGEAR_BLOCK)
        name, out = self._run(spm, env="0")
        self.assertEqual(name, "qsm")
        self.assertEqual(spm.recover_calls, [])   # no window opened at all
        self.assertEqual(spm.read_calls, [])
        self.assertEqual(len(out.rstrip("\n").split("\n")), 1)


class TestBuildButtonsSx8635Layout(unittest.TestCase):
    """_build_buttons merges the chosen layout's dict (from
    MODELS["rn316"]["sx8635"]["layouts"]) with "addr" and constructs
    Sx8635Buttons with that -- not the raw sx8635 model spec (which now
    holds "layouts", not "wheel_range" etc. directly)."""

    class _RecordingSx8635(object):
        last_spec = None

        def __init__(self, gpio, spec):
            type(self).last_spec = spec

    def setUp(self):
        self._orig_classes = dict(app.BUTTON_CLASSES)
        self._orig_class_attr = app.Sx8635Buttons
        self._orig_choose = app._choose_sx8635_layout
        app.BUTTON_CLASSES["sx8635"] = self._RecordingSx8635
        app.Sx8635Buttons = self._RecordingSx8635

    def tearDown(self):
        app.BUTTON_CLASSES.clear()
        app.BUTTON_CLASSES.update(self._orig_classes)
        app.Sx8635Buttons = self._orig_class_attr
        app._choose_sx8635_layout = self._orig_choose

    def _gpio(self):
        gpio = FakeGpio(active=False)
        gpio.has_int_line = True
        return gpio

    def test_netgear_layout_dict_passed_through_with_addr(self):
        app._choose_sx8635_layout = lambda addr: "netgear"
        app._build_buttons(self._gpio(), "rn316", MODELS["rn316"])
        spec = self._RecordingSx8635.last_spec
        expected = MODELS["rn316"]["sx8635"]["layouts"]["netgear"]
        self.assertEqual(spec["addr"], MODELS["rn316"]["sx8635"]["addr"])
        self.assertEqual(spec["wheel_range"], expected["wheel_range"])
        self.assertEqual(spec["detent"], expected["detent"])
        self.assertEqual(spec["compass"], expected["compass"])
        self.assertEqual(spec["keys"], expected["keys"])

    def test_qsm_layout_dict_passed_through_with_addr(self):
        app._choose_sx8635_layout = lambda addr: "qsm"
        app._build_buttons(self._gpio(), "rn316", MODELS["rn316"])
        spec = self._RecordingSx8635.last_spec
        expected = MODELS["rn316"]["sx8635"]["layouts"]["qsm"]
        self.assertEqual(spec["addr"], MODELS["rn316"]["sx8635"]["addr"])
        self.assertEqual(spec["wheel_range"], expected["wheel_range"])
        self.assertEqual(spec["keys"], expected["keys"])
        self.assertEqual(spec["compass"], [])


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
