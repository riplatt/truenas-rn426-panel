import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from rnpanel.paneltest import PanelTest, items_for, COLS1, COLS2
from rnpanel.sx8635 import TAP_ACTIONS
from fakes import FakeGpio, FakeMsp430Buttons, SX8635_LAYOUTS, _sx8635


def netgear():
    return _sx8635(keys=SX8635_LAYOUTS["netgear"]["keys"], layout="netgear")

def qsm():
    return _sx8635(keys=SX8635_LAYOUTS["qsm"]["keys"], layout="qsm")

def msp():
    return FakeMsp430Buttons(FakeGpio())

def keys(items):
    return [k for k, _ in items]


class Run(object):
    """Drives a PanelTest with canned inputs and records every produced line."""
    def __init__(self, btn):
        self.pt = PanelTest.for_button(btn, frame_ms=120)
        self.seq = 0
        self.lt = None
        self.all_lines = list(self.pt.lines())
        self.log = []
        self.now = 0.0

    def feed(self, actions=(), **ep):
        if ep:
            self.seq += 1
            d = dict(seq=self.seq, landing=50, release=50, excursion=0, dur=0.25,
                     rotated=False, scrolled=False, net=0, outcome="none", name=None)
            d.update(ep)
            self.lt = d
        self.now += 0.1
        l1, l2, log = self.pt.feed(list(actions), self.lt, self.now)
        self.all_lines += [l1, l2]
        self.log += log
        return l1, l2

    def tap(self, name, landing=50):
        return self.feed([TAP_ACTIONS[name]], outcome="tap", name=name, landing=landing,
                         release=landing)

    def slide(self, net):
        act = "NEXT" if net < 0 else "PREV"
        for _ in range(abs(net)):
            self.feed([act])
        return self.feed(outcome="scroll", scrolled=True, net=net, excursion=30)


class TestItems(unittest.TestCase):
    def test_netgear(self):
        self.assertEqual(keys(items_for(netgear())),
                         ["OK", "UP", "DOWN", "LEFT", "RIGHT", "CW", "CCW"])

    def test_qsm_short_list(self):
        self.assertEqual(keys(items_for(qsm())), ["OK", "CW", "CCW"])

    def test_msp430(self):
        self.assertEqual(keys(items_for(msp())), ["OK", "UP", "DOWN", "LEFT", "RIGHT"])

    def test_labels_as_displayed(self):
        self.assertEqual([l for _, l in items_for(netgear())],
                         ["OK", "tap UP", "tap DOWN", "tap LEFT", "tap RIGHT",
                          "slide CW", "slide CCW"])


