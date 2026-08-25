"""
Worker-loop errors must not suppress the first transcription failure.

pipeline.py used to share _consecutive_errors between two unrelated failure
sources: the worker loop's catch-all (first 3, then every 25th) and the
transcription call (first 3, then every 10th). Because they shared the counter,
worker-loop errors inflated it and swallowed the first transcription failure.
Reproduced: 3 worker errors, then the server dies, and the first "Transcription
failed" line does not appear until the 7th consecutive failed window (counter
reaches 10). At the shipped --slide 2 that is ~14 s of captionless, silent
operation with nothing in the log.

This is the regression test that would have caught the bug. Do not weaken the
two-counter design or the rate thresholds: they are what keep each layer's
errors visible on their own timeline.

No model, no server and no sound card: the backend is stubbed out to raise on
demand, and audio does not flow.
"""
import unittest
from unittest import mock

import numpy as np

import pipeline as pipeline_mod
import settings
from buffering import Decision


class FakeBackend(object):
    """A backend that raises on transcribe() if told to."""

    def __init__(self, fail_count=0):
        self.fail_count = fail_count
        self.call_count = 0

    def transcribe(self, audio, language=None, task=None, translate=False,
                   options=None):
        self.call_count += 1
        if self.call_count <= self.fail_count:
            raise RuntimeError("Backend failed (test-injected)")
        return []

    def close(self):
        pass


class ErrorBackoffCase(unittest.TestCase):
    """Test error logging with separate worker and transcription counters."""

    def setUp(self):
        # Stub out the hardware-touching builders.
        for name in ("_build_backend", "_build_source"):
            patcher = mock.patch.object(pipeline_mod.Pipeline, name,
                                        lambda self: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.events = []
        self.pipe = pipeline_mod.Pipeline(
            dict(settings.DEFAULTS, vad=False, save=False),
            emit=lambda kind, data: self.events.append((kind, data)))
        self.addCleanup(self.pipe.stop)

    def errors(self):
        """All error log lines emitted during this test."""
        return [d["msg"] for kind, d in self.events
                if kind == "log" and d["level"] == "error"]

    def transcription_errors(self):
        """Error log lines from transcription failures only."""
        return [msg for msg in self.errors()
                if msg.startswith("Transcription failed")]

    def worker_errors(self):
        """Error log lines from worker loop only."""
        return [msg for msg in self.errors()
                if msg.startswith("Worker error")]

    def test_first_three_transcription_failures_each_log(self):
        """The first 3 consecutive transcription failures each appear in the log."""
        self.pipe.backend = FakeBackend(fail_count=3)
        self.pipe.start()

        # Inject 3 transcription failures.
        for i in range(3):
            decision = Decision('transcribe', audio=np.zeros(16000, dtype=np.float32),
                              duration=1.0)
            self.pipe._transcribe(decision)

        # All 3 should have logged.
        transcription_errors = self.transcription_errors()
        self.assertEqual(len(transcription_errors), 3)
        self.assertIn("1 in a row", transcription_errors[0])
        self.assertIn("2 in a row", transcription_errors[1])
        self.assertIn("3 in a row", transcription_errors[2])

    def test_fourth_through_ninth_failures_do_not_log(self):
        """Failures 4 through 9 are suppressed (backoff policy)."""
        self.pipe.backend = FakeBackend(fail_count=9)
        self.pipe.start()

        # Inject 9 transcription failures.
        for i in range(9):
            decision = Decision('transcribe', audio=np.zeros(16000, dtype=np.float32),
                              duration=1.0)
            self.pipe._transcribe(decision)

        # Only the first 3 should have logged.
        transcription_errors = self.transcription_errors()
        self.assertEqual(len(transcription_errors), 3)

    def test_tenth_failure_logs(self):
        """The 10th consecutive failure logs (backoff pattern: every 10th)."""
        self.pipe.backend = FakeBackend(fail_count=10)
        self.pipe.start()

        # Inject 10 transcription failures.
        for i in range(10):
            decision = Decision('transcribe', audio=np.zeros(16000, dtype=np.float32),
                              duration=1.0)
            self.pipe._transcribe(decision)

        # The first 3 plus the 10th should have logged.
        transcription_errors = self.transcription_errors()
        self.assertEqual(len(transcription_errors), 4)
        self.assertIn("10 in a row", transcription_errors[3])

    def test_success_resets_counter(self):
        """A successful transcription resets the counter so the next failure logs."""
        self.pipe.backend = FakeBackend(fail_count=2)
        self.pipe.start()

        # Fail twice.
        for i in range(2):
            decision = Decision('transcribe', audio=np.zeros(16000, dtype=np.float32),
                              duration=1.0)
            self.pipe._transcribe(decision)

        # Succeed once.
        self.pipe.backend.fail_count = 0
        decision = Decision('transcribe', audio=np.zeros(16000, dtype=np.float32),
                          duration=1.0)
        self.pipe._transcribe(decision)

        # Fail once more - should log because counter was reset.
        self.pipe.backend.fail_count = 10
        decision = Decision('transcribe', audio=np.zeros(16000, dtype=np.float32),
                          duration=1.0)
        self.pipe._transcribe(decision)

        # Should have logged: initial 2 failures + 1 after reset.
        transcription_errors = self.transcription_errors()
        self.assertEqual(len(transcription_errors), 3)
        self.assertIn("1 in a row", transcription_errors[2])

    def test_worker_errors_do_not_suppress_first_transcription_failure(self):
        """
        REGRESSION: worker-loop errors must not silence the first transcription
        failure.

        This drives the real _run() except block rather than restating its
        logging rule here. Restating it is what makes the test useless: it
        would pass with the shared counter put back, because the assertion
        would be checking the test's own copy of the rule rather than the
        loop's. The load-bearing assertion is that _consecutive_errors is
        still 0 after the worker loop has failed three times.
        """
        self.pipe.backend = FakeBackend(fail_count=1)

        ticks = []

        def failing_tick(pipe):
            ticks.append(1)
            if len(ticks) >= 3:
                pipe._stop.set()
            raise RuntimeError("worker boom (test-injected)")

        with mock.patch.object(pipeline_mod.Pipeline, "_tick", failing_tick),                 mock.patch.object(pipeline_mod.Pipeline, "_drain_pending",
                                  lambda pipe: None):
            self.pipe._run()

        # The real loop logged all three, on its own counter.
        self.assertEqual(len(self.worker_errors()), 3)
        # ...and left the transcription counter alone. With the counters shared
        # again this is 3, and the assertion below is the ~14 s of silence.
        self.assertEqual(self.pipe._consecutive_errors, 0)

        self.pipe._transcribe(Decision(
            "transcribe", audio=np.zeros(16000, dtype=np.float32), duration=1.0))

        transcription_errors = self.transcription_errors()
        self.assertEqual(len(transcription_errors), 1)
        self.assertIn("1 in a row", transcription_errors[0])


if __name__ == "__main__":
    unittest.main()
