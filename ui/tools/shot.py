# -*- coding: utf-8 -*-
"""
Render the panel to PNGs so the look can be reviewed without a person sitting
in front of it - and so a theme change can be diffed as an image.

RenderTargetBitmap captures the WPF visual tree only. The Mica backdrop is
painted by DWM, not by WPF, so the capture is composited over the palette's
--bg to show what the eye actually sees rather than a transparent hole.

The window is fed a REAL hello - soak.py's engine stand-in, with the same
schema, settings, history, engine and model documents app.py would send - and
EVERY tab is rendered, not just the first. The first version of this tool
captured the Transcript tab against an empty stub, and that is how a blank
Engine tab and a transparent ON toggle survived until the first run on real
hardware (PENDING_WORK.md, step 8). A TabControl builds only the selected
tab's visuals; a screenshot that never switches tabs has looked at one fifth
of the panel.

Usage (repo root):
    .venv\\Scripts\\python.exe ui\\tools\\shot.py                 every tab, dark
    .venv\\Scripts\\python.exe ui\\tools\\shot.py --light         the light pair
    .venv\\Scripts\\python.exe ui\\tools\\shot.py --runtime DIR   a publish that is
                                                                  not ui\\runtime yet
    .venv\\Scripts\\python.exe ui\\tools\\shot.py out.png --tab 0  one tab, one file

Writes <stem>-<tab>.png next to the given name (panel-transcript.png,
panel-settings.png, ...; panel-light-*.png with --light), or exactly <out>
with --tab. --runtime exists because a running soak maps
ui\\runtime\\LiveTranscription.Ui.dll and a publish into it fails until the
soak ends: publish to a scratch directory, point this at it, review, then
publish for real.
"""
from __future__ import print_function

import argparse
import os
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import settings as settings_mod  # noqa: E402
import wpf_panel  # noqa: E402

TABS = ("transcript", "settings", "engine", "benchmark", "log")

# ApplicationBackgroundBrush of each generated theme - what DWM's Mica would
# sit behind, so the composite matches the window on a real desktop.
BG = {"dark": "#0B1013", "light": "#ECEFF1"}


def _use_runtime(path):
    """Load the panel assembly from somewhere other than ui/runtime."""
    path = os.path.abspath(path)
    wpf_panel.RUNTIME_DIR = path
    wpf_panel.UI_DLL = os.path.join(path, "LiveTranscription.Ui.dll")
    wpf_panel.UI_RUNTIMECONFIG = os.path.join(
        path, "LiveTranscription.Ui.runtimeconfig.json")


def _hello(app):
    """The opening document app.py would send, from the soak's stand-in."""
    import soak

    return {
        "version": "0.1.0-shot",
        "schema": settings_mod.schema_json(),
        "settings": dict(app.settings),
        "status": app.status(),
        "devices": {"loopback": [{"id": 0, "name": "Speakers (shot)"}],
                    "input": [{"id": 1, "name": "Microphone (shot)"}],
                    "errors": []},
        "models": app._list_models(),
        "presets": ["meeting", "lecture"],
        "history": [soak._entry(n) for n in range(14)],
        "engine": app._engine_status(),
        "busy": "",
    }


