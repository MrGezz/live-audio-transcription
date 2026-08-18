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
import webbrowser

import audio_sources
import settings as settings_mod
from pipeline import Pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
WEBUI_DIR = os.path.join(HERE, "webui")
PRESET_DIR = os.path.join(HERE, "presets")
MODEL_DIR = os.path.join(HERE, "_models")
SERVER_CMD = os.path.join(HERE, "start_whisper_server.cmd")

VERSION = "2.0"

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
        self._server_proc = None
        self._stop = threading.Event()
        self._last_overlay_style = None
        # Enumerating capture devices costs 585 ms the first time on this
        # machine (PortAudio walks every host API), and the websocket
        # callbacks run ON the event-loop thread - so doing it inside
        # _ws_open would stall every other socket, the keepalive pings and
        # any browser audio arriving, for over half a second, every time a
        # tab connected. Held here, primed before the listener starts, and
        # refreshed off the loop.
        self._devices = {"loopback": [], "input": [], "errors": []}

    # -- lifecycle --------------------------------------------------------
    def run(self):
        self._print_banner()
        try:
            self.pipeline.start()
        except Exception as e:
            raise SystemExit("Could not start: {0}".format(e))

        if self.settings["web"]:
            self._start_server()
        if self.settings["overlay"]:
            self._start_overlay()

        try:
            if self.overlay is not None:
                self.overlay.run()
            else:
                while self.pipeline.alive() and not self._stop.is_set():
                    if self._finished():
                        break
                    time.sleep(0.25)
        except KeyboardInterrupt:
            print("\nStopping transcription...")
        finally:
            self.shutdown()

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
        if self.server is not None:
            return False
        return True

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
                lambda: self.pipeline.alive() and not self._finished(),
                "Transcription stopped - see the console")
            self._last_overlay_style = self._overlay_style()
        except OverlayUnavailable as e:
            self.overlay = None
            print("[overlay] {0} - running console-only.".format(e))
        except RuntimeError as e:
            self.overlay = None
            print("[overlay] {0}".format(e))

    def _overlay_style(self):
        return dict((f.key, self.settings[f.key]) for f in settings_mod.SCHEMA
                    if f.group == "overlay")

    def _on_overlay_move(self, x_pixels, y_pct):
        """Dragging the overlay is a settings change like any other."""
        self.pipeline.apply({"overlay_y_pct": round(y_pct, 3)})

    def _sync_overlay(self):
        """The pipeline does not own the overlay, so the app applies its keys."""
        style = self._overlay_style()
        if style == self._last_overlay_style:
            return
        self._last_overlay_style = style
        if self.overlay is None:
            if style["overlay"]:
                self._start_overlay()
            return
        if not style["overlay"]:
            self.overlay.close()
            self.overlay = None
            return
        self.overlay.configure(self.settings)

    # -- events -----------------------------------------------------------
    def _on_event(self, kind, data):
        if self.server is not None:
            self.server.broadcast({"type": kind, "data": data})
        if kind == "settings":
            self.settings.update(data)
            self._sync_overlay()
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

    # -- web server -------------------------------------------------------
    def _rescan_devices(self):
        """Refresh the device cache. Never on the event-loop thread."""
        try:
            self._devices = audio_sources.list_devices()
        except Exception as e:
            self.pipeline.log("warn", "Could not list devices: {0}".format(e))
            return
        if self.server is not None:
            self.server.broadcast({"type": "devices", "data": self._devices})

    def _start_server(self):
        from wsserver import WSServer
        s = self.settings
        # Primed before anything can connect, so the first tab does not pay
        # for it inside a callback that must not block.
        self._rescan_devices()
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

    def _ws_open(self, client):
        client.send({"type": "hello", "data": {
            "version": VERSION,
            "schema": settings_mod.schema_json(),
            "settings": dict(self.settings),
            "status": self.pipeline.status(),
            "devices": self._devices,
            "models": self._list_models(),
            "presets": self._list_presets(),
            "history": self.pipeline.history(200),
            "serverBat": os.path.exists(SERVER_CMD),
        }})

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

        if name == "start":
            self.pipeline.start()
            ok()
        elif name == "stop":
            self.pipeline.stop()
            ok()
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
            ok(self._whisper_server(args))
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
        """
        Render the session so far in any format, without having been saving.

        Deciding at the end that you wanted subtitles used to mean you did not
        have them. The captions carry their word timings either way, so this is
        just a re-render of what is already in memory.
        """
        import transcript as tr
        fmt = args.get("format", "txt")
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
        try:
            return sorted(os.path.splitext(os.path.basename(p))[0]
                          for p in glob.glob(os.path.join(PRESET_DIR, "*.json")))
        except OSError:
            return []

    def _preset_save(self, name):
        safe = self._safe_preset_name(name)
        if not safe:
            return {"error": "A preset needs a name."}
        settings_mod.save_preset(os.path.join(PRESET_DIR, safe + ".json"),
                                 self.settings)
        self._broadcast_presets()
        return {"saved": safe, "presets": self._list_presets()}

    def _preset_load(self, name):
        safe = self._safe_preset_name(name)
        path = os.path.join(PRESET_DIR, safe + ".json")
        if not safe or not os.path.exists(path):
            return {"error": "No preset called '{0}'.".format(name)}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            return {"error": "Could not read preset: {0}".format(e)}
        data.pop("_comment", None)
        changed, errors = self.pipeline.apply(data, remote=True)
        return {"loaded": safe, "changed": changed, "errors": errors}

    def _preset_delete(self, name):
        safe = self._safe_preset_name(name)
        path = os.path.join(PRESET_DIR, safe + ".json")
        if safe and os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                return {"error": str(e)}
        self._broadcast_presets()
        return {"deleted": safe, "presets": self._list_presets()}

    def _broadcast_presets(self):
        if self.server is not None:
            self.server.broadcast({"type": "presets",
                                   "data": self._list_presets()})

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

    def _whisper_server(self, args):
        """
        Start or stop the GPU server from the panel.

        Deliberately narrow: the only thing that can be passed through is the
        BASENAME of a .bin that already exists in _models, and it is checked
        against the real listing rather than trusted. The panel can be reachable
        from the network, and "run this batch file with these arguments" is not
        something a browser gets to say freely.
        """
        action = args.get("action", "status")
        if action == "stop":
            proc = self._server_proc
            self._server_proc = None
            if proc is None or proc.poll() is not None:
                return {"error": "This panel did not start a server."}
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10)
            except Exception as e:
                return {"error": "Could not stop it: {0}".format(e)}
            return {"stopped": True}

        if action != "start":
            return {"running": self._server_proc is not None
                              and self._server_proc.poll() is None}

        if not os.path.exists(SERVER_CMD):
            return {"error": "start_whisper_server.cmd is not next to app.py."}
        if self._server_proc is not None and self._server_proc.poll() is None:
            return {"error": "A server started from this panel is already "
                             "running."}
        model = os.path.basename(str(args.get("model", "")))
        known = [m["name"] for m in self._list_models()["ggml"]]
        if model and model not in known:
            return {"error": "'{0}' is not a model in _models.".format(model)}
        # Launched by its plain name with cwd set to the project, not by an
        # absolute path: the .cmd resolves _whisper.cpp and _models relative to
        # itself, and keeping every path in this project relative is what lets
        # the folder be moved, cloned or put on a different drive without
        # anything needing to be edited.
        cmd = ["cmd", "/c", "start", "", "cmd", "/k",
               os.path.basename(SERVER_CMD)]
        if model:
            cmd.append(model)
        try:
            self._server_proc = subprocess.Popen(cmd, cwd=HERE)
        except OSError as e:
            return {"error": "Could not start it: {0}".format(e)}
        return {"started": True, "model": model or "(default)",
                "note": "It opens in its own window. Give it a few seconds to "
                        "load, then watch the backend indicator switch to GPU."}


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
