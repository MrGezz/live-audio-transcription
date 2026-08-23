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
  C# edits       2/s  FieldVm setters on the dispatcher, so SettingsVm's
                      debounce -> Flush -> IEngineBridge.ApplySettings runs:
                      the CLR -> Python direction of the bridge, which the
                      hammer above never crosses (it calls the Python object)
  tab cycle    /30 s  every tab gets selected in turn. A TabControl builds
                      only the selected tab's visuals, so without this the
                      settings churn above reaches no bindings at all

Numbers come from the CLR itself (System.Diagnostics.Process and System.GC),
so there is nothing to pip install. A 30 s sample line prints throughout; the
verdict at the end compares the last quarter of the run against the second
quarter - growth under 10% on PRIVATE BYTES and flat handles is a pass. The
managed heap is reported but not judged: the GC is allowed to be lazy.

How long to run, and why 90 minutes rather than the four hours this file
used to default to. Two different leak classes need two different arguments:

  Per-EVENT leaks - a handler never unhooked, a container never released, a
  string interned per caption. These scale with the number of events, not
  with the clock, and the event rates above are fixed. 90 minutes drives
  ~43,000 meter frames, ~36,000 hammer round trips, ~10,800 C#-side edits
  and 180 tab switches. The 2026-08-23 run bounded this class hard: 13,939
  ApplySettings round trips - each rehydrating all 61 FieldVms - moved the
  handle count by 8, so under 0.001 handles and a few hundred bytes per
  event. Four hours multiplies the evidence by 2.7 against a bound already
  three orders of magnitude below anything that matters.

  Time-based drift - a timer queue, heap fragmentation, something that grows
  with the clock whatever the traffic. Here duration IS the sensitivity, and
  it is worth being explicit about the exchange rate. The verdict compares
  two windows whose centres sit T/2 apart, and the threshold is 10% of a
  ~275 MB baseline, so the smallest detectable drift is about 54/T MB per
  hour with T in hours: 13.5 MB/h at four hours, 36 MB/h at 90 minutes. A
  panel open for an eight-hour workday grows ~290 MB at the 90-minute floor
  and would be caught; the extra 2.5 hours only buys the band between that
  and ~110 MB a day, which is drift no one would notice.

So 90 minutes is the gate. Pass --minutes 240 before a release if you want
the finer floor, and --minutes 5 as a smoke test. Note the run is only valid
if nothing rewrites ui/runtime while it runs: the assembly is mapped and
paged in lazily, so replacing the DLL mid-run silently mixes two builds.

Run this BEFORE trusting new content-tab work, not after: if it fails, the
View layer moves behind a WebSocket client and the architecture changes.

