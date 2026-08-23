"""
app.py - the one program, with or without a browser attached.

Why this exists
---------------
There were two ways to run this project and they were different programs. The
console script owned its own worker loop, its own overlay and its own printing;
anything a web UI wanted would have been a second copy of all three, drifting
away from the first the moment either was touched.

So there is one App. It owns a Pipeline (the engine), optionally a Tk overlay,
and optionally a WebSocket server. The console output, the overlay and the
browser are three subscribers to the same event stream, and the browser is the
only one that can also talk back.

  live_transcription.py            -> App(settings).run()      console + overlay
  live_transcription.py --web      -> App(settings).run()      the above, plus a
                                                               control panel
The difference between them is one boolean.

Thread layout, because it is load-bearing:
  main thread     Tk overlay mainloop (Tk cannot live anywhere else), or an
                  idle wait when the overlay is off
  capture thread  owned by the audio source
  worker thread   owned by the pipeline: gate, chunk, transcribe, emit
  server thread   the asyncio loop behind the WebSocket server
Events cross those boundaries as plain dicts. Nothing but the main thread ever
touches Tk, and nothing but the worker ever rebuilds a component.
"""

import glob
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import audio_sources
import settings as settings_mod
from pipeline import Pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
WEBUI_DIR = os.path.join(HERE, "webui")
PRESET_DIR = os.path.join(HERE, "presets")

# Shipped profiles live in a SUBDIRECTORY of the user's preset folder, and the
# nesting is doing two jobs. .gitignore ignores "presets/*.json" because saved
# presets are personal setups - but a gitignore * never crosses a /, so files
# one level down are tracked with no negation rule to maintain. And it makes
# "shipped" structural instead of a blessed-name list in code: Save always
# writes to PRESET_DIR, so saving over a built-in's name SHADOWS it rather than
# destroying it, and deleting that shadow brings the built-in back.
BUILTIN_PRESET_DIR = os.path.join(PRESET_DIR, "builtin")
MODEL_DIR = os.path.join(HERE, "_models")
SERVER_CMD = os.path.join(HERE, "start_whisper_server.cmd")

VERSION = "2.0"

# The only image name this panel will ever kill. See _engine_stop: the port
# says which PID, and this says whether that PID is allowed to be it.
SERVER_IMAGE_PREFIX = "whisper"

# Whether whisper-server is up is not something this process is told - it is
# another program, in another window, and it can die at any moment. So it is
# polled.
ENGINE_POLL_SEC = 5.0

# Console lines that would otherwise arrive several times a second. The browser
# gets everything; a terminal does not want a level meter.
CONSOLE_KINDS = ("log", "transcript", "state", "benchmark")


