# -*- coding: utf-8 -*-
"""
Soak the panel: hours of synthetic engine traffic through the real bridge,
watching for the three ways a WPF front end quietly dies - unmanaged RSS
growth, handle leaks, and a managed heap that never plateaus.

What it drives, and why these rates:
  meter        8 Hz   the real pipeline's METER_INTERVAL_SEC pace, through
                      OfferMeter's lock-and-pull path
  transcript   0.4/s  ~5,800 captions in four hours, so TranscriptVm's trim
                      and the virtualizing panel both do real work
  state        0.2/s  pill and banner churn
  perf         0.2/s  the realtime pill's other feed
  log          0.33/s error lines included, so toasts spawn and expire
  engine       0.1/s  busy flips through the whole can-execute fan-out
  ApplySettings hammered from a plain Python thread, every 150 ms, with the
                      settings echo pushed after each - HydrateSettings plus
                      Refilter across all 61 FieldVms, which is the heaviest
                      binding path the panel has

Numbers come from the CLR itself (System.Diagnostics.Process and System.GC),
so there is nothing to pip install. A 30 s sample line prints throughout; the
verdict at the end compares the last quarter of the run against the second
quarter - growth under 10% on RSS and flat handles is a pass. The managed
heap is reported but not judged: the GC is allowed to be lazy.

Run this BEFORE trusting new content-tab work, not after: if it fails, the
View layer moves behind a WebSocket client and the architecture changes.

Usage (repo root, needs the built panel + pythonnet):
    .venv\\Scripts\\python.exe ui\\tools\\soak.py              4 hours
    .venv\\Scripts\\python.exe ui\\tools\\soak.py --minutes 5  smoke run
"""
from __future__ import print_function

import argparse
import itertools
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

import settings as settings_mod  # noqa: E402
import wpf_panel  # noqa: E402

WORDS = ("the quick brown fox jumps over a lazy dog while somebody keeps "
         "talking about buffers slides gates and backends forever").split()


class _SoakApp(object):
    """The engine's surface, faked: real validation, no audio, no threads."""

    class _P(object):
        def __init__(self, outer):
            self._outer = outer

        def pause(self, on):
            pass

        def clear(self):
            pass

        def clear_history(self):
            pass

        def rebuild(self, *a):
            pass

        def apply(self, patch, remote=False):
            clean, errors = settings_mod.validate(patch,
                                                  self._outer.settings)
            changed = dict((k, v) for k, v in clean.items()
                           if self._outer.settings.get(k) != v)
            self._outer.settings.update(changed)
            return changed, errors

        def status(self):
            return self._outer.status()

        def history(self, n):
            return []

    def __init__(self):
        self.settings = dict(settings_mod.DEFAULTS)
        self.pipeline = self._P(self)
        self.overlay = None
        self._t0 = time.monotonic()

    def status(self):
        return {
            "running": True, "paused": False,
            "uptime_s": round(time.monotonic() - self._t0, 1),
            "backend": "server", "backend_name": "whisper.cpp server",
            "source": "loopback: Speakers (soak)", "source_alive": True,
            "gate": True, "strategy": "sliding_window", "saving": None,
            "error": "", "consecutive_errors": 0,
            "languages": {"en": 12},
            "stats": {"windows": 40, "captions": 38, "dropped": 2,
                      "skipped": 5, "infer_total": 30.0, "audio_total": 80.0,
                      "xrt": 0.42},
        }

    # Everything the bridge can reach has to answer with SOMETHING.
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

    def _refresh_engine(self):
        pass

    def _list_models(self):
        return {"ggml": [{"name": "ggml-base.bin", "size_mb": 148}],
                "faster_whisper": [], "dir": "_models"}

    def _engine_status(self):
        return {"url": "http://127.0.0.1:8080", "host": "127.0.0.1",
                "port": 8080, "reachable": True, "pid": 4242,
                "image": "whisper-server.exe", "ours": True,
                "launched": False, "canStart": True, "backend": "server",
                "models": self._list_models()["ggml"]}

    def _export_payload(self, fmt):
        return {"filename": "soak.txt", "format": fmt, "count": 0,
                "content": ""}

    def _list_presets(self):
        return []

    def _preset_save(self, name):
        return {"saved": name, "presets": [name]}

    def _preset_load(self, name):
        return {"error": "no presets in a soak"}

    def _preset_delete(self, name):
        return {"deleted": name, "presets": []}

    def _rescan_devices(self):
        pass

    def _run_benchmark(self, a):
        return True


def _entry(n):
    words = []
    for i in range(12 + (n % 9)):
        word = WORDS[(n + i) % len(WORDS)]
        p = ((n * 7 + i * 13) % 100) / 100.0
        words.append({"word": " " + word,
                      "probability": p if i % 4 else None})
    text = "".join(w["word"] for w in words).strip()
    return {"id": n, "text": text, "language": "en" if n % 7 else "ms",
            "translated": False, "words": words,
            "t_start": n * 2.5, "duration": 4.0, "level": 0.05,
            "backend": "server" if n % 3 else "local",
            "processing_time": 1.7, "wall": time.time(),
            "detection": {}}