class TestNetgearRun(unittest.TestCase):
    def test_initial_screen(self):
        r = Run(netgear())
        self.assertEqual(r.pt.lines(), ("try OK", "waiting"))

    def test_all_seen_in_order(self):
        r = Run(netgear())
        self.assertEqual(r.feed(["OK"]), ("try tap UP", "OK"))
        self.assertEqual(r.tap("UP", 50), ("try tap DOWN", "tap UP @50"))
        r.tap("DOWN", 10)
        r.tap("LEFT", 70)
        r.tap("RIGHT", 30)
        self.assertEqual(r.pt.line1(), "try slide CW")
        r.slide(-3)
        self.assertEqual(r.pt.line1(), "try slide CCW")
        self.assertEqual(r.slide(3), ("all seen", "CCW scroll +3"))
        self.assertEqual(r.pt.seen, ["OK", "UP", "DOWN", "LEFT", "RIGHT", "CW", "CCW"])

    def test_out_of_order(self):
        r = Run(netgear())
        r.slide(3)
        r.tap("RIGHT", 30)
        self.assertEqual(r.pt.line1(), "try OK")
        r.feed(["OK"])
        self.assertEqual(r.pt.line1(), "try tap UP")
        self.assertEqual(sorted(r.pt.seen), ["CCW", "OK", "RIGHT"])

    def test_slide_cw_and_ccw_from_net_sign(self):
        r = Run(netgear())
        r.slide(-2)
        self.assertIn("CW", r.pt.seen)
        self.assertNotIn("CCW", r.pt.seen)
        self.assertEqual(r.pt.lines()[1], "CW scroll -2")
        r.slide(2)
        self.assertIn("CCW", r.pt.seen)

    def test_single_detent_wander_not_counted(self):
        r = Run(netgear())
        r.slide(-1)
        r.slide(1)
        self.assertEqual(r.pt.seen, [])
        self.assertEqual(r.pt.lines()[1], "scroll +1")

    def test_live_scroll_line_per_detent(self):
        r = Run(netgear())
        self.assertEqual(r.feed(["NEXT"])[1], "scroll -1")
        self.assertEqual(r.feed(["NEXT"])[1], "scroll -2")
        self.assertEqual(r.feed(["NEXT"])[1], "scroll -3")
        r.feed(outcome="scroll", scrolled=True, net=-3)
        self.assertEqual(r.pt.lines()[1], "CW scroll -3")
        # the next episode starts its live count from zero
        self.assertEqual(r.feed(["PREV"])[1], "scroll +1")

    def test_tap_action_is_not_a_scroll(self):
        r = Run(netgear())
        r.tap("UP")
        self.assertEqual(r.pt.lines()[1], "tap UP @50")
        self.assertEqual(r.pt.counts, {"tap": 1})
        self.assertEqual(r.pt.live_net, 0)

    def test_several_actions_in_one_batch(self):
        r = Run(netgear())
        l1, l2 = r.feed(["NEXT", "NEXT", "OK"])
        self.assertIn("OK", r.pt.seen)
        self.assertEqual(r.pt.live_net, -2)
        self.assertEqual(l1, "try tap UP")

    def test_ok_alongside_tap_in_one_batch(self):
        r = Run(netgear())
        r.feed(["PREV", "OK"], outcome="tap", name="UP", landing=50)
        self.assertEqual(sorted(r.pt.seen), ["OK", "UP"])
        self.assertEqual(r.pt.live_net, 0)

    def test_none_line_shows_excursion_and_duration(self):
        r = Run(netgear())
        self.assertEqual(r.feed(outcome="none", excursion=6, dur=0.31)[1], "none exc=6 .31s")
        self.assertEqual(r.pt.seen, [])
        self.assertEqual(r.feed(outcome="none", excursion=0, dur=12.34)[1], "none exc=0 12.3s")

    def test_stale_latch_line(self):
        r = Run(netgear())
        self.assertEqual(r.feed(outcome="stale", landing=None, release=None)[1], "stale latch")
        self.assertEqual(r.pt.seen, [])

    def test_same_last_touch_is_not_reprocessed(self):
        r = Run(netgear())
        r.tap("UP")
        r.feed()
        r.feed()
        self.assertEqual(r.pt.counts, {"tap": 1})

    def test_idle_tick_changes_nothing(self):
        r = Run(netgear())
        before = r.pt.lines()
        self.assertEqual(r.feed(), before)
        self.assertEqual(r.log, [])

    def test_log_lines_carry_raw_numbers(self):
        r = Run(netgear())
        r.tap("UP")
        self.assertTrue(any(l.startswith("touch: landing=50 release=50 excursion=0")
                            and l.endswith("-> tap UP") for l in r.log))
        self.assertIn("seen: tap UP", r.log)
        r.slide(-3)
        self.assertTrue(any("net=-3 -> scroll" in l for l in r.log))


class TestQsmRun(unittest.TestCase):
    def test_intro_until_something_happens(self):
        r = Run(qsm())
        self.assertEqual(r.pt.lines()[0], "qsm layout")
        self.assertEqual(r.feed(), ("qsm layout", "waiting"))
        self.assertEqual(r.feed(["OK"])[0], "try slide CW")

    def test_short_list_completes(self):
        r = Run(qsm())
        r.feed(["OK"])
        r.slide(-4)
        self.assertEqual(r.slide(4), ("all seen", "CCW scroll +4"))

    def test_bit_pads_shown_not_checklist(self):
        r = Run(qsm())
        self.assertEqual(r.feed(["DOWN"])[1], "DOWN")
        self.assertEqual(r.feed(["RIGHT"])[1], "RIGHT")
        self.assertEqual(r.pt.seen, [])
        self.assertEqual(r.pt.counts, {"DOWN": 1, "RIGHT": 1})

    def test_no_taps_means_no_tap_items(self):
        r = Run(qsm())
        self.assertNotIn("try tap UP", r.all_lines)


