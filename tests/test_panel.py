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
Skips itself otherwise, as test_real_model.py skips without the model. The
window opens on screen for a few seconds:

    .venv\\Scripts\\python.exe -m unittest tests.test_panel -v
"""
import json
import os
import shutil
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

        def apply(self, patch, remote=False):
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
        # Found by its dynamic source, not by key: `model` is special-cased by
        # name in FieldTemplateSelector because it is declared a path, and
        # server_model exists partly to show that a new list does not have to
        # be. If this ever needs a name test instead, the declaration drifted.
        self.assertEqual(_field("server_model", "ChoiceSource"), "ggml_models")
        self.assertEqual([v for v, available in self._choices() if available],
                         self._ggml())

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


if __name__ == "__main__":
    unittest.main()