# Valid alone AND against each other: the hammer applies them in a cycle, so
# any pair may be the live base the next patch is validated over.
PATCHES = [
    {"vad": False},
    {"vad": True, "vad_threshold": 0.55},
    {"buffer": 8, "slide": 3},
    {"silence_threshold": 0.02},
    {"language": "en"},
    {"buffer": 4, "slide": 2},
    {"language": "auto"},
    {"translate": True},
    {"translate": False},
    {"confidence_warn": 0.5},
    {"confidence_warn": 0.6},
]


def main():
    parser = argparse.ArgumentParser(description="Soak the desktop panel.")
    parser.add_argument("--minutes", type=float, default=240.0,
                        help="how long to run (default: the full 4 hours)")
    args = parser.parse_args()

    app = _SoakApp()
    panel = wpf_panel.Panel(app)
    panel.start()
    panel.hello({
        "version": "soak",
        "schema": settings_mod.schema_json(),
        "settings": dict(app.settings),
        "status": app.status(),
        "devices": {"loopback": [], "input": [], "errors": []},
        "models": app._list_models(),
        "presets": [],
        "history": [_entry(n) for n in range(40)],
        "engine": app._engine_status(),
        "busy": "",
    })

    from System import GC
    from System.Diagnostics import Process

    proc = Process.GetCurrentProcess()

    def sample():
        proc.Refresh()
        return (proc.WorkingSet64 / 1e6, proc.HandleCount,
                GC.GetTotalMemory(False) / 1e6)

    stop = threading.Event()

    def hammer():
        for patch in itertools.cycle(PATCHES):
            if stop.wait(0.15):
                return
            ack = panel.bridge.ApplySettings(json.dumps(patch))
            errors = json.loads(ack).get("errors")
            if errors:
                print("[soak] patch refused (should never happen): "
                      "{0} -> {1}".format(patch, errors))
            panel.event("settings", dict(app.settings))

    threading.Thread(target=hammer, name="soak-hammer",
                     daemon=True).start()

    deadline = time.monotonic() + args.minutes * 60.0
    samples = []
    n = 0
    next_report = time.monotonic() + 30.0
    print("[soak] running for {0:.0f} minutes - close the window to abort"
          .format(args.minutes))
    print("{0:>8}  {1:>9}  {2:>8}  {3:>10}".format(
        "elapsed", "RSS MB", "handles", "managed MB"))

    try:
        while time.monotonic() < deadline and panel.alive:
            now = time.monotonic()
            panel.event("meter", {
                "level": 0.02 + 0.03 * ((n % 16) / 16.0),
                "threshold": 0.01, "pending_s": (n % 40) / 10.0,
                "queue": n % 3, "gated": ("", "silence", "no-speech")[n % 3],
                "speech_frames": n % 90, "min_frames": 8, "vad": True,
            })
            if n % 20 == 0:
                panel.event("transcript", _entry(n // 20))
            if n % 40 == 0:
                panel.event("state", app.status())
                panel.event("perf", {
                    "infer_s": 1.7, "window_s": 4.0, "xrt": 0.42,
                    "slide_s": 2, "backend": "server",
                    "sustainable": (n // 40) % 5 != 4, "pending_s": 1.0,
                })
            if n % 24 == 0:
                level = ("info", "warn", "error")[(n // 24) % 3] \
                    if (n // 24) % 20 == 19 else ("info", "warn")[(n // 24) % 2]
                panel.event("log", {"level": level,
                                    "msg": "soak line {0}".format(n)})
            if n % 80 == 0:
                engine = app._engine_status()
                engine["busy"] = "restart" if (n // 80) % 10 == 9 else ""
                panel.event("engine", engine)

            if now >= next_report:
                next_report = now + 30.0
                rss, handles, managed = sample()
                samples.append((now, rss, handles, managed))
                print("{0:>7.0f}s  {1:>9.1f}  {2:>8}  {3:>10.1f}".format(
                    now - (deadline - args.minutes * 60.0),
                    rss, handles, managed))

            n += 1
            time.sleep(0.125)           # the 8 Hz beat everything hangs off
    except KeyboardInterrupt:
        print("[soak] interrupted")
    finally:
        stop.set()

    aborted = not panel.alive
    panel.close()

    if aborted:
        print("[soak] the window closed mid-run - no verdict.")
        return 1
    if len(samples) < 8:
        print("[soak] too short for a verdict ({0} samples). RSS and handle "
              "plateaus need at least 4 minutes.".format(len(samples)))
        return 0

    # Second quarter against last quarter: the first quarter is warmup - the
    # 32,000-round-trip boundary test showed RSS plateauing after round 3.
    q = len(samples) // 4
    early = samples[q:2 * q]
    late = samples[-q:]
    rss_early = max(s[1] for s in early)
    rss_late = max(s[1] for s in late)
    h_early = max(s[2] for s in early)
    h_late = max(s[2] for s in late)
    growth = (rss_late - rss_early) / max(rss_early, 1.0)

    print("[soak] RSS {0:.1f} -> {1:.1f} MB ({2:+.1%}), handles {3} -> {4}"
          .format(rss_early, rss_late, growth, h_early, h_late))
    if growth > 0.10 or h_late > h_early + 50:
        print("[soak] FAIL - that is a leak, not jitter. Do not build more "
              "UI on this; see PENDING_WORK.md's step 6 fallback.")
        return 1
    print("[soak] PASS - RSS plateaued and handles are flat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
