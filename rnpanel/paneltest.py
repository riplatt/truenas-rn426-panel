"""Pure logic for `rn426_panel.py test`, the on-panel button check: no I/O, no
clock, no device. app.test() feeds it what btn.events() returned plus the
button object's last_touch dict; it hands back the two LCD lines and the
stdout log lines.

One screen, no steps. Line 1 suggests the first checklist item not seen yet,
line 2 is the last decision in the driver's own words. Items tick off in any
order, nothing is "wrong", and the summary says what was never seen.
"""
from rnpanel.sx8635 import Sx8635Buttons, TAP_ACTIONS

COLS1, COLS2 = 13, 16   # characters that fit line 1 (16 px bold) and line 2 (13 px)
SLIDE_DETENTS = 2       # a slide is |net| >= 2 detents in ONE touch episode

# Checklist keys; the label is what the tester sees after "try ".
LABELS = {"OK": "OK", "UP": "tap UP", "DOWN": "tap DOWN", "LEFT": "tap LEFT",
          "RIGHT": "tap RIGHT", "CW": "slide CW", "CCW": "slide CCW"}
# The RN426 buttons are presses, not taps.
MSP_LABELS = {"OK": "OK", "UP": "UP", "DOWN": "DOWN", "LEFT": "LEFT", "RIGHT": "RIGHT"}
# What Msp430Buttons.events() returns for each of its buttons.
MSP_ACTION_ITEM = {"PREV": "UP", "NEXT": "DOWN", "LEFT": "LEFT", "RIGHT": "RIGHT", "OK": "OK"}

def items_for(btn):
    """The checklist, derived from the button object so it follows whatever
    layout the daemon's own code picked. Returns an ordered list of
    (key, label). Msp430Buttons: its five buttons. Sx8635Buttons: OK if the
    layout has an OK key, the four compass taps if `compass` is non-empty,
    then the two slides."""
    if not isinstance(btn, Sx8635Buttons):
        return [(k, MSP_LABELS[k]) for k in ("OK", "UP", "DOWN", "LEFT", "RIGHT")]
    items = []
    if any(action == "OK" for _, action in btn.keys):
        items.append(("OK", LABELS["OK"]))
    if btn.compass:
        for k in ("UP", "DOWN", "LEFT", "RIGHT"):
            items.append((k, LABELS[k]))
    # Physical clockwise = wheel position DECREASING = NEXT (net < 0);
    # counter-clockwise = PREV (net > 0). See _wheel_emit in sx8635.py.
    items.append(("CW", LABELS["CW"]))
    items.append(("CCW", LABELS["CCW"]))
    return items

def _fmt_dur(dur):
    """Seconds in few characters: .31s, 1.2s, 12.3s."""
    hundredths = int(round(dur * 100))
    if hundredths < 100:
        return ".%02ds" % hundredths
    return "%.1fs" % dur

class PanelTest:
    def __init__(self, items, frame_ms=None, sx=False, compass=False):
        self.items = list(items)
        self.sx = sx
        self.frame_ms = frame_ms
        self.seen = []            # item keys, in the order first seen
        self.counts = {}
        self.last_seq = 0
        self.live_net = 0         # detents emitted so far in the current episode
        self.line2 = "waiting"
        # qsm layout has no taps: say why the list is short until something happens.
        self.intro = "qsm layout" if (sx and not compass) else None

    @classmethod
    def for_button(cls, btn, frame_ms=None):
        sx = isinstance(btn, Sx8635Buttons)
        return cls(items_for(btn), frame_ms, sx, bool(sx and btn.compass))

    def _count(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1

    def _tick(self, key, log):
        if key in dict(self.items) and key not in self.seen:
            self.seen.append(key)
            log.append("seen: %s" % dict(self.items)[key])

    def line1(self):
        for key, label in self.items:
            if key not in self.seen:
                if self.intro and not self.seen and not self.counts:
                    return self.intro[:COLS1]
                return ("try " + label)[:COLS1]
        return "all seen"

    def lines(self):
        return self.line1(), self.line2[:COLS2]

    def feed(self, actions, last_touch, now):
        """One loop tick: `actions` is what btn.events() returned (possibly
        empty), `last_touch` the button object's dict (or None). Returns
        (line1, line2, log_lines). Several actions in one batch are fine."""
        log = []
        actions = list(actions)
        lt = last_touch if (last_touch and last_touch["seq"] != self.last_seq) else None
        if lt is not None:
            self.last_seq = lt["seq"]
        if self.sx:
            self._feed_sx(actions, lt, log)
        else:
            self._feed_msp(actions, log)
        if actions or lt is not None:
            self.intro = None
        return self.line1(), self.line2[:COLS2], log

    def _feed_msp(self, actions, log):
        names = []
        for a in actions:
            key = MSP_ACTION_ITEM.get(a)
            name = key or a
            names.append(name)
            self._count(name)
            log.append("button: %s" % name)
            if key:
                self._tick(key, log)
        if names:
            self.line2 = "+".join(names)

    def _feed_sx(self, actions, lt, log):
        if lt is not None and lt["outcome"] == "tap":
            # the tap's own page/activity action rides in this batch: not a scroll
            tap = TAP_ACTIONS.get(lt["name"], lt["name"])
            if tap in actions:
                actions.remove(tap)
        names = []
        for a in actions:
            if a == "NEXT" or a == "PREV":
                self.live_net += -1 if a == "NEXT" else 1
                self.line2 = "scroll %+d" % self.live_net
            else:
                names.append(a)
                self._count(a)
                log.append("button: %s" % a)
                self._tick(a, log)
                self.line2 = "+".join(names)
        if lt is not None:
            self._episode(lt, log)
            self.live_net = 0

    def _episode(self, lt, log):
        out, net = lt["outcome"], lt["net"]
        log.append("touch: landing=%s release=%s excursion=%s dur=%.2fs rotated=%d "
                   "net=%+d -> %s" % (lt["landing"], lt["release"], lt["excursion"],
                                      lt["dur"], lt["rotated"], net,
                                      "tap " + lt["name"] if out == "tap" else out))
        self._count(out)
        if out == "tap":
            self.line2 = "tap %s @%s" % (lt["name"], lt["landing"])
            self._tick(lt["name"], log)
        elif out == "scroll":
            if net <= -SLIDE_DETENTS:
                self.line2 = "CW scroll %+d" % net
                self._tick("CW", log)
            elif net >= SLIDE_DETENTS:
                self.line2 = "CCW scroll %+d" % net
                self._tick("CCW", log)
            else:
                self.line2 = "scroll %+d" % net
        elif out == "stale":
            self.line2 = "stale latch"
        else:
            self.line2 = "none exc=%s %s" % (lt["excursion"], _fmt_dur(lt["dur"]))

    def summary(self):
        """Lines to print on exit and paste into an issue."""
        keys = [k for k, _ in self.items]
        label = dict(self.items)
        seen = [label[k] for k in self.seen]
        never = [label[k] for k in keys if k not in self.seen]
        counts = ", ".join("%s %d" % (k, self.counts[k]) for k in sorted(self.counts))
        out = ["panel test summary",
               "  seen: %s" % (", ".join(seen) or "nothing"),
               "  never seen: %s" % (", ".join(never) or "nothing"),
               "  counts: %s" % (counts or "none")]
        if self.frame_ms is not None:
            out.append("  frame time: %d ms" % self.frame_ms)
        if self.sx:
            out.append("  legend: none exc=5..9 is the dead band between a tap and a "
                       "detent; landing 73-74 with release 0-1 is the 74/0 seam. "
                       "Both are expected.")
        return out
