"""
The desktop panel's view-models, driven through the published assembly.

shot.py renders and soak.py measures; neither asserts what a view-model DID,
and that is how the preset-ack bug lived to the first run on real hardware:
every preset call toasted "Preset loaded", whatever came back. These tests
put app.py's own handlers - presets, the settings echo, the theme - under the
real SettingsVm, and read the toasts and the theme back off the dispatcher,
so a regression on either side of the bridge fails here.

Needs pythonnet, the .NET Desktop Runtime and a built ui/runtime: it loads
the artifact that ships, so after a ui/ change run `.\\build_ui.cmd` first.
That is checked rather than trusted: build_ui.cmd stamps ui/runtime with a
hash of its sources, and this module skips when the stamp disagrees, since
a green run against a stale assembly would say the opposite of the truth.
tests/test_ui_stamp.py is the part that fails.
Skips itself otherwise, as test_real_model.py skips without the model. The
window opens on screen for a few seconds:

    .venv\\Scripts\\python.exe -m unittest tests.test_panel -v
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOLS = os.path.join(REPO, "ui", "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import settings as settings_mod  # noqa: E402
import uihash  # noqa: E402
import wpf_panel  # noqa: E402

APP = None
PANEL = None
TMP = None
_SAVED_DIRS = None
_SAVED_STATE = None
LOGS = []


def _make_app_class():
    """soak's engine stand-in with app.py's own handlers bound onto it.

    Built here rather than at import, so that discovering the suite does not
    import app.py (and the audio stack under it) on a machine that will skip.
    """
    import app as app_mod
    import soak

    class _Pipe(soak._SoakApp._P):
        def log(self, level, message):
            # soak's stand-in has no logger: it drives the panel and measures
            # it, and never runs the app.py methods that report. Bound handlers
            # DO log, and a couple of them say something a test wants to read
            # back, so they land here instead of on the floor.
            LOGS.append((level, message))

        def feed(self, pcm16le):
            # The capture card's push path: IEngineBridge.PushAudioChunk ->
            # Pipeline.feed. A flag rather than a source, so a test can make
            # the engine refuse - stopped, or not in a push mode - and then
            # take audio again, and can count what arrived.
            outer = self._outer
            outer.fed += 1
            return outer.take_audio

        def apply(self, patch, remote=False, drain=True):
            outer = self._outer
            clean, errors = settings_mod.validate(patch, outer.settings)
            changed = dict((k, v) for k, v in clean.items()
                           if outer.settings.get(k) != v)
            outer.settings.update(changed)
            # What Pipeline.apply does next: emit the settings document.
            # app.py's _on_event fans it out to the panel AND acts on it -
            # the overlay, the theme - which is the path under test.
            outer._on_event("settings", dict(outer.settings))
            return changed, errors

    class TestApp(soak._SoakApp):
        _P = _Pipe
        quiet = True
        server = None

        def __init__(self):
            soak._SoakApp.__init__(self)
            self.settings["wpf"] = True          # as --wpf would have set it
            self._last_overlay_style = None
            self._last_panel_theme = None
            self.take_audio = True      # what feed() answers
            self.fed = 0                # blocks that reached it

    # _list_models is NOT bound: soak's stand-in serves a fixed one-entry list
    # so the panel has something stable to draw, and taking app.py's real
    # filesystem scan instead would make these tests depend on which .bin files
    # happen to be sitting in _models on this machine.
    for name in ("_safe_preset_name", "_list_presets", "_preset_file",
                 "_preset_save", "_preset_load", "_preset_delete",
                 "_broadcast_presets", "_notify", "_on_event",
                 "_overlay_style", "_sync_overlay", "_sync_panel_theme",
                 "_panel_dark", "_boot_model", "_remember_model"):
        setattr(TestApp, name, app_mod.App.__dict__[name])
    return TestApp


def _hello(app):
    import shot

    doc = shot._hello(app)
    doc["presets"] = app._list_presets()
    return doc


# ---- the dispatcher -----------------------------------------------------

def on_ui(fn, timeout=30.0):
    """Run fn on the WPF thread; return what it returned, or re-raise.

    Background priority, the same PanelHost.Post* uses, so an action queues
    BEHIND whatever the engine just pushed - a theme swap, a hello - rather
    than jumping it. That ordering is what lets a test post an edit and then
    read its consequence with two plain calls.
    """
    import System.Windows
    from System import Action
    from System.Windows.Threading import DispatcherPriority

    box = {}
    done = threading.Event()

    def wrapped():
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            done.set()

    System.Windows.Application.Current.Dispatcher.BeginInvoke(
        DispatcherPriority.Background, Action(wrapped))
    if not done.wait(timeout):
        raise AssertionError("the dispatcher never ran the action - window "
                             "closed, or wedged")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def main_vm():
    import System.Windows

    return System.Windows.Application.Current.MainWindow.DataContext


def _toasts():
    return [(str(t.Severity), str(t.Title), str(t.Message))
            for t in main_vm().StatusBar.Toasts]


def _clear_toasts():
    bar = main_vm().StatusBar
    for t in list(bar.Toasts):
        bar.Dismiss(t)


def _presets():
    return [str(p) for p in main_vm().SettingsPane.Presets]


def _preset_call(method, name):
    """SettingsPane.<method>(name) on the dispatcher -> (toasts, presets)."""
    def f():
        _clear_toasts()
        getattr(main_vm().SettingsPane, method)(name)
        return _toasts(), _presets()
    return on_ui(f)


def _preset_call_watching(method, name):
    """
    As _preset_call, but recording how the Presets collection MOVED.

    PresetBox is an editable ComboBox bound straight to that collection, and
    its Text is the selection - so what the collection does to itself is the
    whole question. A Reset (which is what Clear() raises) blanks the box.
    Watching the events is the only way to see that from here: the ComboBox
    itself is a view, and these tests drive the view model.
    """
    def f():
        pane = main_vm().SettingsPane
        seen = []

        def on_changed(_sender, args):
            seen.append(str(args.Action))

        pane.Presets.CollectionChanged += on_changed
        try:
            _clear_toasts()
            getattr(pane, method)(name)
        finally:
            pane.Presets.CollectionChanged -= on_changed
        return seen, _presets()
    return on_ui(f)


def _field(key, prop):
    return on_ui(lambda: getattr(main_vm().SettingsPane.Field(key), prop))


# ---- one window for the module --------------------------------------------

def setUpModule():
    global APP, PANEL, TMP, _SAVED_DIRS, _SAVED_STATE
    if not wpf_panel.available():
        raise unittest.SkipTest("no ui/runtime - run .\\build_ui.cmd first")
    if not uihash.is_current():
        # Everything here loads the shipped assembly rather than building
        # one, so against a stale DLL these tests are not wrong - they are
        # meaningless, and a green run would say the opposite. Skipping is
        # the honest answer; tests/test_ui_stamp.py is the half that fails,
        # and it needs no .NET, so it runs even where this module cannot.
        raise unittest.SkipTest(
            "ui/runtime is stale - ui/ sources have changed since it "
            "was built. Run .\\build_ui.cmd, then rerun.")
    try:
        wpf_panel._load_clr()
    except wpf_panel.PanelUnavailable as e:
        raise unittest.SkipTest(str(e))
    import clr

    clr.AddReference(os.path.join(wpf_panel.RUNTIME_DIR, "Wpf.Ui.dll"))
    for asm in ("PresentationFramework", "PresentationCore", "WindowsBase"):
        clr.AddReference(asm)

    import app as app_mod

    # A preset tree of our own: one shipped profile, one personal file the
    # validator will refuse. app.py reads the module globals at call time.
    TMP = tempfile.mkdtemp(prefix="panel-test-")
    user = os.path.join(TMP, "presets")
    builtin = os.path.join(user, "builtin")
    os.makedirs(builtin)
    with open(os.path.join(builtin, "Speech.json"), "w") as fh:
        json.dump({"vad": True, "vad_min_speech_ms": 250}, fh)
    with open(os.path.join(user, "Bad.json"), "w") as fh:
        json.dump({"buffer": 99}, fh)              # above the field's maximum
    _SAVED_DIRS = (app_mod.PRESET_DIR, app_mod.BUILTIN_PRESET_DIR)
    app_mod.PRESET_DIR, app_mod.BUILTIN_PRESET_DIR = user, builtin

    # Remembered state goes to the temp tree too. _remember_model writes on
    # every engine start, and a test that leaves _state.json in the checkout
    # would change what the NEXT real launch starts on.
    _SAVED_STATE = settings_mod.STATE_FILE
    settings_mod.STATE_FILE = os.path.join(TMP, "_state.json")

    APP = _make_app_class()()
    PANEL = wpf_panel.Panel(APP, dark=APP._panel_dark())
    PANEL.start()
    APP.panel = PANEL
    APP._last_panel_theme = APP.settings["wpf_theme"]
    PANEL.hello(_hello(APP))
    for _ in range(150):
        if on_ui(lambda: main_vm().SettingsPane.IsLoaded):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("the settings pane never loaded the schema")


def tearDownModule():
    if PANEL is not None:
        PANEL.close()
    if _SAVED_DIRS is not None:
        import app as app_mod

        app_mod.PRESET_DIR, app_mod.BUILTIN_PRESET_DIR = _SAVED_DIRS
    if _SAVED_STATE is not None:
        settings_mod.STATE_FILE = _SAVED_STATE
    if TMP is not None:
        shutil.rmtree(TMP, ignore_errors=True)


class Presets(unittest.TestCase):
    """PresetAck: what the panel says is what app.py answered."""

    def test_a_built_in_loads_and_the_toast_names_it(self):
        toasts, names = _preset_call("LoadPreset", "Speech")
        self.assertEqual(toasts, [("Success", "Preset loaded", "Speech")])
        self.assertEqual(names, ["Bad", "Speech"])      # the dropdown survived
        self.assertTrue(_field("vad", "BoolValue"))

    def test_a_missing_preset_is_refused_not_loaded(self):
        toasts, names = _preset_call("LoadPreset", "nope")
        self.assertEqual(toasts, [("Error", "Preset 'nope' was refused",
                                   "No preset called 'nope'.")])
        self.assertEqual(names, ["Bad", "Speech"])

    def test_a_refused_field_is_one_error_toast_and_no_success(self):
        before = _field("buffer", "NumberValue")
        toasts, names = _preset_call("LoadPreset", "Bad")
        self.assertEqual(len(toasts), 1, toasts)
        severity, title, message = toasts[0]
        self.assertEqual((severity, title),
                         ("Error", "One setting in 'Bad' was refused"))
        self.assertEqual(message, "Buffer length: 99 is above the maximum 30")
        self.assertEqual(_field("buffer", "NumberValue"), before)
        self.assertEqual(names, ["Bad", "Speech"])

    def test_your_own_saved_profile_loads_without_an_error_toast(self):
        # Reported from a real session: loading "aaa" said
        #   One setting in 'aaa' was refused
        #   'wpf' can only be set when starting the program or from the
        #   desktop panel.
        # every single time. Nobody had put `wpf` in that preset - save_preset
        # stored every non-default value, and `wpf` is true whenever the panel
        # is running, so saving a profile FROM the panel always captured it and
        # loading it always refused it. The refusal was correct and the preset
        # was wrong.
        #
        # Written and removed inside the test rather than added to the shared
        # fixture: every other test here asserts the dropdown's exact contents,
        # so a permanent extra file rewrites six unrelated expectations.
        import app as app_mod
        path = os.path.join(app_mod.PRESET_DIR, "Legacy.json")
        with open(path, "w") as fh:
            json.dump({"translate": True, "wpf": True}, fh)
        try:
            toasts, names = _preset_call("LoadPreset", "Legacy")
            applied = _field("translate", "BoolValue")
        finally:
            # Remove the file AND resync the dropdown from it. The panel's
            # Presets collection is refilled from each ack, so leaving it
            # holding a name whose file is gone makes the NEXT test see a
            # Remove on a collection it asserts never moves - which is a
            # failure in someone else's test, caused here.
            os.remove(path)
            _preset_call("LoadPreset", "Speech")

        self.assertEqual(toasts, [("Success", "Preset loaded", "Legacy")])
        self.assertTrue(applied,
                        "the profile's real settings must still apply")
        self.assertIn("Legacy", names)

    def test_loading_a_preset_does_not_disturb_the_dropdown(self):
        # The selection used to vanish the moment a load SUCCEEDED. app.py
        # rides the preset list on every ack so a refusal cannot empty the
        # dropdown, PresetAck hands every ack to SetPresets, and SetPresets
        # cleared before it refilled - a Reset on the collection an editable
        # ComboBox is bound to, which blanks its Text. A load cannot change
        # the list, so the collection must not move at all.
        changes, names = _preset_call_watching("LoadPreset", "Speech")
        self.assertEqual(changes, [])
        self.assertEqual(names, ["Bad", "Speech"])

    def test_a_refusal_does_not_disturb_it_either(self):
        changes, names = _preset_call_watching("LoadPreset", "nope")
        self.assertEqual(changes, [])
        self.assertEqual(names, ["Bad", "Speech"])

    def test_saving_and_deleting_move_only_the_entry_that_changed(self):
        # The list genuinely changes here, and it still must not Reset:
        # typing a new name and pressing Save should leave the name in the
        # box. Deleted last, so the tree is as this test found it.
        changes, names = _preset_call_watching("SavePreset", "Fresh")
        self.assertEqual(changes, ["Add"])
        self.assertEqual(names, ["Bad", "Fresh", "Speech"])

        changes, names = _preset_call_watching("DeletePreset", "Fresh")
        self.assertEqual(changes, ["Remove"])
        self.assertEqual(names, ["Bad", "Speech"])

    def test_a_built_in_cannot_be_deleted_but_its_shadow_can(self):
        import app as app_mod

        toasts, _ = _preset_call("DeletePreset", "Speech")
        self.assertEqual(len(toasts), 1, toasts)
        self.assertEqual(toasts[0][:2], ("Error", "Preset 'Speech' was refused"))
        self.assertIn("built-in", toasts[0][2])

        toasts, names = _preset_call("SavePreset", "Speech")
        self.assertEqual(toasts, [("Success", "Preset saved", "Speech")])
        self.assertEqual(names, ["Bad", "Speech"])      # shadowed: listed once
        self.assertTrue(os.path.exists(
            os.path.join(app_mod.PRESET_DIR, "Speech.json")))

        toasts, names = _preset_call("DeletePreset", "Speech")
        self.assertEqual(toasts, [("Informational", "Preset deleted", "Speech")])
        self.assertEqual(names, ["Bad", "Speech"])      # the original is back
        self.assertEqual(APP._preset_file("Speech"),
                         os.path.join(app_mod.BUILTIN_PRESET_DIR, "Speech.json"))

    def test_an_ack_without_a_presets_array_leaves_the_list_alone(self):
        def f():
            pane = main_vm().SettingsPane
            before = _presets()
            pane.SetPresets('{"loaded": "Speech"}')
            missing = _presets()
            pane.SetPresets('{"presets": "not a list"}')
            wrong = _presets()
            pane.SetPresets(json.dumps(APP._list_presets()))
            return before, missing, wrong, _presets()

        before, missing, wrong, restored = on_ui(f)
        self.assertEqual(missing, before)
        self.assertEqual(wrong, before)
        self.assertEqual(restored, ["Bad", "Speech"])


class Theme(unittest.TestCase):
    """wpf_theme: edited in the pane, applied by app.py, seen in the window."""

    @staticmethod
    def _edit(value):
        def f():
            pane = main_vm().SettingsPane
            pane.Field("wpf_theme").TextValue = value
            pane.FlushNow()                 # skip the 250 ms debounce
        on_ui(f)

    @staticmethod
    def _look():
        import System.Windows
        from Wpf.Ui.Appearance import (ApplicationAccentColorManager,
                                       ApplicationTheme,
                                       ApplicationThemeManager)

        app = System.Windows.Application.Current
        return {
            "light": ApplicationThemeManager.GetAppTheme() == ApplicationTheme.Light,
            "accent": str(app.Resources["PanelAccentBrush"].Color),
            "primary": str(ApplicationAccentColorManager.PrimaryAccent),
            "card": str(app.Resources["PanelCardBrush"].Color),
            "window": str(app.MainWindow.Background.Color),
        }

    def test_the_setting_re_themes_the_open_window(self):
        try:
            self._edit("light")
            self.assertEqual(APP.settings["wpf_theme"], "light")
            look = on_ui(self._look)
            self.assertTrue(look["light"], look)
            self.assertEqual(look["accent"], "#FF0097A7")
            self.assertEqual(look["primary"], "#FF0097A7")  # ApplyAccent re-ran
            self.assertEqual(look["card"], "#FFFFFFFF")
            self.assertEqual(look["window"], "#FFECEFF1")   # opaque, and light
        finally:
            self._edit("dark")
        look = on_ui(self._look)
        self.assertFalse(look["light"], look)
        self.assertEqual(look["accent"], "#FF00BCD4")
        self.assertEqual(look["primary"], "#FF00BCD4")
        self.assertEqual(look["window"], "#FF0B1013")

    def test_the_browser_cannot_move_it(self):
        self.assertIn("wpf_theme", settings_mod.REMOTE_LOCKED)
        self.assertTrue(_field("wpf_theme", "LocalOnly"))

    def test_the_primary_buttons_take_the_brand_run_in_both_themes(self):
        # .btn.primary is the accent falling to #00838F. Panel.xaml overrides
        # WPF-UI's three AccentButtonBackground keys with that run, and the
        # override has to survive a live theme switch, which re-applies
        # WPF-UI's own dictionary underneath.
        import System.Windows

        def run_brush():
            w = System.Windows.Application.Current.MainWindow
            brush = w.FindName("BtnRun").Background
            stops = [str(s.Color) for s in brush.GradientStops]
            return type(brush).__name__, stops

        try:
            self._edit("light")
            self.assertEqual(on_ui(run_brush),
                             ("LinearGradientBrush", ["#FF0097A7", "#FF00838F"]))
        finally:
            self._edit("dark")
        self.assertEqual(on_ui(run_brush),
                         ("LinearGradientBrush", ["#FF00BCD4", "#FF00838F"]))


class EngineModel(unittest.TestCase):
    """server_model: one picker, and the choice survives the next launch."""

    @staticmethod
    def _ggml():
        return [m["name"] for m in APP._list_models()["ggml"]]

    @staticmethod
    def _choices():
        return on_ui(lambda: [(c.Value, c.Available) for c in
                              main_vm().SettingsPane.Field("server_model").Choices])

    def test_the_field_is_a_picker_over_the_bin_files_in_models(self):
        # Found by its dynamic source, never by key. Both model lists work this
        # way now - `model` was the last key special-cased by name anywhere in
        # the UI and is a declared choice too, so if this ever needs a name
        # test instead, the declaration drifted.
        self.assertEqual(_field("server_model", "ChoiceSource"), "ggml_models")
        self.assertEqual([v for v, available in self._choices() if available],
                         self._ggml())

    def test_the_engine_tab_is_the_only_editor_for_it(self):
        """
        One key, one control.

        server_model had two: this picker, and a row in the generated settings
        form. That is not a second opinion - the form's copy looked like it
        applied on its own and could not, because rebuild="engine" means the
        running server cannot be told. owner="engine" is how the schema says
        which surface draws it, and Refilter is what honours that.

        Asserted on IsVisible rather than on the field's absence, deliberately:
        the field MUST still be there. It carries the value, it flushes through
        the same path, and SetModels finds it - only the widget is gone.
        """
        self.assertEqual(_field("server_model", "Owner"), "engine")
        self.assertFalse(_field("server_model", "IsVisible"),
                         "server_model is still drawn in the settings form")
        self.assertFalse(_field("server_model", "IsShown"))
        # Still present and still holding its value, which is the half that
        # would be easy to break by "hiding" it out of the collection.
        self.assertEqual(_field("server_model", "Key"), "server_model")

    def test_the_faster_whisper_model_is_a_picker_too(self):
        """
        `model` is a folder picker over _models, by dynamic source.

        It was declared a path and special-cased by the literal name "model" in
        FieldTemplateSelector, SettingsVm and webui/app.js. Three copies of one
        exception is what made it worth removing: this pins the replacement, so
        a revert to kind="path" fails here rather than showing a text box that
        invites a hand-typed path into a REMOTE_LOCKED setting.
        """
        self.assertEqual(_field("model", "Kind"), "choice")
        self.assertEqual(_field("model", "ChoiceSource"), "faster_whisper_models")
        # Unlike server_model it stays in the form - nothing else edits it.
        self.assertEqual(_field("model", "Owner"), "")
        folders = [m["path"] for m in APP._list_models()["faster_whisper"]]
        shown = on_ui(lambda: [c.Value for c in
                               main_vm().SettingsPane.Field("model").Choices
                               if c.Available])
        self.assertEqual(shown, folders)

    def test_a_configured_model_that_is_not_there_stays_visible(self):
        # The engine's list here deliberately does not contain the configured
        # model, which is the "remembered it, then deleted or renamed the file"
        # case. The name has to stay in the dropdown and be marked, because the
        # alternative is a picker quietly showing a different model than the
        # one the setting holds - and then starting on that.
        current = APP.settings["server_model"]
        self.assertNotIn(current, self._ggml())
        self.assertIn((current, False), self._choices())

    def test_starting_on_a_model_remembers_it_and_the_panel_follows(self):
        names = self._ggml()
        if not names:
            self.skipTest("the engine stand-in served no models")
        before = APP.settings["server_model"]
        pick = next(n for n in names if n != before)
        try:
            APP._remember_model(pick)
            self.assertEqual(APP.settings["server_model"], pick)
            # The file is what the next launch reads, so assert the file and
            # not just the dict - _remember_model updating one without the
            # other is exactly the bug this pair exists to catch.
            self.assertEqual(settings_mod.load_state(), {"server_model": pick})
            # on_ui is Background priority, so this read queues behind the
            # notify _remember_model posted; no sleep needed.
            self.assertEqual(_field("server_model", "TextValue"), pick)
        finally:
            APP._remember_model(before)

    def test_the_tab_opens_on_the_configured_model_until_a_human_picks(self):
        # One test and not two on purpose. "Has anyone picked yet" is sticky
        # for the life of the window - that is the point of it - so a separate
        # test that picks would decide this one's result through whichever ran
        # first. The ordering IS the behaviour, so it is asserted in order.
        served = APP._engine_status()["model"]
        self.assertTrue(served)

        # Opens on the configured model, not the "(the launcher's default)"
        # entry. MainVm.Hello refreshes the list TWICE - ApplyModels, then
        # Apply - and only the second carries the configured name, so the
        # first used to win and the tab advertised a model the engine was not
        # going to start on.
        self.assertEqual(
            on_ui(lambda: main_vm().Engine.SelectedModel.Value), served)

        def pick_default():
            engine = main_vm().Engine
            engine.SelectedModel = engine.Models[0]     # the launcher default
            return engine.SelectedModel.Value

        def refresh_then_read():
            engine = main_vm().Engine
            engine.Apply(json.dumps(APP._engine_status()))
            return engine.SelectedModel.Value

        self.assertEqual(on_ui(pick_default), "")
        # The configured model must NOT reclaim the box now. Choosing the
        # launcher's default deliberately looks identical BY VALUE to an
        # untouched dropdown - both are "" - so this is the case that proves
        # the two are told apart by reference instead.
        self.assertEqual(on_ui(refresh_then_read), "")

    def test_the_launcher_default_is_not_remembered(self):
        before = APP.settings["server_model"]
        APP._remember_model("")
        self.assertEqual(APP.settings["server_model"], before)

    def test_a_remembered_model_that_is_gone_does_not_stop_the_server(self):
        before = APP.settings["server_model"]
        del LOGS[:]
        try:
            APP.settings["server_model"] = "ggml-not-here.bin"
            # "" means "let start_whisper_server.cmd pick", which is the whole
            # point: a model deleted since the last run must not turn into a
            # boot that refuses to start the engine at all. _engine_start's own
            # check DOES refuse an unknown name, because there the name came
            # from a browser; the two callers want opposite things.
            self.assertEqual(APP._boot_model(), "")
        finally:
            APP.settings["server_model"] = before
        warnings = [m for level, m in LOGS if level == "warn"]
        self.assertTrue(any("ggml-not-here.bin" in m for m in warnings), LOGS)

        # And the ordinary case still passes the name through.
        served = self._ggml()
        if served:
            APP.settings["server_model"] = served[0]
            try:
                self.assertEqual(APP._boot_model(), served[0])
            finally:
                APP.settings["server_model"] = before


class RefusedEdits(unittest.TestCase):
    """ApplyAck: a refused edit snaps back everywhere it had reached."""

    def test_a_refusal_takes_the_show_if_gates_back_with_it(self):
        # Switching capture to "file" is refused while no WAV is named
        # (settings.validate's cross-field rule). OnFieldEdited writes the
        # new value into the live map at once so the WAV-file row can open
        # before the ack; the refusal has to take that back, or the row stays
        # open on a value the engine never held until the next echo - and a
        # patch refused whole produces no echo at all.
        #
        # Read inside the one dispatcher action, before the stand-in's echo
        # (posted at Background priority) can land: that is the window the
        # fix is for.
        def f():
            pane = main_vm().SettingsPane
            _clear_toasts()
            capture = pane.Field("capture")
            wav = pane.Field("file_path")
            capture.TextValue = "file"
            shown_on_edit = wav.IsVisible
            pane.FlushNow()
            return (shown_on_edit, wav.IsVisible, capture.TextValue,
                    capture.Error, _toasts())

        shown_on_edit, shown_after, value, error, toasts = on_ui(f)
        self.assertTrue(shown_on_edit, "the row must follow the edit at once")
        self.assertFalse(shown_after, "the row must follow the refusal too")
        self.assertEqual(value, "loopback")
        self.assertIn("no WAV file", error)
        self.assertEqual(len(toasts), 1, toasts)
        self.assertEqual(toasts[0][:2], ("Error", "That setting was refused"))
        self.assertEqual(APP.settings["capture"], "loopback")

    def test_two_refusals_in_one_patch_each_name_their_own_field(self):
        # settings._coerce writes "<label>: <why>" and the ack carries one
        # flat list, so a refused row has to find its sentence by its label.
        # Both rows used to show the first one: the theme row carrying the
        # capture row's complaint.
        def f():
            pane = main_vm().SettingsPane
            _clear_toasts()
            pane.Field("capture").TextValue = "bogus"
            pane.Field("wpf_theme").TextValue = "purple"
            pane.FlushNow()
            return (pane.Field("capture").Error,
                    pane.Field("wpf_theme").Error,
                    pane.Field("capture").TextValue,
                    pane.Field("wpf_theme").TextValue, _toasts())

        capture_error, theme_error, capture, theme, toasts = on_ui(f)
        self.assertTrue(capture_error.startswith("Capture mode: "), capture_error)
        self.assertTrue(theme_error.startswith("Desktop panel theme: "), theme_error)
        self.assertEqual((capture, theme), ("loopback", "dark"))
        self.assertEqual(len(toasts), 1, toasts)
        self.assertEqual(toasts[0][:2], ("Error", "2 settings were refused"))


class Toasts(unittest.TestCase):
    """StatusVm.Toast: four slots, and which one gives way."""

    @staticmethod
    def _life():
        from LiveTranscription.Ui.ViewModels import ToastLife
        return ToastLife

    def test_a_question_survives_a_burst_of_errors(self):
        # A ToastLife.Never toast is a question - "the backend has nowhere to
        # go" - and it is asked exactly when the error lines arrive. Evicting
        # oldest-first dropped it behind the fourth error, which answered the
        # question "no" with nobody saying so.
        def f():
            life = self._life()
            bar = main_vm().StatusBar
            _clear_toasts()
            bar.Toast("Warning", "Question", "still there?", life.Never)
            for i in range(6):
                bar.Toast("Error", "Error", "line {0}".format(i), life.Error)
            titles = [str(t.Title) for t in bar.Toasts]
            _clear_toasts()
            return titles

        titles = on_ui(f)
        self.assertEqual(len(titles), 4, titles)
        self.assertEqual(titles[0], "Question")
        self.assertEqual(titles[1:], ["Error"] * 3)

    def test_only_when_every_slot_is_a_question_does_the_oldest_go(self):
        def f():
            life = self._life()
            bar = main_vm().StatusBar
            _clear_toasts()
            for i in range(5):
                bar.Toast("Warning", "Q{0}".format(i), "?", life.Never)
            titles = [str(t.Title) for t in bar.Toasts]
            _clear_toasts()
            return titles

        self.assertEqual(on_ui(f), ["Q1", "Q2", "Q3", "Q4"])


class CaptureCard(unittest.TestCase):
    """AudioVm: when the card may say the engine is not taking audio."""

    #: AudioVm.RejectedBeforeComplaint - 40 blocks of 128 ms, about five
    #: seconds. Pinned as the number on purpose: it was 8, and one second is
    #: inside the window a running pipeline legitimately takes to switch its
    #: capture mode (Pipeline.apply lands at the top of the NEXT worker
    #: iteration, which is up to one inference later).
    BLOCKS = 40
    COMPLAINT = "the engine is not taking audio"
    STREAMING = "streaming the microphone"

    # The streamer is NAudio and a real device, so the accounting under test
    # is driven from the callback NAudio calls, and the state Start() would
    # have left behind is set by hand. Reflection, by name: a rename on the
    # C# side fails here loudly, which is the right failure.

    def _feed(self, n):
        from System import Array, Byte, Object
        from System.Reflection import BindingFlags

        def f():
            audio = main_vm().Audio
            flags = BindingFlags.NonPublic | BindingFlags.Instance
            on_block = audio.GetType().GetMethod("OnBlock", flags)
            args = Array[Object]([Array[Byte](4096)])
            for _ in range(n):
                on_block.Invoke(audio, args)
        on_ui(f)

    def _set(self, state, streaming_text, rejected, complained):
        from System import Int32
        from System.Reflection import BindingFlags

        def f():
            audio = main_vm().Audio
            flags = BindingFlags.NonPublic | BindingFlags.Instance
            t = audio.GetType()
            t.GetProperty("StateText").SetValue(audio, state)
            t.GetField("_streamingText", flags).SetValue(audio, streaming_text)
            t.GetField("_rejected", flags).SetValue(audio, Int32(rejected))
            t.GetField("_complained", flags).SetValue(audio, complained)
        on_ui(f)

    def _state(self):
        return on_ui(lambda: str(main_vm().Audio.StateText))

    def test_the_complaint_waits_five_seconds_and_goes_when_audio_is_taken(self):
        fed_before = APP.fed
        self._set(self.STREAMING, self.STREAMING, 0, False)
        try:
            APP.take_audio = False
            self._feed(self.BLOCKS - 1)
            self.assertEqual(self._state(), self.STREAMING,
                             "complained before the switch could have landed")
            self._feed(1)
            self.assertTrue(self._state().startswith(self.COMPLAINT),
                            self._state())

            APP.take_audio = True
            self._feed(1)
            self.assertEqual(self._state(), self.STREAMING,
                             "the complaint stayed over audio being taken")
            self.assertEqual(APP.fed - fed_before, self.BLOCKS + 1,
                             "the blocks never reached the bridge")
        finally:
            APP.take_audio = True
            self._set("not streaming", "", 0, False)


class EngineCard(unittest.TestCase):
    """EngineVm: the Stop button, and whose process is on the port."""

    def test_stop_is_dead_for_a_process_that_is_not_ours(self):
        # App._engine_stop refuses, by design, to kill an image that is not a
        # whisper server, so a Stop lit for a stranger's process on the port
        # could only ever produce an error line. The card's advice for that
        # state already says to close the program yourself.
        base = APP._engine_status()

        def can_stop(**changes):
            def f():
                engine = main_vm().Engine
                engine.Apply(json.dumps(dict(base, **changes)))
                return engine.StopCommand.CanExecute(None)
            return on_ui(f)

        try:
            self.assertFalse(can_stop(reachable=False, pid=4242, ours=False))
            self.assertTrue(can_stop(reachable=False, pid=4242, ours=True))
            self.assertTrue(can_stop(reachable=True, pid=4242, ours=True))
            self.assertFalse(can_stop(reachable=False, pid=None, ours=False))
        finally:
            on_ui(lambda: main_vm().Engine.Apply(json.dumps(base)))


class TranscriptLines(unittest.TestCase):
    """TranscriptLineVm: what a caption's words look like on screen."""

    @staticmethod
    def _line(words, text=""):
        from LiveTranscription.Ui.Bridge import Doc
        from LiveTranscription.Ui.ViewModels import TranscriptLineVm
        entry = {"id": 1, "text": text, "language": "en", "translated": False,
                 "backend": "server", "t_start": 0.0, "duration": 4.0,
                 "processing_time": 1.0, "wall": 0,
                 "words": [{"word": w, "probability": 0.9} for w in words]}
        line = TranscriptLineVm(Doc.Parse(json.dumps(entry)), 0.6)
        return [str(w.Text) for w in line.Words]

    def test_the_first_word_does_not_carry_its_leading_space(self):
        # Both backends keep whisper's leading space on every word - it is
        # what lets the Runs read as prose. On the first word it is an
        # indent the browser never shows (HTML collapses it) and a TextBlock
        # does, so the display copy drops it. Later words keep theirs, or
        # the line runs together.
        self.assertEqual(self._line([" the", " quick", " fox"]),
                         ["the", " quick", " fox"])

    def test_a_blank_first_word_is_skipped_not_shown(self):
        self.assertEqual(self._line([" ", " the", " fox"]), ["the", " fox"])

    def test_the_flat_text_fallback_is_untouched(self):
        self.assertEqual(self._line([], text="no word timings"),
                         ["no word timings"])


