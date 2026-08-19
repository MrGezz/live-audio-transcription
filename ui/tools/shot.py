# -*- coding: utf-8 -*-
"""
Render the panel to a PNG so the look can be reviewed without a person sitting
in front of it - and so a theme change can be diffed as an image.

RenderTargetBitmap captures the WPF visual tree only. The Mica backdrop is
painted by DWM, not by WPF, so the capture is composited over the palette's
--bg to show what the eye actually sees rather than a transparent hole.

Usage (repo root):  .venv\\Scripts\\python.exe ui\\tools\\shot.py [out.png]
"""
from __future__ import print_function

import os
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

import wpf_panel  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "panel.png")
BG = "#0B1013"


class _Stub(object):
    """Enough surface for the window to bind against."""

    class _P(object):
        def pause(self, on):
            pass

        def clear(self):
            pass

        def rebuild(self, *a):
            pass

        def apply(self, patch, remote=False):
            return dict(patch), []

        def status(self):
            return {"running": True}

        def history(self, n):
            return []

    def __init__(self):
        self.pipeline = self._P()
        self.settings = {}

    def _lifecycle(self, name, fn):
        return name

    def _pipeline_start(self):
        pass

    def _pipeline_stop(self):
        pass

    def _pipeline_restart(self):
        pass

    def _request_exit(self):
        pass

    def _engine_action(self, a, b):
        return True

    def _list_models(self):
        return {}

    def _engine_status(self):
        return {}

    def _list_presets(self):
        return []

    def _rescan_devices(self):
        pass

    def _run_benchmark(self, a):
        return True


def main():
    panel = wpf_panel.Panel(_Stub())
    panel.start()
    panel.status("listening - server backend, xRT 1.7x")
    panel.running(True, paused=False)
    panel.banner("Warning", "The capture device is not running",
                 "Recording-device capture needs the 'sounddevice' package: "
                 "pip install sounddevice.")
    time.sleep(1.2)

    from System import Action
    from System.IO import FileAccess, FileMode, FileStream
    from System.Windows import Rect, Size
    from System.Windows.Media import (ColorConverter, DrawingVisual,
                                      PixelFormats, SolidColorBrush,
                                      VisualBrush)
    from System.Windows.Media.Imaging import (BitmapFrame, PngBitmapEncoder,
                                              RenderTargetBitmap)

    host = panel._host
    win = None
    err = {}
    done = threading.Event()

    def capture():
        try:
            app = __import__("System").Windows.Application.Current
            w = app.MainWindow
            if w is None:
                for x in app.Windows:
                    w = x
                    break
            width, height = int(w.ActualWidth), int(w.ActualHeight)
            content = w.Content
            content.Measure(Size(width, height))
            content.Arrange(Rect(0, 0, width, height))
            content.UpdateLayout()

            dv = DrawingVisual()
            dc = dv.RenderOpen()
            dc.DrawRectangle(
                SolidColorBrush(ColorConverter.ConvertFromString(BG)), None,
                Rect(0, 0, width, height))
            dc.DrawRectangle(VisualBrush(content), None,
                             Rect(0, 0, width, height))
            dc.Close()

            rtb = RenderTargetBitmap(width * 2, height * 2, 192, 192,
                                     PixelFormats.Pbgra32)
            rtb.Render(dv)
            enc = PngBitmapEncoder()
            enc.Frames.Add(BitmapFrame.Create(rtb))
            fs = FileStream(OUT, FileMode.Create, FileAccess.Write)
            try:
                enc.Save(fs)
            finally:
                fs.Close()
        except Exception:  # noqa: BLE001
            err["t"] = traceback.format_exc()
        finally:
            done.set()

    import System.Windows

    System.Windows.Application.Current.Dispatcher.BeginInvoke(Action(capture))
    done.wait(60)
    panel.close()

    if err:
        print(err["t"])
        return 1
    print("wrote {0}  ({1} bytes)".format(OUT, os.path.getsize(OUT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
