"""
REMOTE_LOCKED: what a browser may not set, and why each key is on the list.

This existed as a claim before it existed as code. settings.py's comment said a
page "must not be able to point --model at an arbitrary path", Pipeline.apply's
docstring said remote=True "refuses ... the model path", and README.md promised
the same thing to users - while `model` was not in the tuple at all. Three
documents asserting a control that was not there is worse than no control,
because it is the state in which nobody goes looking.

So these tests assert the LIST, not just the mechanism. A key silently leaving
REMOTE_LOCKED would otherwise reproduce exactly the situation being fixed.

Needs no WPF, no model, no audio and no network.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline as pipeline_mod  # noqa: E402
import settings as settings_mod  # noqa: E402


class TheList(unittest.TestCase):

    def test_the_listener_and_the_host_window_are_locked(self):
        for key in ("web", "web_host", "web_port", "web_token", "web_open",
                    "wpf", "wpf_theme"):
            self.assertIn(key, settings_mod.REMOTE_LOCKED, key)

    def test_every_path_that_is_the_only_route_to_its_sink_is_locked(self):
        # model     -> faster-whisper's download_model() for any non-directory
        #              string, i.e. an outbound fetch and a weight load
        # vad_model -> the tree's only onnxruntime.InferenceSession
        # output    -> the tree's only makedirs + open for writing
        for key in ("model", "vad_model", "output"):
            self.assertIn(key, settings_mod.REMOTE_LOCKED, key)

    def test_file_path_is_deliberately_not_locked(self):
        # Not an oversight, and this test is here so that "add it for
        # consistency" has to argue with something. Locking file_path would
        # disable capture="file" in the browser while removing NO capability:
        # the benchmark command carries its own wav path from the socket into
        # wave.open (app.py _run_benchmark -> Pipeline.benchmark ->
        # benchmark.load_wav) and never passes through Pipeline.apply, so
        # REMOTE_LOCKED cannot reach it. Read paths are constrained by SHAPE
        # at the sink instead, which reaches both doors - see safe_paths.py,
        # and tests/test_safe_paths.py for the half this file cannot tell.
        self.assertNotIn("file_path", settings_mod.REMOTE_LOCKED)

    def test_server_model_is_not_locked_because_it_is_checked_at_the_sink(self):
        # app.py _engine_start basenames it and then checks membership against
        # the real _list_models() listing before it can reach the launcher, so
        # the value has no sink to be dangerous at. Locking it would only cost
        # the preset round trip.
        self.assertNotIn("server_model", settings_mod.REMOTE_LOCKED)


class TheRefusal(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="locked-")
        # cwd moves into the temp tree for the whole class. Turning `save` on
        # builds a real writer, and with no `output` set transcript.py
        # auto-names it in the CURRENT directory - which, run from a checkout,
        # drops a transcript_YYYYmmdd_HHMMSS.txt in the repo every time the
        # suite runs. Gitignored, so it never shows up as a diff to notice.
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self.events = []
        self.pipe = pipeline_mod.Pipeline(
            {}, emit=lambda kind, data: self.events.append(kind))

    def tearDown(self):
        writer = getattr(self.pipe, "writer", None)
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_browser_patch_is_refused_and_changes_nothing(self):
        before = self.pipe.settings["model"]
        changed, errors = self.pipe.apply(
            {"model": "attacker/faster-whisper-backdoor"}, remote=True)

        self.assertEqual(changed, {})
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("model", errors[0])
        self.assertEqual(self.pipe.settings["model"], before)

    def test_the_refusal_names_where_it_CAN_be_set(self):
        # The old wording said "not from the browser", which became wrong the
        # moment _preset_load started routing preset files through the same
        # remote=True path. Both callers are covered by naming the two places
        # that still work instead of the one that does not.
        _, errors = self.pipe.apply({"output": "x.txt"}, remote=True)
        self.assertIn("starting the program", errors[0])
        self.assertIn("desktop panel", errors[0])

    def test_a_locked_key_never_reaches_the_filesystem(self):
        # The sink for `output` is transcript.py's makedirs+open. Asserting the
        # directory was not created, rather than only that the patch errored,
        # because the ordering is the actual control: Pipeline.apply pops
        # locked keys BEFORE validate(), and validate() itself touches the
        # filesystem for other keys.
        evil = os.path.join(self.tmp, "created-by-a-browser")
        target = os.path.join(evil, "transcript.txt")
        self.pipe.apply({"save": True}, remote=False)
        self.events[:] = []

        changed, errors = self.pipe.apply({"output": target}, remote=True)

        self.assertNotIn("output", changed)
        self.assertTrue(errors)
        self.assertFalse(os.path.exists(evil),
                         "a refused patch created {0}".format(evil))

    def test_the_desktop_panel_is_not_restricted(self):
        # wpf_panel.py passes remote=False deliberately: a local panel forbidden
        # from choosing where its own transcript lands would be obeying a rule
        # written for a different threat. If this ever fails, the lock has been
        # applied at the wrong layer.
        #
        # `output` rather than `model` on purpose. Both are locked and either
        # would prove the point, but model carries rebuild="backend", so
        # accepting it rebuilds the backend for real - which reaches out to
        # whisper-server, waits for the connection to be refused and logs a
        # fallback. A test that asserts a dictionary should not open a socket.
        local = os.path.join(self.tmp, "mine.txt")
        changed, errors = self.pipe.apply({"output": local}, remote=False)

        self.assertEqual(errors, [])
        self.assertEqual(changed, {"output": local})
        self.assertEqual(self.pipe.settings["output"], local)

    def test_locking_leaves_the_rest_of_the_patch_alone(self):
        # One refused key must not lose the caller its other nineteen good
        # ones - the same rule validate() follows for unknown keys.
        changed, errors = self.pipe.apply(
            {"model": "attacker/x", "buffer": 8}, remote=True)

        self.assertEqual(changed.get("buffer"), 8)
        self.assertNotIn("model", changed)
        self.assertEqual(len(errors), 1, errors)


if __name__ == "__main__":
    unittest.main()