class MicDecode(unittest.TestCase):
    """
    PcmDecimator: the capture card's bytes, without a device.

    MicStreamer owns the WASAPI endpoint and NAudio's thread; the arithmetic
    was split out of it so that these can feed synthetic frames and read the
    blocks back. Formats are NAudio's own WaveFormat objects, built the way
    the drivers build them.
    """

    @classmethod
    def setUpClass(cls):
        import clr
        clr.AddReference(os.path.join(wpf_panel.RUNTIME_DIR, "NAudio.Core.dll"))

    @staticmethod
    def _decimator():
        from System import Action, Array, Byte
        from LiveTranscription.Ui.Audio import PcmDecimator

        blocks = []
        dec = PcmDecimator(Action[Array[Byte]](lambda b: blocks.append(bytes(b))))
        return dec, blocks

    @staticmethod
    def _shorts(block):
        return struct.unpack("<%dh" % (len(block) // 2), block)

    def test_float_stereo_at_48k_becomes_mono_16k_in_exact_blocks(self):
        from NAudio.Wave import WaveFormatExtensible

        dec, blocks = self._decimator()
        dec.Configure(WaveFormatExtensible(48000, 32, 2))   # the mix format
        self.assertEqual(dec.Ratio, 3.0)
        # 6144 frames at 48 kHz = 2048 output samples = exactly one block;
        # left 0.5, right 0.25 -> mono 0.375 -> 12288.
        frames = struct.pack("<2f", 0.5, 0.25) * 6144
        dec.Feed(frames, len(frames))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0]), 4096, "a block is exactly 2048 samples")
        self.assertEqual(set(self._shorts(blocks[0])), {12288})

    def test_the_carry_crosses_buffer_boundaries(self):
        # The same 6144 frames, in two buffers of unequal length, produce
        # the identical block: the accumulator and the phase carry across
        # calls, so a buffer boundary is not a discontinuity.
        from NAudio.Wave import WaveFormatExtensible

        frames = struct.pack("<2f", 0.5, 0.25) * 6144
        whole, one = self._decimator()
        whole.Configure(WaveFormatExtensible(48000, 32, 2))
        whole.Feed(frames, len(frames))

        split, two = self._decimator()
        split.Configure(WaveFormatExtensible(48000, 32, 2))
        cut = 1000 * 8
        split.Feed(frames[:cut], cut)
        self.assertEqual(two, [], "a block arrived before 2048 samples were ready")
        split.Feed(frames[cut:], len(frames) - cut)
        self.assertEqual(two, one)

    def test_16_bit_pcm_is_read_as_integers_and_downmixed(self):
        from NAudio.Wave import WaveFormat

        dec, blocks = self._decimator()
        dec.Configure(WaveFormat(16000, 16, 2))            # no decimation
        frames = struct.pack("<2h", 1000, 3000) * 2048
        dec.Feed(frames, len(frames))
        self.assertEqual(set(self._shorts(blocks[0])), {2000})

    def test_32_bit_integer_pcm_is_not_read_as_float(self):
        # The audit's item 6: "float" used to be inferred from the width, so
        # a driver handing out 24-in-32 integer samples was decoded as
        # exponents. 0x40000000 is 0.5 of full scale as an integer and 2.0
        # as a float; read wrongly it clamps to 32767, read rightly it is
        # 16384.
        from NAudio.Wave import WaveFormat, WaveFormatEncoding

        dec, blocks = self._decimator()
        dec.Configure(WaveFormat.CreateCustomFormat(
            WaveFormatEncoding.Pcm, 16000, 1, 16000 * 4, 4, 32))
        frames = struct.pack("<i", 0x40000000) * 2048
        dec.Feed(frames, len(frames))
        self.assertEqual(set(self._shorts(blocks[0])), {16384})

    def test_out_of_range_samples_clamp_instead_of_wrapping(self):
        # 1.5 after a gain stage is ordinary; min(1, v) * 32767 on -1.5
        # wraps to a large positive value, a sign flip mid-word.
        from NAudio.Wave import WaveFormat

        dec, blocks = self._decimator()
        dec.Configure(WaveFormat.CreateIeeeFloatWaveFormat(16000, 1))
        frames = struct.pack("<f", 1.5) * 1024 + struct.pack("<f", -1.5) * 1024
        dec.Feed(frames, len(frames))
        shorts = self._shorts(blocks[0])
        self.assertEqual(set(shorts[:1024]), {32767})
        self.assertEqual(set(shorts[1024:]), {-32767})

    def test_formats_it_cannot_decode_are_refused_by_name(self):
        from System import InvalidOperationException
        from NAudio.Wave import WaveFormat, WaveFormatEncoding

        dec, _ = self._decimator()
        for fmt in (WaveFormat(48000, 24, 2),
                    WaveFormat.CreateCustomFormat(
                        WaveFormatEncoding.IeeeFloat, 48000, 1, 48000 * 8, 8, 64)):
            with self.assertRaises(InvalidOperationException) as caught:
                dec.Configure(fmt)
            self.assertIn(str(fmt.BitsPerSample) + "-bit", str(caught.exception))
            self.assertIn("32-bit float, 32-bit PCM and 16-bit PCM",
                          str(caught.exception))


