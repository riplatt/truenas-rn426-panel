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
from fakes import FakeGpio, _sx8635


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


class _RecLcd(object):
    def __init__(self):
        self.frames = []

    def lines(self, a, b):
        self.frames.append((a, b))

    def init(self):
        pass


def _script_btn(script):
    """A real-shaped (netgear) button object whose events() replays a script
    of (actions, last_touch) per call, then raises KeyboardInterrupt, like
    Ctrl-C."""
    btn = _sx8635(keys=MODELS["rn316"]["sx8635"]["layouts"]["netgear"]["keys"],
                  layout="netgear")
    btn.trace = True
    btn.script = list(script)
    btn.calls = []

    def events(now):
        btn.calls.append(now)
        if not btn.script:
            raise KeyboardInterrupt
        actions, lt = btn.script.pop(0)
        if lt is not None:
            btn.last_touch = lt
        return actions
    btn.events = events
    return btn


def _lt(seq, **kw):
    d = dict(seq=seq, landing=50, release=50, excursion=0, dur=0.2, rotated=False,
             scrolled=False, net=0, outcome="tap", name="UP")
    d.update(kw)
    return d


class TestPanelTestMode(unittest.TestCase):
    def setUp(self):
        self._saved = (app._service_active, app._build, app._build_buttons, app.time.sleep)
        app.time.sleep = lambda s: None
        self.lcd = _RecLcd()
        self.btn = None
        app._service_active = lambda: False
        app._build = lambda model: (object(), self.lcd)
        app._build_buttons = lambda gpio, model, spec: self.btn

    def tearDown(self):
        app._service_active, app._build, app._build_buttons, app.time.sleep = self._saved

    def _run(self, model="rn316"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            app.test(model)
        return out.getvalue(), err.getvalue()

    def test_main_dispatches_test(self):
        seen = []
        saved = (app.test, app.detect_model, sys.argv)
        app.test = lambda model: seen.append(model)
        app.detect_model = lambda: "rn426"
        sys.argv = ["rn426_panel.py", "test"]
        try:
            app.main()
        finally:
            app.test, app.detect_model, sys.argv = saved
        self.assertEqual(seen, ["rn426"])

    def test_finishes_only_when_all_seen_and_quiet(self):
        class Pt(object):
            def __init__(self, l1): self._l1 = l1
            def line1(self): return self._l1
        self.assertFalse(app._test_finished(Pt("try UP"), 60))
        self.assertFalse(app._test_finished(Pt("all seen"), app.TEST_DONE_IDLE - 0.1))
        self.assertTrue(app._test_finished(Pt("all seen"), app.TEST_DONE_IDLE))

    def test_activity_token_moves_on_a_repeat_press(self):
        # Same button twice leaves both LCD lines unchanged; it must still
        # count as activity so the quiet timer restarts.
        class Pt(object):
            counts = {"OK": 1}; last_seq = 0; live_net = 0
        a = app._test_activity(Pt, None)
        Pt.counts = {"OK": 2}
        self.assertNotEqual(app._test_activity(Pt, None), a)

    def test_exits_1_when_buttons_none(self):
        with self.assertRaises(SystemExit) as cm:
            self._run()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(self.lcd.frames, [])

    def test_refuses_when_service_active(self):
        app._service_active = lambda: True
        built = []
        app._build = lambda model: built.append(model)
        with self.assertRaises(SystemExit) as cm:
            self._run()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(built, [])   # refused before touching any hardware

    def test_service_check_tolerates_missing_systemctl(self):
        saved = app.subprocess.call
        app._service_active = self._saved[0]
        def boom(*a, **k):
            raise OSError("no systemctl")
        app.subprocess.call = boom
        try:
            self.assertFalse(app._service_active())
        finally:
            app.subprocess.call = saved

    def test_service_check_uses_is_active_quiet(self):
        saved = app.subprocess.call
        app._service_active = self._saved[0]
        got = []
        app.subprocess.call = lambda cmd: got.append(cmd) or 0
        try:
            self.assertTrue(app._service_active())
        finally:
            app.subprocess.call = saved
        self.assertEqual(got, [["systemctl", "is-active", "--quiet", "rn426-panel"]])

    def test_redraws_only_on_action_or_episode_and_only_on_change(self):
        tap = _lt(1)
        self.btn = _script_btn([
            ([], None),                  # idle
            (["OK"], None),              # action: line 2 changes -> draw
            (["OK"], None),              # action, lines unchanged -> no draw
            ([], None),                  # idle
            (["PREV"], tap),             # tap episode -> draw
            ([], tap),                   # same episode re-read -> no draw
        ])
        out, _ = self._run()
        # frame 0 = the timed initial frame, then OK, then the tap, then Test done
        self.assertEqual(self.lcd.frames, [
            ("try OK", "waiting"),
            ("try tap UP", "OK"),
            ("try tap DOWN", "tap UP @50"),
            ("Test done", ""),
        ])
        self.assertIn("frame time:", out)
        self.assertIn("panel test summary", out)

    def test_no_redraw_while_touching(self):
        # no actions and no new last_touch: nothing to draw however long it runs
        self.btn = _script_btn([([], None)] * 50)
        self._run()
        self.assertEqual(self.lcd.frames, [("try OK", "waiting"), ("Test done", "")])

    def test_trace_forced_off_and_events_called_each_tick(self):
        self.btn = _script_btn([([], None)] * 3)
        self._run()
        self.assertFalse(self.btn.trace)
        self.assertEqual(len(self.btn.calls), 4)   # 3 scripted + the one that raised

    def test_exit_draw_failure_does_not_mask_summary(self):
        self.btn = _script_btn([(["OK"], None)])
        def lines(a, b):
            if a == "Test done":
                raise OSError("boom")
            self.lcd.frames.append((a, b))
        self.lcd.lines = lines
        out, _ = self._run()
        self.assertIn("panel test summary", out)

    def test_sigterm_handler_installed_and_exits_cleanly(self):
        import signal
        self.btn = _script_btn([])
        old = signal.getsignal(signal.SIGTERM)
        try:
            self._run()
            handler = signal.getsignal(signal.SIGTERM)
            with self.assertRaises(SystemExit):
                handler(signal.SIGTERM, None)
        finally:
            signal.signal(signal.SIGTERM, old)

    def test_does_not_sleep_the_panel_or_idle_gpio(self):
        self.lcd.sleep = lambda: self.fail("lcd.sleep called")
        self.btn = _script_btn([])
        self._run()


if __name__ == "__main__":
    unittest.main()