class TestMsp430Run(unittest.TestCase):
    def test_all_five(self):
        r = Run(msp())
        self.assertEqual(r.pt.lines(), ("try OK", "waiting"))
        self.assertEqual(r.feed(["OK"]), ("try UP", "OK"))
        self.assertEqual(r.feed(["PREV"]), ("try DOWN", "UP"))
        self.assertEqual(r.feed(["NEXT"]), ("try LEFT", "DOWN"))
        r.feed(["LEFT"])
        self.assertEqual(r.feed(["RIGHT"]), ("all seen", "RIGHT"))

    def test_chord_in_one_batch(self):
        r = Run(msp())
        self.assertEqual(r.feed(["PREV", "OK"])[1], "UP+OK")
        self.assertEqual(sorted(r.pt.seen), ["OK", "UP"])

    def test_activity_shown_not_ticked(self):
        r = Run(msp())
        self.assertEqual(r.feed(["ACTIVITY"])[1], "ACTIVITY")
        self.assertEqual(r.pt.seen, [])


class TestColumnBudget(unittest.TestCase):
    def test_every_produced_line_fits(self):
        for make in (netgear, qsm, msp):
            r = Run(make())
            r.feed(["OK"])
            for n in ("UP", "DOWN", "LEFT", "RIGHT"):
                r.tap(n, 70)
            r.slide(-12)
            r.slide(12)
            r.slide(-1)
            r.feed(outcome="none", excursion=79, dur=123.456)
            r.feed(outcome="stale", landing=None, release=None)
            r.feed(["NEXT"] * 40)
            r.feed(["PREV", "NEXT", "LEFT", "RIGHT", "OK", "ACTIVITY"])
            lines = r.all_lines
            for i in range(0, len(lines), 2):
                self.assertLessEqual(len(lines[i]), COLS1, lines[i])
                self.assertLessEqual(len(lines[i + 1]), COLS2, lines[i + 1])
        self.assertEqual((COLS1, COLS2), (13, 16))

    def test_longest_item_prompts_fit(self):
        for make in (netgear, qsm, msp):
            for _, label in items_for(make()):
                self.assertLessEqual(len("try " + label), COLS1)


class TestSummary(unittest.TestCase):
    def test_contents(self):
        r = Run(netgear())
        r.feed(["OK"])
        r.tap("UP")
        r.slide(-3)
        r.feed(outcome="none", excursion=6, dur=0.3)
        text = "\n".join(r.pt.summary())
        self.assertIn("seen: OK, tap UP, slide CW", text)
        self.assertIn("never seen: tap DOWN, tap LEFT, tap RIGHT, slide CCW", text)
        self.assertIn("none 1", text)
        self.assertIn("scroll 1", text)
        self.assertIn("tap 1", text)
        self.assertIn("frame time: 120 ms", text)
        self.assertIn("74/0 seam", text)
        self.assertIn("dead band", text)

    def test_nothing_seen(self):
        text = "\n".join(Run(netgear()).pt.summary())
        self.assertIn("seen: nothing", text)
        self.assertIn("never seen: OK, tap UP", text)

    def test_all_seen_says_nothing_missing(self):
        r = Run(msp())
        r.feed(["OK", "PREV", "NEXT", "LEFT", "RIGHT"])
        text = "\n".join(r.pt.summary())
        self.assertIn("never seen: nothing", text)
        self.assertNotIn("seam", text)

    def test_no_frame_time_when_unknown(self):
        pt = PanelTest.for_button(msp())
        self.assertNotIn("frame time", "\n".join(pt.summary()))


if __name__ == "__main__":
    unittest.main()
