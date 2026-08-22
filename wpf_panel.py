# -*- coding: utf-8 -*-
"""
The desktop control panel: a WPF window, driven from this process.

Why this exists alongside webui/
-------------------------------
The browser panel is a fine remote control and a poor local one. It needs a
listener, a port and a token; its buttons are command NAMES on a wire, so a
typo is a runtime "unknown command"; and it cannot edit the five web_* settings
because REMOTE_LOCKED strips them - a local panel being forbidden to configure
the listener it is talking through. This one calls the engine directly:

    <ui:Button Command="{Binding RestartCommand}" />   compiled XAML
        -> RelayCommand
        -> IEngineBridge.Restart()                     a CLR symbol
        -> EngineBridge.Restart below                  a Python method
        -> App._lifecycle("restart", ...)              the same code the socket calls

Neither replaces the other. --web and --wpf are independent, both can be open
at once, and _lifecycle's single busy slot already makes that safe.

How it is put together
----------------------
The View layer is a compiled C# assembly in ui/. That split is not taste: WPF
cannot data-bind to a Python object at all. pythonnet projects a Python class
as a CLR type with ZERO properties, so {Binding Status} against one renders an
empty string - silently, with no error and no exception. Measured, not assumed.
Since the whole settings pane is generated from settings.schema_json() by
binding, the ViewModels have to be C#, and Python implements one narrow
interface the C# calls.

Threading
---------
WPF runs on its own .NET-created STA thread. It must never be the main thread:
overlay.py's Tk mainloop owns that one, and app.py has a commit's worth of
scar tissue about it. Importing sounddevice already puts the main thread in
MAIN_STA, and hosting the CLR is apartment-neutral - verified in both import
orders - so nothing here disturbs soundcard's COM.

Every WPF object is thread-affine: reading a property from the wrong thread
throws InvalidOperationException. Nothing in this module touches the window
directly. State goes in through PanelHost.Post*, which marshals
asynchronously - never a blocking Dispatcher.Invoke, which would deadlock
against a bridge call waiting on the GIL.
"""
from __future__ import print_function

import json
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(HERE, "ui", "runtime")
UI_DLL = os.path.join(RUNTIME_DIR, "LiveTranscription.Ui.dll")
UI_RUNTIMECONFIG = os.path.join(
    RUNTIME_DIR, "LiveTranscription.Ui.runtimeconfig.json")

# The web panel's --accent, so the two front ends are visibly the same product.
ACCENT = "#00BCD4"

_LOADED = False
_LOAD_LOCK = threading.Lock()
_BRIDGE_CLASS = None


class PanelUnavailable(RuntimeError):
    """The panel cannot be built here, with a reason worth printing.

    Never fatal: --wpf failing has to degrade to "no desktop panel", not to
    "no transcription". A missing .NET runtime on someone else's machine must
    not be the difference between the program working and not.
    """


def _explain(exc):
    """Turn a load failure into something actionable rather than a stack."""
    text = "{0}: {1}".format(type(exc).__name__, exc)
    low = text.lower()
    if "clr_loader" in low or "hostfxr" in low or "coreclr" in low:
        return ("The .NET Desktop runtime 8.0 or newer was not found. "
                "Install 'Microsoft.WindowsDesktop.App' from "
                "https://dotnet.microsoft.com/download/dotnet - the Desktop "
                "Runtime, not the SDK.")
    if "presentationframework" in low:
        return ("The .NET runtime that was found has no WPF in it. That is "
                "the ASP.NET or base runtime; the Desktop Runtime is a "
                "separate download.")
    return text


def available():
    """Is the built panel present? Cheap - no CLR is loaded to answer this."""
    return os.path.isfile(UI_DLL) and os.path.isfile(UI_RUNTIMECONFIG)


def _load_clr():
    """Host the CLR once per process. Safe to call from several threads."""
    global _LOADED
    with _LOAD_LOCK:
        if _LOADED:
            return
        if not available():
            raise PanelUnavailable(
                "The panel assembly has not been built. Run build_ui.cmd "
                "(it needs the .NET 8 SDK, once) to create ui\\runtime.")
        try:
            from clr_loader import get_coreclr
            from pythonnet import set_runtime
        except ImportError:
            raise PanelUnavailable(
                "The desktop panel needs pythonnet: pip install pythonnet")

        try:
            # The PUBLISHED runtimeconfig, not a hand-written one: it names
            # Microsoft.WindowsDesktop.App, and clr_loader's own generated
            # config names only Microsoft.NETCore.App - which has no WPF.
            set_runtime(get_coreclr(runtime_config=UI_RUNTIMECONFIG))
            import clr

            clr.AddReference(UI_DLL)
        except PanelUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PanelUnavailable(_explain(exc))
        _LOADED = True


