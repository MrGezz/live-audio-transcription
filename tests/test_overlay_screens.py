"""
The overlay stays on the monitor it was dragged to.

Tk's winfo_screenwidth/height are the PRIMARY monitor on Windows, and the
overlay used to clamp every placement to them - so a strip dragged onto a
second screen was pulled back by the next caption, and the vertical position
it recorded was a fraction of the wrong screen. These drive Overlay._place and
Overlay._release with a stand-in root, so they need no display and no Tk: the
arithmetic is the whole question, and the monitor lookup is a module function
that a test can answer for a layout of its choosing.

    .venv\\Scripts\\python.exe -m unittest tests.test_overlay_screens -v
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import overlay as overlay_mod  # noqa: E402
from overlay import Overlay  # noqa: E402

PRIMARY = (0, 0, 1920, 1080)
RIGHT = (1920, 0, 1920, 1080)          # a second monitor to the right
ABOVE = (0, -1080, 1920, 1080)         # a second monitor above


def _layout(*monitors):
    """A monitor_rect for these rectangles: the one containing the point."""
    def lookup(x, y):
        for left, top, w, h in monitors:
            if left <= x < left + w and top <= y < top + h:
                return (left, top, w, h)
        return None
    return lookup


class _Root(object):
    """Enough of a Tk root for _place and _release: geometry is recorded."""

    def __init__(self, x=0, y=0, width=800, height=100):
        self.x, self.y, self.width, self.height = x, y, width, height
        self.placed = []

    def winfo_screenwidth(self):
        return PRIMARY[2]

    def winfo_screenheight(self):
        return PRIMARY[3]

    def winfo_x(self):
        return self.x

    def winfo_y(self):
        return self.y

    def winfo_width(self):
        return self.width

    def winfo_height(self):
        return self.height

    def update_idletasks(self):
        pass

    def geometry(self, spec):
        self.placed.append(spec)


class _Label(object):
    def configure(self, **kw):
        pass

    def winfo_reqheight(self):
        return 60


class _Font(object):
    def metrics(self, name):
        return 30


def _overlay(**settings):
    ov = Overlay(settings)
    ov._root = _Root()
    ov._label = _Label()
    ov._font = _Font()
    return ov


def _xy(spec):
    """(x, y) out of a Tk geometry string, signs included."""
    size, rest = spec.split("+", 1)
    x, y = rest.replace("+-", "+~").split("+")
    return int(x.replace("~", "-")), int(y.replace("~", "-"))


class Placement(unittest.TestCase):

    def test_before_any_drag_the_strip_is_centred_on_the_primary_screen(self):
        ov = _overlay(overlay_width_pct=50, overlay_y_pct=0.85)
        with mock.patch.object(overlay_mod, "monitor_rect", _layout(PRIMARY, RIGHT)):
            ov._place()
        x, y = _xy(ov._root.placed[-1])
        self.assertEqual(x, (1920 - 960) // 2)
        self.assertEqual(y, int(1080 * 0.85))

    def test_a_strip_left_on_a_second_monitor_stays_there(self):
        # The regression: x = 2500 is past the primary's right edge, and the
        # old clamp pulled it back to 1920 - width on the next caption.
        ov = _overlay(overlay_width_pct=50, overlay_y_pct=0.85)
        ov._x = 2500
        ov._anchor = (2500 + 480, 950)
        with mock.patch.object(overlay_mod, "monitor_rect", _layout(PRIMARY, RIGHT)):
            ov._place()
        x, y = _xy(ov._root.placed[-1])
        self.assertEqual(x, 2500)
        self.assertEqual(y, int(1080 * 0.85))

    def test_a_monitor_above_the_primary_means_a_negative_y(self):
        ov = _overlay(overlay_width_pct=50, overlay_y_pct=0.85)
        ov._x = 480
        ov._anchor = (960, -500)
        with mock.patch.object(overlay_mod, "monitor_rect", _layout(PRIMARY, ABOVE)):
            ov._place()
        x, y = _xy(ov._root.placed[-1])
        self.assertEqual(x, 480)
        self.assertEqual(y, -1080 + int(1080 * 0.85))

    def test_the_strip_is_still_kept_inside_its_own_monitor(self):
        # The clamp is not gone, it is measured against the right screen: a
        # caption tall enough to push the strip past the bottom pulls it up
        # to that monitor's bottom edge, not the primary's.
        ov = _overlay(overlay_width_pct=50, overlay_y_pct=1.0)
        ov._x = 1920 + 1920 - 200          # further right than the width allows
        ov._anchor = (3800, 1000)
        with mock.patch.object(overlay_mod, "monitor_rect", _layout(PRIMARY, RIGHT)):
            ov._place()
        x, y = _xy(ov._root.placed[-1])
        self.assertEqual(x, 1920 + 1920 - 960)
        self.assertEqual(y, 1080 - (2 * 30 + 2 * overlay_mod.PAD_PX))

    def test_without_a_monitor_answer_the_primary_screen_is_the_fallback(self):
        # Not Windows, or the call failed: the old behaviour, deliberately.
        ov = _overlay(overlay_width_pct=50, overlay_y_pct=0.85)
        ov._x = 2500
        ov._anchor = (2980, 950)
        with mock.patch.object(overlay_mod, "monitor_rect", lambda x, y: None):
            ov._place()
        x, _y = _xy(ov._root.placed[-1])
        self.assertEqual(x, 1920 - 960)


class Release(unittest.TestCase):

    def test_letting_go_on_a_second_monitor_records_that_monitors_fraction(self):
        moves = []
        ov = _overlay(overlay_width_pct=50, overlay_y_pct=0.85)
        ov._on_move = lambda x, y_pct: moves.append((x, y_pct))
        ov._root = _Root(x=2500, y=900, width=960, height=100)
        ov._drag_from = (5, 5)
        ov._pressed_at = (0, 0)
        with mock.patch.object(overlay_mod, "monitor_rect", _layout(PRIMARY, RIGHT)):
            ov._release(None)
        self.assertEqual(ov._anchor, (2500 + 480, 950))
        self.assertEqual(ov._settings["overlay_y_pct"], round(900 / 1080.0, 3))
        self.assertEqual(moves, [(2500, round(900 / 1080.0, 3))])

    def test_letting_go_above_the_primary_is_not_a_negative_fraction(self):
        ov = _overlay(overlay_width_pct=50, overlay_y_pct=0.85)
        ov._root = _Root(x=480, y=-300, width=960, height=100)
        ov._drag_from = (5, 5)
        ov._pressed_at = (0, 0)
        with mock.patch.object(overlay_mod, "monitor_rect", _layout(PRIMARY, ABOVE)):
            ov._release(None)
        self.assertEqual(ov._settings["overlay_y_pct"],
                         round((-300 + 1080) / 1080.0, 3))

    def test_a_click_without_a_move_records_nothing(self):
        moves = []
        ov = _overlay()
        ov._on_move = lambda x, y_pct: moves.append((x, y_pct))
        ov._root = _Root(x=2500, y=900)
        ov._drag_from = (5, 5)
        ov._pressed_at = (2500, 900)
        ov._release(None)
        self.assertIsNone(ov._anchor)
        self.assertEqual(moves, [])


class TheMonitorLookup(unittest.TestCase):

    @unittest.skipUnless(sys.platform == "win32", "Win32 monitor calls")
    def test_the_primary_monitor_contains_the_origin(self):
        rect = overlay_mod.monitor_rect(0, 0)
        self.assertIsNotNone(rect)
        left, top, w, h = rect
        self.assertLessEqual(left, 0)
        self.assertLessEqual(top, 0)
        self.assertGreater(w, 0)
        self.assertGreater(h, 0)

    @unittest.skipIf(sys.platform == "win32", "the fallback is for elsewhere")
    def test_elsewhere_it_answers_none(self):
        self.assertIsNone(overlay_mod.monitor_rect(0, 0))


if __name__ == "__main__":
    unittest.main()