class TopBar(unittest.TestCase):
    """The run and pause buttons: the glyph follows the word."""

    def test_the_glyphs_follow_the_words(self):
        # "Stop" sat under a play triangle and "Resume" under a pause bar for
        # as long as the panel has existed; the browser swaps both
        # (webui/app.js:642, :513). Read off the buttons themselves, because
        # the swap lives in the XAML and a view-model test would not see it.
        import System.Windows

        def glyphs():
            w = System.Windows.Application.Current.MainWindow
            return (str(w.FindName("BtnRun").Icon.Symbol),
                    str(w.FindName("BtnPause").Icon.Symbol))

        def put(running, paused):
            vm = main_vm()
            vm.Running = running
            vm.Paused = paused

        was = on_ui(lambda: (main_vm().Running, main_vm().Paused))
        try:
            on_ui(lambda: put(True, False))
            self.assertEqual(on_ui(glyphs), ("Stop24", "Pause24"))
            on_ui(lambda: put(True, True))
            self.assertEqual(on_ui(glyphs), ("Stop24", "Play24"))   # Resume
            on_ui(lambda: put(False, False))
            self.assertEqual(on_ui(glyphs), ("Play24", "Pause24"))
        finally:
            on_ui(lambda: put(*was))


if __name__ == "__main__":
    unittest.main()
