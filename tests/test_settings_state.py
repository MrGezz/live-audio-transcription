"""
Remembered state: the one value that outlives a run without being a preset.

settings.py is otherwise a pure function of defaults, a named preset and the
flags someone typed. server_model breaks that on purpose - whisper-server reads
its model once, at startup, so the panel picking one is not "change a number
the running engine will pick up" but "launch a different program" - and the
rule that keeps the exception honest is the precedence: remembered state sits
directly above the defaults and below anything anyone actually asked for.

Needs no WPF, no model and no audio, so unlike tests/test_panel.py this half
runs on every machine.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import settings as settings_mod  # noqa: E402


class StateFile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="state-test-")
        self.path = os.path.join(self.tmp, "_state.json")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_only_the_allow_listed_keys_are_written(self):
        # The allow-list is the whole reason this is not a settings file. If a
        # future key wants remembering it has to be added to REMEMBERED
        # deliberately, rather than arriving because someone changed it once.
        written = settings_mod.save_state(
            {"server_model": "ggml-large-v3-turbo-q8_0.bin",
             "backend": "local", "buffer": 8}, self.path)
        self.assertTrue(written)
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["server_model"], "ggml-large-v3-turbo-q8_0.bin")
        self.assertNotIn("backend", data)
        self.assertNotIn("buffer", data)

    def test_nothing_to_remember_writes_no_file(self):
        self.assertFalse(settings_mod.save_state({"buffer": 8}, self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_a_round_trip_returns_what_went_in(self):
        settings_mod.save_state({"server_model": "ggml-small-q8_0.bin"},
                                self.path)
        self.assertEqual(settings_mod.load_state(self.path),
                         {"server_model": "ggml-small-q8_0.bin"})

    def test_the_comment_does_not_come_back_as_a_setting(self):
        settings_mod.save_state({"server_model": "ggml-small-q8_0.bin"},
                                self.path)
        with open(self.path, "r", encoding="utf-8") as fh:
            self.assertIn("_comment", json.load(fh))
        self.assertNotIn("_comment", settings_mod.load_state(self.path))

    def test_a_missing_or_broken_file_is_not_an_error(self):
        # First run is the missing case, and it is by far the most common, so
        # it stays silent and empty rather than doing what load_preset does -
        # that one was named on a command line by someone who meant it.
        self.assertEqual(settings_mod.load_state(self.path), {})
        for junk in ("", "{", "null", "[1, 2]", '"a string"'):
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(junk)
            self.assertEqual(settings_mod.load_state(self.path), {}, junk)

    def test_an_unknown_key_in_the_file_is_dropped(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"server_model": "ggml-base-q5_1.bin",
                       "not_a_setting": 1}, fh)
        self.assertEqual(settings_mod.load_state(self.path),
                         {"server_model": "ggml-base-q5_1.bin"})


class Precedence(unittest.TestCase):
    """defaults < remembered < preset < flags actually typed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="state-prec-")
        self.saved = settings_mod.STATE_FILE
        settings_mod.STATE_FILE = os.path.join(self.tmp, "_state.json")
        self.preset = os.path.join(self.tmp, "p.json")

    def tearDown(self):
        import shutil

        settings_mod.STATE_FILE = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _settings(self, argv):
        parser = settings_mod.build_parser("test")
        settings, _ = settings_mod.from_args(parser, argv)
        return settings

    def _remember(self, name):
        settings_mod.save_state({"server_model": name})

    def test_with_nothing_remembered_the_default_stands(self):
        self.assertEqual(self._settings([])["server_model"],
                         settings_mod.DEFAULTS["server_model"])

    def test_remembered_beats_the_default(self):
        self._remember("ggml-large-v3-turbo-q8_0.bin")
        self.assertEqual(self._settings([])["server_model"],
                         "ggml-large-v3-turbo-q8_0.bin")

    def test_a_preset_beats_what_was_remembered(self):
        # Naming a preset is a statement about THIS run, so it has to win over
        # what the panel happened to be doing last time - otherwise a profile
        # could never move the model at all.
        self._remember("ggml-large-v3-turbo-q8_0.bin")
        with open(self.preset, "w", encoding="utf-8") as fh:
            json.dump({"server_model": "ggml-small-q8_0.bin"}, fh)
        got = self._settings(["--preset", self.preset])
        self.assertEqual(got["server_model"], "ggml-small-q8_0.bin")

    def test_a_typed_flag_beats_everything(self):
        self._remember("ggml-large-v3-turbo-q8_0.bin")
        with open(self.preset, "w", encoding="utf-8") as fh:
            json.dump({"server_model": "ggml-small-q8_0.bin"}, fh)
        got = self._settings(["--preset", self.preset,
                              "--server-model", "ggml-base-q5_1.bin"])
        self.assertEqual(got["server_model"], "ggml-base-q5_1.bin")

    def test_remembering_does_not_disturb_any_other_setting(self):
        # The failure this guards against is a state file quietly becoming a
        # settings file: if load_state ever returned more than REMEMBERED, a
        # stale value would start overriding defaults nobody asked it to.
        self._remember("ggml-small-q8_0.bin")
        got = self._settings([])
        for key, value in settings_mod.DEFAULTS.items():
            if key != "server_model":
                self.assertEqual(got[key], value, key)


