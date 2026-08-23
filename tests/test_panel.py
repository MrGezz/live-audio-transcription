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


def _make_app_class():
    """soak's engine stand-in with app.py's own handlers bound onto it.

    Built here rather than at import, so that discovering the suite does not
    import app.py (and the audio stack under it) on a machine that will skip.
    """
    import app as app_mod
    import soak

    class _Pipe(soak._SoakApp._P):
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

    for name in ("_safe_preset_name", "_list_presets", "_preset_file",
                 "_preset_save", "_preset_load", "_preset_delete",
                 "_broadcast_presets", "_notify", "_on_event",
                 "_overlay_style", "_sync_overlay", "_sync_panel_theme",
                 "_panel_dark"):
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
    global APP, PANEL, TMP, _SAVED_DIRS
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


if __name__ == "__main__":
    unittest.main()