def _bridge_class():
    """
    Define the bridge type lazily, exactly once.

    It subclasses a .NET interface, so the class statement cannot even be
    parsed until the CLR is up and the assembly is loaded - which is why this
    is a function and not a module-level class.

    Cached because the statement does not merely define a Python class: it
    EMITS a CLR type into __namespace__. Running it twice raises "Duplicate
    type name within an assembly", and there is no way to unregister one.
    """
    global _BRIDGE_CLASS
    if _BRIDGE_CLASS is not None:
        return _BRIDGE_CLASS

    from LiveTranscription.Ui.Bridge import IEngineBridge

    class EngineBridge(IEngineBridge):
        """What every button ends up calling. One method, one engine call.

        Two rules, both load-bearing:

        1. Return promptly. These run ON the WPF dispatcher thread and hold
           the GIL while they run, so anything slow freezes the window.
           App._lifecycle already does the right thing - it hands the work to
           a daemon thread and returns immediately.
        2. Never touch the window from in here, and never block on it. A
           synchronous Dispatcher.Invoke from a bridge method, while the UI
           thread waits on the GIL, is the one deadlock this design can
           produce.
        """

        __namespace__ = "LiveTranscription.PythonBridge"

        def __init__(self, app):
            self._app = app

        # ---- lifecycle --------------------------------------------------
        # bool: was the command ACCEPTED. Not whether it succeeded - the real
        # outcome arrives later as events, because _lifecycle returns before
        # the work happens.

        def Start(self):
            return bool(self._app._lifecycle("start", self._app._pipeline_start))

        def Stop(self):
            return bool(self._app._lifecycle("stop", self._app._pipeline_stop))

        def Restart(self):
            return bool(self._app._lifecycle("restart", self._app._pipeline_restart))

        def Rebuild(self, components):
            wanted = [str(c) for c in components] or ["backend"]
            return bool(self._app._lifecycle(
                "rebuild", lambda: self._app.pipeline.rebuild(*wanted)))

        def Pause(self):
            self._app.pipeline.pause(True)

        def Resume(self):
            self._app.pipeline.pause(False)

        def ClearTranscript(self):
            self._app.pipeline.clear_history()
            if self._app.overlay is not None:
                # Queued by overlay.py for its own pump - safe off-thread.
                self._app.overlay.clear()

        def RequestExit(self):
            self._app._request_exit()

        # ---- whisper-server ---------------------------------------------

        def EngineAction(self, action, model):
            action = str(action)
            if action == "status":
                # Cheap enough not to need the single slot, and a poll that
                # queued behind a running Restart would report stale numbers
                # for the whole of it - the same special case _command makes.
                threading.Thread(target=self._app._refresh_engine,
                                 name="engine", daemon=True).start()
                return True
            args = {"action": action}
            if model:
                args["model"] = str(model)
            # Through _lifecycle, exactly like the websocket path: start
            # waits up to 45 s for the server to answer, and this method
            # runs ON the dispatcher thread - calling it directly would
            # freeze the window for the duration.
            return bool(self._app._lifecycle(
                "engine " + action,
                lambda: self._app._engine_action(action, args)))

        # ---- settings ----------------------------------------------------

        def GetSchemaJson(self):
            import settings as settings_mod

            return json.dumps(settings_mod.schema_json())

        def GetSettingsJson(self):
            return json.dumps(self._app.settings)

        def ApplySettings(self, patch_json):
            patch = json.loads(patch_json) if patch_json else {}
            # remote=False: this panel is local by construction, so the five
            # REMOTE_LOCKED settings are editable here. That is the point -
            # a desktop panel forbidden from configuring the listener would be
            # obeying a rule written for a different threat.
            changed, errors = self._app.pipeline.apply(patch, remote=False)
            return json.dumps({"changed": changed, "errors": errors})

        # ---- documents ---------------------------------------------------

        def GetStatusJson(self):
            return json.dumps(self._app.pipeline.status())

        def GetHistoryJson(self, limit):
            return json.dumps(self._app.pipeline.history(int(limit)))

        def GetModelsJson(self):
            return json.dumps(self._app._list_models())

        def GetDevicesJson(self):
            import audio_sources

            return json.dumps(audio_sources.list_devices())

        def GetEngineJson(self):
            return json.dumps(self._app._engine_status())

        def ExportJson(self, fmt):
            return json.dumps(self._app._export_payload(str(fmt)))

        # ---- presets ------------------------------------------------------

        def GetPresetsJson(self):
            return json.dumps(self._app._list_presets())

        def PresetSave(self, name):
            return json.dumps(self._app._preset_save(str(name)))

        def PresetLoad(self, name):
            return json.dumps(self._app._preset_load(str(name)))

        def PresetDelete(self, name):
            return json.dumps(self._app._preset_delete(str(name)))

        # ---- audio in ------------------------------------------------------

        def PushAudioChunk(self, pcm16le):
            # The desktop half of what `capture: browser` does: 16 kHz mono
            # little-endian PCM16, the same bytes the browser puts on its
            # socket, into the same Pipeline.feed. Runs on NAudio's capture
            # thread and must stay cheap - feed appends under a lock and
            # returns. bytes() copies out of the CLR array via the buffer
            # protocol; the array is exactly as long as the audio in it,
            # which is the C# side's contract.
            return bool(self._app.pipeline.feed(bytes(pcm16le)))

        # ---- misc ----------------------------------------------------------

        def RescanDevices(self):
            # Enumeration costs 585 ms the first time (PortAudio walks every
            # host API) - the same reason _ws_open never does it inline.
            threading.Thread(target=self._app._rescan_devices,
                             name="devices", daemon=True).start()

        def RunBenchmark(self, args_json):
            args = json.loads(args_json) if args_json else {}
            # _run_benchmark spawns the thread and returns nothing; the bool
            # here is "was it started", and everything after arrives as
            # benchmark events.
            self._app._run_benchmark(args)
            return True

    _BRIDGE_CLASS = EngineBridge
    return EngineBridge