class EngineRebuildTag(unittest.TestCase):
    """`engine` is a tag the pipeline reports and then deliberately drops."""

    def test_the_tag_is_in_the_documented_set(self):
        # REBUILDS validates nothing - it is the comment block's machine-
        # readable half - so a tag missing from it fails silently as
        # documentation rather than as code. Assert it instead.
        self.assertIn("engine", settings_mod.REBUILDS)
        self.assertEqual(
            settings_mod.BY_KEY["server_model"].rebuild, "engine")

    def test_changing_the_model_asks_for_engine_and_nothing_else(self):
        self.assertEqual(
            settings_mod.rebuilds_for({"server_model": "a.bin"},
                                      {"server_model": "b.bin"}),
            {"engine"})

    def test_the_pipeline_tears_nothing_down_for_it(self):
        # Asserted on the EMITTED events, not on pipe._rebuild: _drain_pending
        # empties that set on its way through, so it reads as empty whether or
        # not the tag was discarded and would pass against the bug. What
        # actually differs is the extra "state" document - the tag matches no
        # branch in _drain_pending, but left in the set it still makes
        # `if rebuild:` true and pushes state for a rebuild that never
        # happened. Measured: an undiscarded no-op tag emits
        # ['state', 'settings', 'log'] where this emits ['settings', 'log'].
        import pipeline as pipeline_mod

        events = []
        pipe = pipeline_mod.Pipeline(
            {}, emit=lambda kind, data: events.append(kind))
        changed, errors = pipe.apply({"server_model": "ggml-small-q8_0.bin"})

        self.assertEqual(errors, [])
        self.assertEqual(changed, {"server_model": "ggml-small-q8_0.bin"})
        self.assertEqual(pipe.settings["server_model"], "ggml-small-q8_0.bin")
        self.assertNotIn("state", events)


class PresetContents(unittest.TestCase):
    """
    What belongs in a saved profile, and what was getting in by accident.

    A preset is a TRANSCRIPTION profile. `wpf` is "is the desktop panel open"
    - not a property of a profile, and not something loading one should be
    able to change. It ended up in every preset saved from the panel anyway,
    because save_preset stored every non-default value and `wpf` is true
    exactly when the panel is running. The visible result was an error toast
    on the user's own saved profile, every time they loaded it:
    "One setting in 'aaa' was refused - 'wpf' can only be set when starting
    the program or from the desktop panel."
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="preset-")
        self.path = os.path.join(self.tmp, "profile.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def saved(self, **overrides):
        values = dict(settings_mod.DEFAULTS)
        values.update(overrides)
        settings_mod.save_preset(self.path, values)
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.pop("_comment", None)
        return data

    def test_the_launch_time_keys_are_left_out(self):
        # Every REMOTE_LOCKED key, not just wpf: they are all refused on the
        # way back in, so storing any of them writes a preset that cannot
        # load cleanly.
        data = self.saved(wpf=True, wpf_theme="light", web=True,
                          web_port=9999, output="somewhere.txt")
        for key in settings_mod.REMOTE_LOCKED:
            self.assertNotIn(key, data, key)

    def test_the_profile_settings_are_still_written(self):
        # The failure mode of over-filtering: a preset that saves nothing.
        data = self.saved(wpf=True, translate=True, buffer=12,
                          server_model="ggml-large-v3.bin")

        self.assertEqual(data, {"translate": True, "buffer": 12,
                                "server_model": "ggml-large-v3.bin"})

    def test_a_profile_of_only_launch_keys_saves_empty_rather_than_broken(self):
        self.assertEqual(self.saved(wpf=True, web=True), {})


if __name__ == "__main__":
    unittest.main()