def main():
    parser = argparse.ArgumentParser(description="Render the panel to PNGs.")
    parser.add_argument("out", nargs="?", default=os.path.join(REPO, "panel.png"),
                        help="output name; per-tab files take its stem")
    parser.add_argument("--light", action="store_true",
                        help="render the light theme pair")
    parser.add_argument("--runtime", metavar="DIR",
                        help="panel assembly directory other than ui/runtime")
    parser.add_argument("--tab", type=int, choices=range(len(TABS)),
                        help="render this one tab to <out> exactly")
    parser.add_argument("--settle", type=float, default=1.5,
                        help="seconds to let bindings settle after a tab "
                             "switch (default 1.5)")
    args = parser.parse_args()

    if args.runtime:
        _use_runtime(args.runtime)
        print("using runtime: {0}".format(wpf_panel.RUNTIME_DIR))

    import soak

    app = soak._SoakApp()
    panel = wpf_panel.Panel(app, dark=not args.light)
    panel.start()
    panel.hello(_hello(app))
    panel.status("listening - server backend, xRT 0.42x")
    panel.running(True, paused=False)
    panel.event("state", app.status())
    panel.event("perf", {"infer_s": 1.7, "window_s": 4.0, "xrt": 0.42,
                         "slide_s": 2, "backend": "server",
                         "sustainable": True, "pending_s": 1.0})
    panel.event("engine", app._engine_status())
    for i in range(6):
        panel.event("log", {"level": ("info", "warn")[i % 2],
                            "msg": "shot line {0}".format(i)})
    panel.event("meter", {"level": 0.041, "threshold": 0.01, "pending_s": 1.2,
                          "queue": 1, "gated": "", "speech_frames": 22,
                          "min_frames": 8, "vad": True})
    # After the state event, not before: a healthy state hides the run
    # banner (MainVm.RefreshRunBanner), and the banner chrome is worth
    # having in the captures.
    panel.banner("Warning", "The capture device is not running",
                 "Recording-device capture needs the 'sounddevice' package: "
                 "pip install sounddevice.")
    time.sleep(2.0)

    import System.Windows
    from System import Action
    from System.IO import FileAccess, FileMode, FileStream
    from System.Windows import Rect, Size
    from System.Windows.Media import (ColorConverter, DrawingVisual,
                                      PixelFormats, SolidColorBrush,
                                      VisualBrush)
    from System.Windows.Media.Imaging import (BitmapFrame, PngBitmapEncoder,
                                              RenderTargetBitmap)
    from System.Windows.Threading import DispatcherPriority

    bg = BG["light" if args.light else "dark"]
    errors = []

    def on_ui(fn, timeout=60):
        """Run fn on the WPF thread and wait. Background priority, the same
        PanelHost.Post* uses, so it queues BEHIND the hello and the events
        rather than jumping them."""
        done = threading.Event()

        def wrapped():
            try:
                fn()
            except Exception:  # noqa: BLE001
                errors.append(traceback.format_exc())
            finally:
                done.set()

        System.Windows.Application.Current.Dispatcher.BeginInvoke(
            DispatcherPriority.Background, Action(wrapped))
        if not done.wait(timeout):
            # The old version treated this as success and reported whatever
            # file a previous run had left behind.
            errors.append("the dispatcher never ran the action within "
                          "{0}s - window closed, or wedged".format(timeout))
            return False
        return True

    def window():
        a = System.Windows.Application.Current
        w = a.MainWindow
        if w is None:
            for x in a.Windows:
                w = x
                break
        return w

    def select(i):
        window().DataContext.SelectedTab = i

    def capture(path):
        w = window()
        width, height = int(w.ActualWidth), int(w.ActualHeight)
        content = w.Content
        content.Measure(Size(width, height))
        content.Arrange(Rect(0, 0, width, height))
        content.UpdateLayout()

        dv = DrawingVisual()
        dc = dv.RenderOpen()
        dc.DrawRectangle(
            SolidColorBrush(ColorConverter.ConvertFromString(bg)), None,
            Rect(0, 0, width, height))
        dc.DrawRectangle(VisualBrush(content), None,
                         Rect(0, 0, width, height))
        dc.Close()

        rtb = RenderTargetBitmap(width * 2, height * 2, 192, 192,
                                 PixelFormats.Pbgra32)
        rtb.Render(dv)
        enc = PngBitmapEncoder()
        enc.Frames.Add(BitmapFrame.Create(rtb))
        fs = FileStream(path, FileMode.Create, FileAccess.Write)
        try:
            enc.Save(fs)
        finally:
            fs.Close()
        print("wrote {0}  ({1} bytes)".format(path, os.path.getsize(path)))

    stem, ext = os.path.splitext(os.path.abspath(args.out))
    if args.tab is not None:
        plan = [(args.tab, os.path.abspath(args.out))]
    else:
        plan = [(i, "{0}-{1}{2}{3}".format(
                    stem, "light-" if args.light else "", name, ext or ".png"))
                for i, name in enumerate(TABS)]

    # The Log tab last: opening it clears the unread badge, and the badge
    # is worth having in the other captures.
    for i, path in plan:
        if not on_ui(lambda: select(i)):
            break
        time.sleep(args.settle)         # bindings, virtualization, layout
        if not on_ui(lambda: capture(path)):
            break

    panel.close()

    for text in errors:
        print(text)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