Usage (repo root, needs the built panel + pythonnet):
    .venv\\Scripts\\python.exe ui\\tools\\soak.py                90 minutes
    .venv\\Scripts\\python.exe ui\\tools\\soak.py --minutes 240  release gate
    .venv\\Scripts\\python.exe ui\\tools\\soak.py --minutes 5    smoke run
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
            who = ("hammer" if threading.current_thread().name == "soak-hammer"
                   else "dispatcher")
            self._outer.apply_calls[who] += 1
            clean, errors = settings_mod.validate(patch,
                                                  self._outer.settings)
            changed = dict((k, v) for k, v in clean.items()
                           if self._outer.settings.get(k) != v)
            self._outer.settings.update(changed)
            # What app.py does after every patch: broadcast the settings
            # document, which is what drives HydrateSettings + Refilter in
            # the panel. Posted, never invoked - when the C# side is the
            # caller this runs ON the dispatcher thread.
            panel = self._outer.panel
            if panel is not None:
                panel.event("settings", dict(self._outer.settings))
            return changed, errors

        def status(self):
            return self._outer.status()

        def history(self, n):
            return []

    def __init__(self):
        self.settings = dict(settings_mod.DEFAULTS)
        self.pipeline = self._P(self)
        self.overlay = None
        self.panel = None               # set once the window is up
        self.tick = 0                   # the main loop's 8 Hz counter
        # ApplySettings arrivals by direction. The hammer thread is Python
        # calling Python; anything else is the WPF thread coming in through
        # pythonnet - the CLR -> Python direction, which is the one that
        # can only be proven exercised by counting it.
        self.apply_calls = {"hammer": 0, "dispatcher": 0}
        self._t0 = time.monotonic()

    def status(self):
        # Varies with the tick so a state event actually moves something: a
        # constant document short-circuits every Set() and the pill and
        # banner code never runs. Once a minute the capture "dies" for one
        # event, which is the run banner's capture-dead state.
        k = self.tick // 40             # one step per state event
        return {
            "running": True, "paused": k % 30 == 29,
            "uptime_s": round(time.monotonic() - self._t0, 1),
            "backend": "server", "backend_name": "whisper.cpp server",
            "source": "loopback: Speakers (soak)",
            "source_alive": k % 12 != 11,
            "gate": k % 2 == 0, "strategy": "sliding_window", "saving": None,
            "error": "", "consecutive_errors": 3 if k % 20 == 19 else 0,
            "languages": {"en": 12 + k % 5, "ms": 1 + k % 3},
            "stats": {"windows": 40 + k, "captions": 38 + k, "dropped": k % 7,
                      "skipped": 5, "infer_total": 30.0 + k,
                      "audio_total": 80.0 + 2 * k,
                      "xrt": 0.42 + 0.05 * (k % 4)},
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
        # "model" mirrors app.py's real document: it is what the Engine tab
        # opens its dropdown on. Named from the stand-in's own list rather
        # than from settings, so the shot.py screenshots that end up in the
        # README show a real selection instead of the empty "(the launcher's
        # default)" entry, which is what this looked like when the key was
        # missing entirely.
        return {"url": "http://127.0.0.1:8080", "host": "127.0.0.1",
                "port": 8080, "reachable": True, "pid": 4242,
                "image": "whisper-server.exe", "ours": True,
                "launched": False, "canStart": True, "backend": "server",
                "model": self._list_models()["ggml"][0]["name"],
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
    parser.add_argument("--minutes", type=float, default=90.0,
                        help="how long to run (default 90; the smallest "
                             "detectable drift is about 54/T MB per hour "
                             "with T in hours - see the module docstring)")
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
        # PRIVATE bytes is what the verdict judges - not the working set.
        # The working set is the OS's decision about how much of the commit
        # stays resident, so it moves with machine-wide memory pressure that
        # has nothing to do with this process. Measured here: 213 MB resident
        # against 375 MB private, and the 2026-08-23 run watched the working
        # set fall 293 -> 170 MB across one 30 s sample while allocation was
        # flat. A trim landing in the LAST quarter subtracts from exactly the
        # number the verdict watches, which turns a leak into a PASS - the
        # one direction of error a leak soak must not have. Private bytes is
        # what the process asked for and no one else can give back.
        proc.Refresh()
        return (proc.PrivateMemorySize64 / 1e6, proc.WorkingSet64 / 1e6,
                proc.HandleCount, GC.GetTotalMemory(False) / 1e6)

    app.panel = panel                   # apply() echoes through it now

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

    threading.Thread(target=hammer, name="soak-hammer",
                     daemon=True).start()

    # ---- the other direction of the bridge, and the other four tabs ------
    # panel.bridge is the Python object, so the hammer above is Python calling
    # Python. What the panel actually does on a user edit is FieldVm.Commit ->
    # Edited -> SettingsVm's 250 ms debounce -> Flush ->
    # IEngineBridge.ApplySettings, which pythonnet dispatches INTO the Python
    # bridge on the dispatcher thread and marshals the ack back out. That
    # reverse call - the GIL it takes, the strings it copies, ApplyAck's
    # per-field re-hydrate - is only reachable from the C# side, so drive it
    # from there: FieldVm setters, posted to the dispatcher, spaced wider than
    # the debounce so every edit really flushes.
    import System.Windows
    from System import Action
    from System.Windows.Threading import DispatcherPriority

    def on_ui(fn):
        """Queue fn on the WPF thread. Asynchronous: never blocks here.

        Background priority, the same PanelHost.Post* uses, so these queue
        BEHIND the hello and the events rather than jumping them - at the
        default priority the first edit ran before the schema had loaded.
        """
        System.Windows.Application.Current.Dispatcher.BeginInvoke(
            DispatcherPriority.Background, Action(fn))

    def main_vm():
        return System.Windows.Application.Current.MainWindow.DataContext

    def edit(key, prop, value):
        def f(s):
            field = s.Field(key)
            if field is None:
                raise KeyError(key)
            setattr(field, prop, value)
        return f

    EDITS = [
        [edit("vad", "BoolValue", False)],
        [edit("vad", "BoolValue", True)],
        # buffer+slide land in ONE flush: same dispatcher action, one debounce
        [edit("buffer", "NumberValue", 8.0), edit("slide", "NumberValue", 3.0)],
        [edit("confidence_warn", "NumberValue", 0.5)],
        [edit("buffer", "NumberValue", 4.0), edit("slide", "NumberValue", 2.0)],
        [edit("translate", "BoolValue", True)],
        [edit("translate", "BoolValue", False)],
        [edit("confidence_warn", "NumberValue", 0.6)],
    ]
    edit_failures = []

    def edit_from_csharp(i):
        def f():
            try:
                pane = main_vm().SettingsPane
                if not pane.IsLoaded:       # no schema yet: nothing to edit
                    return
                for step in EDITS[i % len(EDITS)]:
                    step(pane)
            except Exception as exc:  # noqa: BLE001
                if not edit_failures:
                    print("[soak] C#-side edit failed: {0!r}".format(exc))
                edit_failures.append(exc)
        on_ui(f)

    def select_tab(i):
        # A TabControl realises only the selected tab's visuals. Cycling means
        # every tab's templates, item containers and bindings exist for the
        # whole run, and the detach/reattach of a tab switch happens 480
        # times - a realistic thing to leak on.
        on_ui(lambda: setattr(main_vm(), "SelectedTab", i % 5))

    deadline = time.monotonic() + args.minutes * 60.0
    samples = []
    n = 0
    next_report = time.monotonic() + 30.0
    print("[soak] running for {0:.0f} minutes - close the window to abort"
          .format(args.minutes))
    print("{0:>8}  {1:>10}  {2:>9}  {3:>8}  {4:>10}".format(
        "elapsed", "private MB", "RSS MB", "handles", "managed MB"))

    interrupted = False
    try:
        while time.monotonic() < deadline and panel.alive:
            now = time.monotonic()
            app.tick = n
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
            if n % 4 == 0:              # 2 Hz: wider than the 250 ms debounce
                edit_from_csharp(n // 4)
            if n % 240 == 0:            # every 30 s
                select_tab(n // 240)

            if now >= next_report:
                next_report = now + 30.0
                private, rss, handles, managed = sample()
                samples.append((now, private, rss, handles, managed))
                print("{0:>7.0f}s  {1:>10.1f}  {2:>9.1f}  {3:>8}  "
                      "{4:>10.1f}".format(
                          now - (deadline - args.minutes * 60.0),
                          private, rss, handles, managed))

            n += 1
            time.sleep(0.125)           # the 8 Hz beat everything hangs off
    except KeyboardInterrupt:
        print("[soak] interrupted")
        interrupted = True
    finally:
        stop.set()

    aborted = not panel.alive
    panel.close()

    calls = app.apply_calls
    print("[soak] ApplySettings arrivals: {0} from the Python hammer, {1} from "
          "C# on the dispatcher (the CLR -> Python direction)".format(
              calls["hammer"], calls["dispatcher"]))
    if edit_failures:
        print("[soak] {0} C#-side edits failed; see the first error above."
              .format(len(edit_failures)))
    if not calls["dispatcher"] and len(samples) >= 8:
        # The harness did not do what it claims. A verdict from that run
        # would be a PASS for a path that never ran, so refuse to give one.
        print("[soak] the CLR -> Python path was never exercised - no "
              "verdict. Check the field keys in EDITS against the schema.")
        return 2

    # Exit codes: 0 judged and passed, 1 judged and failed, 2 not judged. A
    # run that measured nothing must not share PASS's exit code.
    if aborted or interrupted:
        print("[soak] {0} mid-run - no verdict.".format(
            "the window closed" if aborted else "interrupted"))
        return 2
    if len(samples) < 8:
        # Samples land at 30 s, 60 s, ... so the 8th needs 240 s of run PLUS
        # the loop's own lag; 4 minutes flat yields 7.
        print("[soak] too short for a verdict ({0} samples). RSS and handle "
              "plateaus need at least 4.5 minutes.".format(len(samples)))
        return 2

    # Second quarter against last quarter: the first quarter is warmup - the
    # 32,000-round-trip boundary test showed RSS plateauing after round 3.
    q = len(samples) // 4
    early = samples[q:2 * q]
    late = samples[-q:]
    mem_early = max(s[1] for s in early)
    mem_late = max(s[1] for s in late)
    h_early = max(s[3] for s in early)
    h_late = max(s[3] for s in late)
    growth = (mem_late - mem_early) / max(mem_early, 1.0)

    print("[soak] private {0:.1f} -> {1:.1f} MB ({2:+.1%}), handles {3} -> {4}"
          .format(mem_early, mem_late, growth, h_early, h_late))
    if growth > 0.10 or h_late > h_early + 50:
        print("[soak] FAIL - that is a leak, not jitter. Do not build more "
              "UI on this; see PENDING_WORK.md's step 6 fallback.")
        return 1
    print("[soak] PASS - private bytes plateaued and handles are flat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