class Panel(object):
    """Owns the WPF thread and is the only thing that talks to the window."""

    def __init__(self, app, accent=ACCENT, dark=True):
        self._app = app
        self._accent = accent
        self._dark = dark
        self._host = None
        self._thread = None
        self.bridge = None
        self._ready = threading.Event()
        self._error = None

    def start(self, timeout=30.0):
        """Bring the window up on its own STA thread. Returns when it is up."""
        _load_clr()

        from LiveTranscription.Ui import PanelHost
        from System.Threading import ApartmentState, Thread, ThreadStart

        self._host = PanelHost()
        bridge = _bridge_class()(self._app)
        # Kept so callers (and tests) use the SAME bridge the window uses,
        # rather than minting a second one.
        self.bridge = bridge

        def run():
            try:
                # Blocks in Application.Run until the window closes.
                self._host.Start(bridge, self._accent, self._dark)
            except Exception as exc:  # noqa: BLE001
                self._error = exc
            finally:
                self._ready.set()

        thread = Thread(ThreadStart(run))
        # STA is not optional: WPF requires it, and CPython's main thread is
        # the wrong place to get it - Tk lives there.
        thread.SetApartmentState(ApartmentState.STA)
        thread.IsBackground = True
        thread.Name = "wpf-panel"
        thread.Start()
        self._thread = thread

        deadline = threading.Event()
        while not deadline.wait(0.05):
            if self._error is not None:
                raise PanelUnavailable(_explain(self._error))
            if self._host.IsReady:
                return True
            if self._ready.is_set():        # run() finished without readiness
                raise PanelUnavailable(
                    "The panel window closed while it was starting.")
            timeout -= 0.05
            if timeout <= 0:
                raise PanelUnavailable("The panel did not open within 30s.")
        return False

    @property
    def alive(self):
        return self._host is not None and bool(self._host.IsReady)

    # ---- pushing state in -------------------------------------------------
    # All async. Never a blocking Invoke - see the module docstring.

    def hello(self, payload):
        """The opening document - the same dict _ws_open sends a browser."""
        if self._host is not None:
            self._host.PostHello(json.dumps(payload))

    def event(self, kind, data):
        """One (kind, data) pair off the engine's event stream.

        Meter frames skip the dispatcher entirely: OfferMeter parses and
        parks the values under a lock on THIS thread, and a 30 Hz timer on
        the UI thread publishes them. Eight BeginInvokes a second forever is
        exactly the cost that design exists to avoid. Everything else is rare
        enough that one marshal per event is the simple, correct answer.
        """
        if self._host is None:
            return
        if kind == "meter":
            self._host.OfferMeter(json.dumps(data))
        else:
            self._host.PostEvent(str(kind), json.dumps(data))

    def busy(self, name):
        if self._host is not None:
            self._host.PostBusy(name or "")

    def running(self, running, paused=False):
        if self._host is not None:
            self._host.PostRunning(bool(running), bool(paused))

    def status(self, text):
        if self._host is not None:
            self._host.PostStatus(str(text))

    def banner(self, severity, title, message=""):
        if self._host is not None:
            self._host.PostBanner(str(severity), str(title), str(message))

    def hide_banner(self):
        if self._host is not None:
            self._host.PostBannerHidden()

    def request_close(self):
        """Ask the window to close, without waiting for it.

        This is the ONLY close that may be called from a bridge method (the
        Exit button ends up here): close() joins the WPF thread, and joining
        the WPF thread FROM the WPF thread deadlocks for the whole timeout.
        """
        if self._host is not None:
            self._host.Shutdown()

    def close(self, timeout=8.0):
        """Close the window and wait for the WPF thread to end.

        For shutdown paths that do not run on the panel's own thread -
        App.shutdown on the main thread. Joining matters there: the process
        is about to end, and a message loop still draining while the
        interpreter finalizes is a use-after-free with a stack.
        """
        self.request_close()
        if self._thread is not None:
            self._thread.Join(int(timeout * 1000))