class App(object):
    def __init__(self, settings, quiet=False):
        self.settings = dict(settings)
        self.quiet = quiet
        self.pipeline = Pipeline(self.settings, emit=self._on_event)
        self.overlay = None
        self.server = None
        self.panel = None
        self._server_proc = None
        self._engine_poll_started = False
        self._stop = threading.Event()
        self._last_overlay_style = None
        self._last_panel_theme = None
        # Enumerating capture devices costs 585 ms the first time on this
        # machine (PortAudio walks every host API), and the websocket
        # callbacks run ON the event-loop thread - so doing it inside
        # _ws_open would stall every other socket, the keepalive pings and
        # any browser audio arriving, for over half a second, every time a
        # tab connected. Held here, primed before the listener starts, and
        # refreshed off the loop.
        self._devices = {"loopback": [], "input": [], "errors": []}
        # Same reasoning for the engine: netstat, tasklist and an HTTP probe
        # are all blocking, so the panel reads a cache that a poller refreshes.
        self._engine = {}
        self._busy = ""              # the lifecycle command in flight, by name
        self._busy_lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------
    def run(self):
        self._print_banner()
        try:
            self.pipeline.start()
        except Exception as e:
            raise SystemExit("Could not start: {0}".format(e))

        if self.settings["web"]:
            self._start_server()
        if self.settings["wpf"]:
            self._start_wpf()
        if self._has_front_end():
            self._start_engine_poll()

        try:
            self._main_loop()
        except KeyboardInterrupt:
            print("\nStopping transcription...")
        finally:
            self.shutdown()

    def _main_loop(self):
        """
        Stay alive, and own Tk while doing it.

        Two jobs, and they are really one. Tk may only be created and pumped on
        the thread the process started on, so this is the only place an overlay
        can be built or torn down; everything else - the worker, the capture
        thread, the websocket loop - can only ask, by moving the `overlay`
        setting.

        The loop around overlay.run() is the point of the rewrite. run() IS the
        Tk mainloop, and it returns whenever the window goes away - and the
        panel unticking the overlay is one of the ways it goes away. That used
        to return straight into shutdown(), so turning off the overlay from the
        browser ended the whole program, took the panel down with it, and left
        every button after that pressing on a closed socket. Now the window
        closing is just an event: the loop decides again, and only the two
        conditions below end anything.

        The same is true of a stopped pipeline. Pressing Stop looks exactly
        like a dead worker, and so does the gap in the middle of a Restart -
        exiting there would remove the only thing that can start it again. With
        no panel attached nothing has changed: a dead worker still ends the
        program.
        """
        while not self._stop.is_set():
            if self.overlay is not None:
                self.overlay.run()          # blocks until the window closes
                self.overlay = None
                self._last_overlay_style = None
                continue                    # decide again - do not exit

            # Checked before the overlay is rebuilt, so a watchdog close that
            # happened *because* there is nothing left to caption ends the
            # program rather than opening a fresh window for it to close again.
            if self._finished():
                break
            if not self.pipeline.alive() and not self._has_front_end():
                break

            if self.settings["overlay"]:
                self._start_overlay()
                if self.overlay is None:
                    # It cannot be opened - no display, or a Tk that will not
                    # start. Say so once by turning the setting off, rather than
                    # retrying every quarter second forever; the panel's
                    # checkbox then shows what is actually true.
                    self.pipeline.apply({"overlay": False})
                continue

            time.sleep(0.25)

    def _finished(self):
        """
        Should the program end on its own?

        Only when a WAV file has been transcribed to the end - a capture device
        never finishes, so this stays False and Ctrl+C remains the way out. The
        panel is the exception: leaving the process up means the transcript is
        still there to read and export, which is usually the reason a file was
        transcribed in the first place.
        """
        if not self.pipeline.drained():
            return False
        if self._has_front_end():
            return False
        return True

    def _has_front_end(self):
        """
        Is anything attached that can receive events and give orders?

        This is the question every one of the old `self.server is not None`
        guards was really asking. The WPF panel counts exactly like a
        browser: while either is up, a stopped pipeline is undoable and a
        finished file is still there to read and export - and with neither,
        pressing Stop from a front end that then vanished must not leave a
        deaf process running forever.
        """
        panel = self.panel
        return self.server is not None \
            or (panel is not None and panel.alive)

    def _notify(self, kind, data):
        """
        Fan one event out to every attached front end.

        The subscriber list PENDING_WORK.md promised, in its simplest form:
        two subscribers, both optional. This is deliberately NOT guarded by
        _has_front_end - each front end checks itself, so a panel that died
        mid-session costs nothing and breaks nothing.
        """
        if self.server is not None:
            self.server.broadcast({"type": kind, "data": data})
        panel = self.panel
        if panel is not None and panel.alive:
            panel.event(kind, data)

    def shutdown(self):
        self._stop.set()
        if self.overlay is not None:
            try:
                self.overlay.close()
            except Exception:
                pass
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                pass
        if self.panel is not None:
            try:
                # The joining close, and this is the one place it is safe:
                # shutdown runs on the main thread, never on the panel's
                # dispatcher. A message loop still draining while the
                # interpreter finalizes is a crash on the way out.
                self.panel.close()
            except Exception:
                pass
        self.pipeline.stop()
        print("Stream closed.")

    def _print_banner(self):
        s = self.settings
        print("Live audio transcription {0}".format(VERSION))
        print("  capture   {0}{1}".format(
            s["capture"],
            " ({0})".format(s["file_path"]) if s["capture"] == "file" else ""))
        print("  chunking  {0} buffer {1}s slide {2}s".format(
            s["strategy"], s["buffer"], s["slide"]))
        print("  language  {0}{1}".format(
            s["language"], " -> English" if s["translate"] else ""))

    # -- overlay ----------------------------------------------------------
    def _start_overlay(self):
        try:
            from overlay import Overlay, OverlayUnavailable
        except ImportError as e:
            print("[overlay] unavailable ({0}) - running console-only.".format(e))
            return
        try:
            self.overlay = Overlay(self.settings, on_move=self._on_overlay_move)
            self.overlay.start()
            # Two ways this ends: the worker thread died, or a WAV file was
            # transcribed to its last sample. Either way an overlay left
            # showing a stale caption over everything else is the wrong answer.
            self.overlay.set_watchdog(
                self._overlay_should_stay,
                "Transcription stopped - see the console")
            self._last_overlay_style = self._overlay_style()
        except OverlayUnavailable as e:
            self.overlay = None
            print("[overlay] {0} - running console-only.".format(e))
        except RuntimeError as e:
            self.overlay = None
            print("[overlay] {0}".format(e))

    def _overlay_should_stay(self):
        """
        Is there still a reason to keep the overlay on screen?

        The watchdog closes it two seconds after this first returns False, and
        for a dead worker that is right: a stale caption floating over
        everything with nothing behind it is worse than no overlay at all.
        A stop you asked for is a different thing. With the panel up there is
        something that can undo it, so the window stays and the app keeps
        running; the transcript on screen is the last thing that was said, not
        a lie about the present.
        """
        if self._finished():
            return False
        if self.pipeline.alive():
            return True
        return self._has_front_end() and not self._stop.is_set()

    def _overlay_style(self):
        return dict((f.key, self.settings[f.key]) for f in settings_mod.SCHEMA
                    if f.group == "overlay")

    def _on_overlay_move(self, x_pixels, y_pct):
        """Dragging the overlay is a settings change like any other."""
        self.pipeline.apply({"overlay_y_pct": round(y_pct, 3)})

    def _sync_overlay(self):
        """
        Apply the overlay settings. Runs on whichever thread emitted them.

        Deliberately cannot build a window. This is called from the pipeline's
        worker thread, and creating Tk anywhere but the main thread is the
        single most common way one of these programs dies - with "main thread
        is not in main loop", raised from somewhere unrelated to the actual
        call. So turning the overlay ON only records the setting and _main_loop
        opens it. close() and configure() are safe from anywhere: overlay.py
        queues them for its own pump.
        """
        style = self._overlay_style()
        if style == self._last_overlay_style:
            return
        self._last_overlay_style = style
        if self.overlay is None:
            return                      # _main_loop opens one if it is wanted
        if not style["overlay"]:
            self.overlay.close()        # _main_loop sees run() return
            return
        self.overlay.configure(self.settings)

    def _panel_dark(self):
        return self.settings.get("wpf_theme", "dark") != "light"

    def _sync_panel_theme(self):
        """
        Re-theme the desktop panel when wpf_theme changes. Any thread.

        The panel's _sync_overlay, and the reason the theme is applied from
        HERE rather than read by the panel: C# must not know a settings key
        by name (CONTRIBUTING invariant 13 - the pane is generated, and
        `model` is the one sanctioned exception), so the window is told
        "dark" or "light" and nothing else. Asynchronous, through
        PanelHost.PostTheme, like every other push: this runs on whichever
        thread emitted the settings event - the dispatcher itself when the
        edit came from the panel - and a blocking call back into the window
        from there is the one deadlock the bridge rules exist to prevent.
        """
        theme = self.settings.get("wpf_theme", "dark")
        if theme == self._last_panel_theme:
            return
        self._last_panel_theme = theme
        panel = self.panel
        if panel is not None and panel.alive:
            panel.theme(theme != "light")

    # -- events -----------------------------------------------------------
    def _on_event(self, kind, data):
        self._notify(kind, data)
        if kind == "settings":
            self.settings.update(data)
            self._sync_overlay()
            self._sync_panel_theme()
        if kind == "transcript":
            if self.overlay is not None:
                self.overlay.show(data["text"])
            self._print_caption(data)
            return
        if self.quiet or kind not in CONSOLE_KINDS:
            return
        if kind == "log":
            prefix = {"error": "[error] ", "warn": "[warn] "}.get(
                data.get("level"), "")
            print(prefix + data.get("msg", ""))
        elif kind == "state" and data.get("error"):
            print("[state] {0}".format(data["error"]))

    def _print_caption(self, entry):
        # Same shape the console has always printed: [ms->EN] for a translation,
        # [ms->MS] otherwise. Codes on both sides deliberately - see
        # whisper_backends.language_code for why the label must not change when
        # auto falls back from the server to the CPU mid-session.
        code = entry.get("language") or "??"
        target = "EN" if entry.get("translated") else code.upper()
        print("[{0}→{1}] {2}".format(code, target, entry["text"]))

    # -- front ends -------------------------------------------------------
    def _rescan_devices(self):
        """Refresh the device cache. Never on the event-loop thread."""
        try:
            self._devices = audio_sources.list_devices()
        except Exception as e:
            self.pipeline.log("warn", "Could not list devices: {0}".format(e))
            return
        self._notify("devices", self._devices)

    def _start_server(self):
        from wsserver import WSServer
        s = self.settings
        # Both primed before anything can connect, so the first tab does not
        # pay for either inside a callback that must not block.
        self._rescan_devices()
        self._engine = self._engine_status()
        self.server = WSServer(
            host=s["web_host"], port=s["web_port"], static_dir=WEBUI_DIR,
            token=s["web_token"], on_open=self._ws_open,
            on_message=self._ws_message, on_close=self._ws_close,
            on_log=lambda level, msg: print("[web] {0}".format(msg))
            if level in ("warn", "error") else None)
        try:
            self.server.start()
        except OSError as e:
            self.server = None
            print("[web] could not listen on {0}:{1} ({2}). The control panel "
                  "is off; transcription carries on.".format(
                      s["web_host"], s["web_port"], e))
            return
        print("Control panel: {0}".format(self.server.url))
        if s["web_host"] not in ("127.0.0.1", "localhost") and not s["web_token"]:
            print("[web] WARNING: listening on {0} with no access token. "
                  "Anyone on this network can read the transcript and change "
                  "settings.".format(s["web_host"]))
        if s["web_open"]:
            threading.Timer(0.4, webbrowser.open, (self.server.url,)).start()

    def _start_wpf(self):
        """
        Open the desktop panel, or say once why not and carry on.

        --wpf failing means "no desktop panel", never "no transcription": a
        missing .NET runtime or pythonnet on someone else's machine must not
        be the difference between the program working and not. wpf_panel puts
        an actionable sentence in PanelUnavailable for exactly this print.
        """
        try:
            import wpf_panel
        except ImportError as e:
            print("[wpf] unavailable ({0}) - running without the desktop "
                  "panel.".format(e))
            return
        # The panel reads both caches in its opening document, so prime them
        # the way _start_server does - unless the web server already did.
        if not self._devices["loopback"] and not self._devices["input"]:
            self._rescan_devices()
        if not self._engine:
            self._engine = self._engine_status()
        try:
            panel = wpf_panel.Panel(self, dark=self._panel_dark())
            panel.start()
        except wpf_panel.PanelUnavailable as e:
            print("[wpf] {0}".format(e))
            print("[wpf] The desktop panel is off; transcription carries on.")
            return
        except Exception as e:  # noqa: BLE001 - degrade, never die, see above
            print("[wpf] The desktop panel could not start ({0}: {1}); "
                  "transcription carries on.".format(type(e).__name__, e))
            return
        self.panel = panel
        # The window opened in this theme; _sync_panel_theme moves it from
        # here when the setting changes.
        self._last_panel_theme = self.settings.get("wpf_theme", "dark")
        panel.hello(self._hello_payload())
        print("Desktop panel: open.")

    def _start_engine_poll(self):
        """The whisper-server poller, once, however many front ends exist."""
        if self._engine_poll_started:
            return
        self._engine_poll_started = True
        threading.Thread(target=self._engine_poll, name="engine-poll",
                         daemon=True).start()

    def _hello_payload(self):
        """The opening document every front end hydrates from, wire or not."""
        return {
            "version": VERSION,
            "schema": settings_mod.schema_json(),
            "settings": dict(self.settings),
            "status": self.pipeline.status(),
            "devices": self._devices,
            "models": self._list_models(),
            "presets": self._list_presets(),
            "history": self.pipeline.history(200),
            "engine": dict(self._engine),
            "busy": self._busy,
        }

    def _ws_open(self, client):
        client.send({"type": "hello", "data": self._hello_payload()})

    def _ws_close(self, client, reason):
        pass

    def _ws_message(self, client, data):
        if isinstance(data, (bytes, bytearray)):
            self.pipeline.feed(bytes(data))
            return
        try:
            msg = json.loads(data)
        except ValueError:
            client.send({"type": "error", "data": {"msg": "malformed JSON"}})
            return
        kind = msg.get("type")
        if kind == "config":
            changed, errors = self.pipeline.apply(msg.get("data") or {},
                                                  remote=True)
            client.send({"type": "ack", "data": {"changed": changed,
                                                 "errors": errors}})
        elif kind == "command":
            self._command(client, msg.get("name", ""), msg.get("args") or {})
        elif kind == "hello":
            client.role = str(msg.get("role", "control"))[:32]

    # -- commands ---------------------------------------------------------
    def _command(self, client, name, args):
        def ok(payload=None):
            client.send({"type": "ack", "data": dict({"command": name},
                                                     **(payload or {}))})

        # start / stop / restart are the slow ones: loading a CPU model takes
        # seconds and stop() joins the worker with a six-second timeout, and all
        # of this runs ON the asyncio thread. Handed to _lifecycle, which runs
        # them off the loop and refuses to run two at once.
        if name == "start":
            ok({"queued": self._lifecycle("start", self._pipeline_start)})
        elif name == "stop":
            ok({"queued": self._lifecycle("stop", self._pipeline_stop)})
        elif name == "restart":
            ok({"queued": self._lifecycle("restart", self._pipeline_restart)})
        elif name == "rebuild":
            ok({"queued": self._lifecycle(
                "rebuild", lambda: self.pipeline.rebuild(
                    *(args.get("what") or ["backend"])))})
        elif name == "pause":
            self.pipeline.pause(True)
            ok()
        elif name == "resume":
            self.pipeline.pause(False)
            ok()
        elif name == "clear":
            self.pipeline.clear_history()
            if self.overlay is not None:
                self.overlay.clear()
            ok()
        elif name == "devices":
            # Off the event loop, then broadcast: see the note on _devices.
            threading.Thread(target=self._rescan_devices, name="devices",
                             daemon=True).start()
            ok()
        elif name == "models":
            client.send({"type": "models", "data": self._list_models()})
        elif name == "history":
            client.send({"type": "history", "data": self.pipeline.history(
                int(args.get("limit", 500)))})
        elif name == "status":
            client.send({"type": "state", "data": self.pipeline.status()})
        elif name == "benchmark":
            self._run_benchmark(args)
            ok()
        elif name == "export":
            client.send({"type": "export", "data": self._export(args)})
        elif name == "preset_save":
            ok(self._preset_save(args.get("name", "")))
        elif name == "preset_load":
            ok(self._preset_load(args.get("name", "")))
        elif name == "preset_delete":
            ok(self._preset_delete(args.get("name", "")))
        elif name == "whisper_server":
            action = str(args.get("action", "status"))
            if action == "status":
                # Cheap enough not to need the single slot, and a poll that
                # queued behind a running Restart would report stale numbers
                # for the whole of it.
                threading.Thread(target=self._refresh_engine, name="engine",
                                 daemon=True).start()
                ok({"queued": "status"})
            else:
                ok({"queued": self._lifecycle(
                    "engine " + action,
                    lambda: self._engine_action(action, args))})
        elif name == "shutdown":
            ok()
            threading.Timer(0.3, self._request_exit).start()
        else:
            client.send({"type": "error",
                         "data": {"msg": "unknown command '{0}'".format(name)}})

    def _request_exit(self):
        self._stop.set()
        if self.overlay is not None:
            self.overlay.close()
        panel = self.panel
        if panel is not None:
            # request_close, never close: the Exit button lands here ON the
            # panel's own dispatcher thread, and close() joins that thread -
            # which from itself is a deadlock for the whole timeout. The
            # joining close belongs to shutdown(), on the main thread.
            panel.request_close()

    def _run_benchmark(self, args):
        buffers = args.get("buffers") or [4, 8, 16, 24]
        wav = args.get("wav") or None
        reps = int(args.get("reps", 2))
        threading.Thread(
            target=self.pipeline.benchmark,
            kwargs={"buffers": buffers, "wav": wav, "reps": reps},
            name="benchmark", daemon=True).start()

    # -- transcript export ------------------------------------------------
    def _export(self, args):
        return self._export_payload(args.get("format", "txt"))

    def _export_payload(self, fmt):
        """
        Render the session so far in any format, without having been saving.

        Deciding at the end that you wanted subtitles used to mean you did not
        have them. The captions carry their word timings either way, so this is
        just a re-render of what is already in memory.

        Shared by both front ends: the browser gets it as an `export` message
        and downloads it, the desktop panel calls it over the bridge and puts
        the content behind a save dialog. Same payload either way -
        {filename, format, count, content}.
        """
        import transcript as tr
        if fmt not in tr.EXTENSIONS:
            fmt = "txt"
        entries = self.pipeline.history(0)
        lines, cue = [], 0
        if fmt == "vtt":
            lines.append("WEBVTT\n")
        for entry in entries:
            if fmt == "txt":
                lines.append(entry["text"])
            elif fmt == "jsonl":
                lines.append(json.dumps(entry, ensure_ascii=False))
            else:
                comma = fmt == "srt"
                start, end = tr.TranscriptWriter._span(entry)
                cue += 1
                head = "{0}\n".format(cue) if comma else ""
                lines.append("{0}{1} --> {2}\n{3}\n".format(
                    head, tr._clock(start, comma), tr._clock(end, comma),
                    entry["text"]))
        return {"filename": tr.default_path(fmt), "format": fmt,
                "count": len(entries), "content": "\n".join(lines) + "\n"}

    # -- presets ----------------------------------------------------------
    @staticmethod
    def _safe_preset_name(name):
        """
        A file name, not a path.

        The control panel can be served to a network, so a preset name is
        untrusted input; without this, "..\\..\\Windows\\System32\\x" would be a
        write primitive.
        """
        keep = "-_. "
        cleaned = "".join(c for c in str(name) if c.isalnum() or c in keep)
        return cleaned.strip().strip(".")[:64]

    def _list_presets(self):
        """Every preset name, shipped and personal, each appearing once.

        A user preset shadows a built-in of the same name, so the name is
        listed once either way and `_preset_file` decides which file it means.
        """
        names = set()
        for directory in (BUILTIN_PRESET_DIR, PRESET_DIR):
            try:
                for path in glob.glob(os.path.join(directory, "*.json")):
                    names.add(os.path.splitext(os.path.basename(path))[0])
            except OSError:
                continue
        return sorted(names)

    @staticmethod
    def _preset_file(safe):
        """The file a preset name refers to, or None.

        The user's own copy wins; the shipped one is the fallback. Saving over
        a built-in's name therefore overrides it without touching the tracked
        file, and deleting that copy restores the original.
        """
        if not safe:
            return None
        user = os.path.join(PRESET_DIR, safe + ".json")
        if os.path.exists(user):
            return user
        builtin = os.path.join(BUILTIN_PRESET_DIR, safe + ".json")
        return builtin if os.path.exists(builtin) else None

    def _preset_save(self, name):
        safe = self._safe_preset_name(name)
        if not safe:
            return {"error": "A preset needs a name.",
                    "presets": self._list_presets()}
        settings_mod.save_preset(os.path.join(PRESET_DIR, safe + ".json"),
                                 self.settings)
        self._broadcast_presets()
        return {"saved": safe, "presets": self._list_presets()}

    def _preset_load(self, name):
        safe = self._safe_preset_name(name)
        path = self._preset_file(safe)
        if path is None:
            return {"error": "No preset called '{0}'.".format(name),
                    "presets": self._list_presets()}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            return {"error": "Could not read preset: {0}".format(e),
                    "presets": self._list_presets()}
        data.pop("_comment", None)
        changed, errors = self.pipeline.apply(data, remote=True)
        # The list rides on EVERY return, including the error shapes. The
        # desktop panel refills its dropdown from this ack - SettingsVm's own
        # comment names the "presets" key as the contract - so a return
        # without it emptied the preset list on the first load, which is
        # exactly the gesture the shipped profiles exist for.
        return {"loaded": safe, "changed": changed, "errors": errors,
                "presets": self._list_presets()}

    def _preset_delete(self, name):
        safe = self._safe_preset_name(name)
        user = os.path.join(PRESET_DIR, safe + ".json") if safe else ""
        if user and os.path.exists(user):
            try:
                os.remove(user)
            except OSError as e:
                return {"error": str(e), "presets": self._list_presets()}
        elif self._preset_file(safe):
            # Only your own copy is deletable. The remaining file is shipped
            # with the checkout, so removing it would make the name disappear
            # until the next git restore - and deleting a built-in you had
            # saved over is how you get the original back, which only works
            # if this call stops here.
            return {"error": "'{0}' is a built-in profile. Deleting a saved "
                             "copy restores it; the original stays."
                             .format(safe),
                    "presets": self._list_presets()}
        self._broadcast_presets()
        return {"deleted": safe, "presets": self._list_presets()}

    def _broadcast_presets(self):
        self._notify("presets", self._list_presets())

    # -- lifecycle commands ------------------------------------------------
    ENGINE_STOP_WAIT_SEC = 8.0
    ENGINE_START_WAIT_SEC = 45.0

    def _lifecycle(self, name, fn):
        """
        Run one slow lifecycle command off the event loop, one at a time.

        Two reasons for the single slot. The obvious one is that Restart is
        stop() followed by start(), and a second Restart arriving in the middle
        would have its stop() race the first one's start() - and the button is
        easy to press twice when nothing has visibly happened yet. The other is
        that these take seconds, so without it a panel open in three tabs could
        have three model loads running at once.

        Returns the name if it was accepted, "" if something else is running.
        """
        with self._busy_lock:
            if self._busy:
                self.pipeline.log("warn", "'{0}' is still running - ignoring "
                                          "'{1}'.".format(self._busy, name))
                return ""
            self._busy = name
        self._broadcast_engine()

        def body():
            try:
                fn()
            except Exception as e:
                self.pipeline.log("error", "{0} failed: {1}".format(name, e))
            finally:
                with self._busy_lock:
                    self._busy = ""
                try:
                    self._refresh_engine()
                except Exception:
                    pass
                self.pipeline.push_state()

        threading.Thread(target=body, name=name.replace(" ", "-"),
                         daemon=True).start()
        return name

    def _pipeline_start(self):
        self.pipeline.start()
        if self.overlay is not None:
            self.overlay.clear()

    def _pipeline_stop(self):
        self.pipeline.stop()
        if self.overlay is not None:
            # Not close(): the watchdog decides that, and with the panel up a
            # stop is undoable. This only stops the last caption sitting there
            # looking live.
            self.overlay.show("— transcription stopped —")

    def _pipeline_restart(self):
        self.pipeline.restart()
        if self.overlay is not None:
            self.overlay.clear()

    # -- whisper-server control -------------------------------------------
    def _list_models(self):
        """
        What is actually sitting in _models, split by which backend eats it.

        The UI needs this to offer a model switch: when the perf line says the
        GPU cannot hold pace, the useful next move is a smaller GGML file, and
        the only way to know which ones exist is to look.
        """
        ggml, faster = [], []
        try:
            for path in sorted(glob.glob(os.path.join(MODEL_DIR, "*.bin"))):
                ggml.append({"name": os.path.basename(path),
                             "size_mb": round(os.path.getsize(path) / 1e6)})
            for path in sorted(glob.glob(os.path.join(MODEL_DIR, "*"))):
                if os.path.isdir(path) and (
                        os.path.exists(os.path.join(path, "model.bin"))
                        or os.path.exists(os.path.join(path, "config.json"))):
                    faster.append({"name": os.path.basename(path),
                                   "path": os.path.relpath(path, HERE)})
        except OSError:
            pass
        return {"ggml": ggml, "faster_whisper": faster,
                "dir": os.path.relpath(MODEL_DIR, HERE)}

    def _server_endpoint(self):
        """(host, port) of the whisper-server this session is configured for."""
        url = self.settings.get("server_url") or "http://127.0.0.1:8080"
        if "://" not in url:
            url = "http://" + url
        parts = urllib.parse.urlsplit(url)
        return parts.hostname or "127.0.0.1", parts.port or 80

    def _server_reachable(self, timeout=1.5):
        """Is anything answering HTTP there? Blocking - never on the loop."""
        url = (self.settings.get("server_url") or "").rstrip("/")
        if not url:
            return False
        for path in ("/health", "/"):
            try:
                urllib.request.urlopen(url + path, timeout=timeout).read(64)
                return True
            except urllib.error.HTTPError:
                # It answered, and that is the whole question. /health only
                # exists in later whisper.cpp builds, and a 404 from an older
                # one still means the process is up - the same reasoning as
                # ServerBackend.ping.
                return True
            except Exception:
                continue
        return False

    def _server_pid(self):
        """
        PID of whatever holds the configured port, or None.

        By port, never by image name. Killing by name would also kill a second
        server somebody is running for something else, and this panel can be
        open on a machine that is not the one in front of you. The port is what
        the session actually depends on.
        """
        if os.name != "nt":
            return None
        _host, port = self._server_endpoint()
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, timeout=10).stdout
        except Exception:
            return None
        listening, connected = None, None
        for line in out.decode("utf-8", "replace").splitlines():
            bits = line.split()
            if len(bits) < 5 or bits[1].rsplit(":", 1)[-1] != str(port):
                continue
            try:
                pid = int(bits[-1])
            except ValueError:
                continue
            # PID 0 is the kernel holding a socket that no process owns any
            # more - a TIME_WAIT left by the server that was just killed. It
            # lingers for seconds and it is not something that can be stopped,
            # so treating it as "the port is taken" made Restart kill the
            # server and then refuse to start it again.
            if pid == 0:
                continue
            # The state is read by shape rather than by spelling: LISTENING is
            # localised on some Windows installs, but a listener's foreign
            # address is 0.0.0.0:0 or [::]:0 in every locale. Rows for accepted
            # connections carry the same owning PID anyway, so they are a
            # fallback rather than a wrong answer.
            if bits[2].rsplit(":", 1)[-1] == "0":
                listening = pid
            elif connected is None:
                connected = pid
        return listening if listening is not None else connected

    @staticmethod
    def _image_name(pid):
        """The .exe behind a PID, or "". Used to refuse to kill the wrong one."""
        if not pid or os.name != "nt":
            return ""
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq {0}".format(pid), "/NH", "/FO",
                 "CSV"], capture_output=True, timeout=10).stdout
        except Exception:
            return ""
        rows = out.decode("utf-8", "replace").strip().splitlines()
        # A miss prints "INFO: No tasks are running which match ...", which is
        # localised; the leading quote of a CSV row is not, so that is the test.
        if not rows or not rows[0].startswith('"'):
            return ""
        return rows[0].split('","')[0].strip('"')

    def _engine_status(self):
        """Everything the panel needs about the GPU server. Blocking."""
        pid = self._server_pid()
        host, port = self._server_endpoint()
        image = self._image_name(pid)
        return {
            "url": self.settings.get("server_url"),
            "host": host,
            "port": port,
            "reachable": self._server_reachable(),
            "pid": pid,
            "image": image,
            "ours": bool(image) and image.lower().startswith(
                SERVER_IMAGE_PREFIX),
            "launched": self._server_proc is not None,
            "canStart": os.path.exists(SERVER_CMD),
            "backend": self.settings.get("backend"),
            "models": self._list_models()["ggml"],
        }

    def _refresh_engine(self):
        self._engine = self._engine_status()
        self._broadcast_engine()

    def _broadcast_engine(self, extra=None):
        # No early-out on "no server": with the WPF panel as the only front
        # end this is the one channel that makes a busy transition observable
        # at all - _lifecycle announces the slot through here, and a panel
        # that never hears it never re-enables its buttons.
        data = dict(self._engine)
        data["busy"] = self._busy
        if extra:
            data.update(extra)
        self._notify("engine", data)

    def _engine_poll(self):
        """
        Keep the panel honest about a process this one does not own.

        Only broadcasts on a change, so an idle panel is not carrying a message
        every five seconds forever, and skips entirely while a lifecycle
        command runs - that command reports its own result, and a poll landing
        mid-restart would contradict it.
        """
        while not self._stop.wait(ENGINE_POLL_SEC):
            if self._busy or not self._has_front_end():
                continue
            try:
                fresh = self._engine_status()
            except Exception:
                continue
            was, self._engine = self._engine, fresh
            # "models" is in here because dropping a .bin into _models is a
            # thing you do WHILE the panel is open - you go find a smaller
            # model precisely because the perf line said the current one cannot
            # hold pace. Without it the dropdown stayed stale until something
            # unrelated changed, and the file you just downloaded looked like
            # it had not arrived. Re-broadcasting is cheap: fillEngineModels
            # keeps a signature of the list and returns early when it matches,
            # so an unchanged list costs one string compare in the browser.
            if any(was.get(k) != fresh.get(k)
                   for k in ("reachable", "pid", "image", "url", "canStart",
                             "models")):
                self._broadcast_engine()
                if fresh["reachable"] and not was.get("reachable"):
                    self._backend_may_be_back()

    def _backend_may_be_back(self):
        """
        The server just came up. Reconnect, if nothing else is going to.

        AutoBackend re-probes on its own schedule and needs no help here. The
        pinned server backend does: create_backend checked once at build time,
        failed, and left Pipeline.backend as None with nothing that would ever
        look again. That is the state in which restarting the engine changes
        nothing at all and the panel looks broken.
        """
        if self.settings.get("backend") == "server" \
                and self.pipeline.backend is None:
            self.pipeline.log("info", "whisper-server is up - reconnecting the "
                                      "transcription backend.")
            self.pipeline.rebuild("backend")

    def _engine_action(self, action, args):
        if action == "stop":
            self._engine_stop()
        elif action == "start":
            self._engine_start(args)
        elif action == "restart":
            if self._server_pid() is not None:
                self._engine_stop()
            self._engine_start(args)
        else:
            self.pipeline.log("warn",
                              "Unknown engine action '{0}'.".format(action))

    def _engine_stop(self):
        """
        Stop whisper-server - including one this panel did not start.

        A stored Popen handle is not the answer on its own, and it used to be
        the only one. The launcher line is: cmd /c start "" cmd /k
        start_whisper_server.cmd - and that outer cmd exits the instant it has
        spawned the window, so the handle is dead within milliseconds while the
        server it started runs for hours. "This panel did not start a server"
        was therefore the reply even when it had. The port is the thing that is
        actually true, and it is equally true for a server started from the
        launcher, from another panel, or by hand.
        """
        pid = self._server_pid()
        host, port = self._server_endpoint()
        if pid is None:
            self._server_proc = None
            self.pipeline.log("warn", "Nothing is listening on {0}:{1}.".format(
                host, port))
            return
        image = self._image_name(pid)
        if not image.lower().startswith(SERVER_IMAGE_PREFIX):
            self.pipeline.log(
                "error", "Port {0} is held by PID {1}, which is {2} - not a "
                         "whisper server. Refusing to kill it. Either point "
                         "server_url somewhere else, or close that program "
                         "yourself.".format(
                             port, pid,
                             image or "a process this account cannot identify"))
            return
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        except Exception as e:
            self.pipeline.log("error", "Could not stop PID {0}: {1}".format(
                pid, e))
            return
        self._server_proc = None
        # Only the server is killed, not the console window around it: the last
        # lines of that log are the only place the reason for a crash is
        # written down, and the window sits at its own pause holding them.
        deadline = time.monotonic() + self.ENGINE_STOP_WAIT_SEC
        while time.monotonic() < deadline and self._server_pid() is not None:
            time.sleep(0.25)
        self.pipeline.log("info", "{0} (PID {1}) stopped; port {2} is free."
                          .format(image, pid, port))

    def _engine_start(self, args):
        """
        Start the GPU server, then wait until it actually answers.

        Deliberately narrow about what a browser can pass through: the only
        argument is the BASENAME of a .bin that already exists in _models, and
        it is checked against the real listing rather than trusted. The panel
        can be reachable from the network, and "run this batch file with these
        arguments" is not something a browser gets to say freely.
        """
        if not os.path.exists(SERVER_CMD):
            self.pipeline.log("error", "{0} is not next to app.py.".format(
                os.path.basename(SERVER_CMD)))
            return
        # The browser-supplied argument is checked first, before anything about
        # the state of the machine. Both can be wrong at once, and "that model
        # does not exist" is the more useful of the two answers - it is the one
        # about something the caller can fix.
        model = os.path.basename(str(args.get("model", "")))
        known = [m["name"] for m in self._list_models()["ggml"]]
        if model and model not in known:
            self.pipeline.log("error",
                              "'{0}' is not a model in _models.".format(model))
            return
        host, port = self._server_endpoint()
        if host not in ("127.0.0.1", "localhost", "::1"):
            self.pipeline.log("error", "server_url points at {0}, which is not "
                                       "this machine - nothing here can start "
                                       "it.".format(host))
            return
        pid = self._server_pid()
        if pid is not None:
            self.pipeline.log("warn", "Port {0} is already held by PID {1} "
                                      "({2}). Use Restart to replace it."
                              .format(port, pid,
                                      self._image_name(pid) or "unknown"))
            return
        # Relative, with cwd set to the project - never an absolute path. The
        # .cmd resolves _whisper.cpp and _models relative to itself, and
        # keeping every path here relative is what lets the folder be moved,
        # cloned or put on a different drive with nothing to edit.
        #
        # The ".\\" is load-bearing and was the bug: cmd does not resolve a
        # BARE batch name from the working directory, so `cmd /c
        # start_whisper_server.cmd` answers "is not recognized as an internal
        # or external command" - into a console nobody sees, which is why
        # pressing Start did nothing at all and reported nothing.
        #
        # CREATE_NEW_CONSOLE rather than the old `cmd /c start "" cmd /k`. That
        # form needed an interactive window station to work and gave back a
        # handle to the launcher, which exited within milliseconds of spawning
        # the window - so the handle said "dead" while the server ran for
        # hours. This gives it its own window AND a handle whose lifetime is
        # the server's.
        cmd = ["cmd", "/c", os.path.join(".", os.path.basename(SERVER_CMD))]
        if model:
            cmd.append(model)
        try:
            self._server_proc = subprocess.Popen(
                cmd, cwd=HERE,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except OSError as e:
            self.pipeline.log("error", "Could not start it: {0}".format(e))
            return
        self.pipeline.log("info", "whisper-server starting with {0} - it loads "
                                  "in its own window.".format(
                                      model or "the default model"))
        deadline = time.monotonic() + self.ENGINE_START_WAIT_SEC
        while time.monotonic() < deadline:
            proc = self._server_proc
            if proc is not None and proc.poll() is not None:
                # The .cmd ends in `pause`, so it only returns once its window
                # has been closed or it never opened one. Either way there is
                # nothing left to wait for.
                self.pipeline.log(
                    "error", "The launcher exited with code {0} before the "
                             "server answered. Its window has the reason - if "
                             "none opened, run {1} yourself to see it.".format(
                                 proc.returncode,
                                 os.path.basename(SERVER_CMD)))
                self._server_proc = None
                return
            if self._server_reachable(timeout=1.0):
                self.pipeline.log("info", "whisper-server is answering on "
                                          "{0}.".format(
                                              self.settings.get("server_url")))
                self._refresh_engine()
                self._backend_may_be_back()
                return
            time.sleep(0.5)
        self.pipeline.log(
            "warn", "whisper-server has not answered within {0:.0f}s. A large "
                    "model can take longer than that to load - watch its "
                    "window, and press Reconnect once it says it is listening."
                    .format(self.ENGINE_START_WAIT_SEC))


def main(argv=None):
    parser = settings_mod.build_parser(
        "Live system audio transcription with an overlay and an optional "
        "browser control panel.")
    resolved, args = settings_mod.from_args(parser, argv)
    if args.list_devices:
        audio_sources.print_devices()
        return 0
    App(resolved).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
