"""
apply(drain=False): the rebuild moves, the ack does not.

With the session stopped, Pipeline.apply drains its own patch inline - there
is no worker to do it. For the socket that is fine; for the desktop panel it
put a CPU model load on the WPF dispatcher, because IEngineBridge.ApplySettings
runs there and needs the (applied, errors) ack synchronously. drain=False keeps
the ack synchronous and leaves the REBUILD queued for drain_pending(), which
wpf_panel hands to App._lifecycle. start() clears that queue, because it
rebuilds everything from the settings anyway.

No model, no audio: the two builders that would touch hardware are counted
stand-ins, everything else in start() runs for real.

    .venv\\Scripts\\python.exe -m unittest tests.test_apply_drain -v
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline as pipeline_mod  # noqa: E402
import settings  # noqa: E402


class DrainCase(unittest.TestCase):

    def setUp(self):
        self.built = {"backend": 0, "source": 0}

        def counter(name):
            def build(pipe):
                self.built[name] += 1
            return build

        for name in ("backend", "source"):
            patcher = mock.patch.object(pipeline_mod.Pipeline,
                                        "_build_" + name, counter(name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.events = []
        self.pipe = pipeline_mod.Pipeline(
            dict(settings.DEFAULTS, vad=False, save=False),
            emit=lambda kind, data: self.events.append(kind))
        self.addCleanup(self.pipe.stop)

    def apply(self, patch, **kw):
        del self.events[:]
        return self.pipe.apply(patch, **kw)


class WithTheSessionStopped(DrainCase):

    def test_the_default_still_drains_inline(self):
        # The socket's path, unchanged: the rebuild happens inside apply().
        changed, errors = self.apply({"backend": "local"})
        self.assertEqual((changed, errors), ({"backend": "local"}, []))
        self.assertEqual(self.built["backend"], 1)
        self.assertIn("state", self.events)
        self.assertFalse(self.pipe.rebuild_pending())

    def test_drain_false_answers_now_and_rebuilds_later(self):
        changed, errors = self.apply({"backend": "local"}, drain=False)

        # The synchronous half: validated, applied, echoed - the ack the
        # panel's snap-back depends on is complete before this returns.
        self.assertEqual((changed, errors), ({"backend": "local"}, []))
        self.assertEqual(self.pipe.settings["backend"], "local")
        self.assertIn("settings", self.events)

        # The slow half did NOT run on this thread.
        self.assertEqual(self.built["backend"], 0)
        self.assertNotIn("state", self.events)
        self.assertTrue(self.pipe.rebuild_pending())

        del self.events[:]
        self.pipe.drain_pending()
        self.assertEqual(self.built["backend"], 1)
        self.assertIn("state", self.events)
        self.assertFalse(self.pipe.rebuild_pending())

    def test_a_patch_that_rebuilds_nothing_is_drained_whatever_drain_says(self):
        # Hopping to the lifecycle slot for an attribute write would cost a
        # busy flip on the panel for nothing, so apply keeps those inline.
        changed, errors = self.apply({"confidence_warn": 0.5}, drain=False)
        self.assertEqual(changed, {"confidence_warn": 0.5})
        self.assertEqual(errors, [])
        self.assertFalse(self.pipe.rebuild_pending())
        self.assertEqual(self.built, {"backend": 0, "source": 0})

    def test_a_refused_patch_leaves_nothing_queued(self):
        changed, errors = self.apply({"buffer": 99}, drain=False)
        self.assertEqual(changed, {})
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.pipe.rebuild_pending())

    def test_drain_pending_with_nothing_queued_is_a_no_op(self):
        del self.events[:]
        self.pipe.drain_pending()
        self.assertEqual(self.events, [])
        self.assertEqual(self.built, {"backend": 0, "source": 0})


class WhenStartFollows(DrainCase):

    def test_start_builds_the_backend_once_and_clears_the_queue(self):
        # The drain that _lifecycle refused: queued, never run, then Start.
        self.apply({"backend": "local"}, drain=False)
        self.assertTrue(self.pipe.rebuild_pending())

        self.pipe.start()
        try:
            self.assertEqual(self.built["backend"], 1)
            self.assertFalse(self.pipe.rebuild_pending())
            # Give the worker a few iterations: a queue left in place would
            # have it rebuild the backend again at the top of its first one.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(self.built["backend"], 1)
            self.assertEqual(self.built["source"], 1)
        finally:
            self.pipe.stop()


class WhileRunning(DrainCase):

    def test_drain_false_changes_nothing_the_worker_does_not_already_do(self):
        # Alive: the worker drains at the top of its next iteration either
        # way, so rebuild_pending is False and drain_pending has no caller.
        self.pipe.start()
        try:
            changed, errors = self.apply({"backend": "local"}, drain=False)
            self.assertEqual((changed, errors), ({"backend": "local"}, []))
            self.assertFalse(self.pipe.rebuild_pending())
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and self.built["backend"] < 2:
                time.sleep(0.05)
            self.assertEqual(self.built["backend"], 2)     # start, then the worker
        finally:
            self.pipe.stop()


if __name__ == "__main__":
    unittest.main()
