"""
A Stop and a Start are a new session, and the strategy has to agree.

Pipeline._build_strategy reuses the strategy object across a restart, which
is right - the settings behind it have not moved. But everything the object
is HOLDING belongs to the session that just ended, and nothing called
reset(). So a Start put the audio buffered at the moment of the Stop at the
front of the first new window - measured at 3.0 s carried across, making
that window straddle the gap - and, on the sliding window, matched the first
new caption against the text of a window it is not adjacent to.

The other half of these tests is the behaviour that must not leave with it.
configure() deliberately keeps both of those things when a setting moves
mid-session, because re-tuning a threshold must not cost you the sentence
being spoken. That is why the reset lives in start() and not in
_build_strategy, which _drain_pending calls too - so both directions are
checked here, and moving the call fails one of them.

No model, no server and no sound card: the backend and the capture source
are stubbed out, and audio goes into the strategy directly, which is what
the worker thread does with it anyway.
"""
import unittest
from unittest import mock

import numpy as np

import pipeline as pipeline_mod
import settings
from buffering import SAMPLERATE
from whisper_backends import Segment

# A real caption, from tests/fixtures/speech_sample.wav through
# whisper-server. Said once per session in the tests below, so the question
# "is this an overlap or did somebody repeat themselves" has a right answer.
LINE = "Silero decides whether this buffer contains speech."


def segments(*texts):
    """One window, in the shape ServerBackend returns."""
    return [Segment(text, "en", translated=False) for text in texts]


class RestartCase(unittest.TestCase):
    strategy_name = "sliding_window"

    def setUp(self):
        # The two builders that would touch hardware. Everything else in
        # start() - the gate, the strategy, the writer, the worker thread -
        # runs for real, because the ordering between them is the thing
        # under test.
        for name in ("_build_backend", "_build_source"):
            patcher = mock.patch.object(pipeline_mod.Pipeline, name,
                                        lambda self: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.events = []
        self.pipe = pipeline_mod.Pipeline(
            dict(settings.DEFAULTS, strategy=self.strategy_name,
                 vad=False, save=False),
            emit=lambda kind, data: self.events.append((kind, data)))
        self.addCleanup(self.pipe.stop)

    def feed(self, seconds):
        """Audio the strategy is holding, as if a Stop landed mid-sentence."""
        self.pipe.strategy.feed(
            np.zeros(int(seconds * SAMPLERATE), dtype=np.float32))

    def pending(self):
        return self.pipe.strategy.pending_seconds()

    def errors(self):
        return [d["msg"] for kind, d in self.events
                if kind == "log" and d["level"] == "error"]


class TheAudioLeftOver(RestartCase):
    def test_a_start_after_a_stop_drops_it(self):
        self.pipe.start()
        self.feed(3.0)
        self.assertAlmostEqual(self.pending(), 3.0)
        self.pipe.stop()
        # The Stop keeps it on purpose: stopping is not throwing away, and
        # a strategy with audio in it is how a paused session used to be
        # resumed. The Start is where it becomes stale.
        self.assertAlmostEqual(self.pending(), 3.0)
        self.pipe.start()
        self.assertAlmostEqual(self.pending(), 0.0)

    def test_it_is_reset_not_replaced(self):
        # The distinction matters: building a new strategy would also empty
        # the buffer, and would throw away anything else living on the
        # instance to get there. Reuse plus reset says what is meant.
        self.pipe.start()
        first = self.pipe.strategy
        self.feed(3.0)
        self.pipe.stop()
        self.pipe.start()
        self.assertIs(self.pipe.strategy, first)
        self.assertAlmostEqual(self.pending(), 0.0)

    def test_the_first_run_has_nothing_to_reset(self):
        # start() also runs once on a pipeline that has never started, where
        # the strategy is built two lines above the reset. Resetting a brand
        # new one has to be a quiet no-op, not an error.
        self.pipe.start()
        self.assertAlmostEqual(self.pending(), 0.0)
        self.assertEqual(self.errors(), [])

    def test_a_start_against_a_running_session_leaves_it_alone(self):
        # Two Starts in a row - a double click on the button, or a front end
        # that re-sends on reconnect. The early return in start() means the
        # second never reaches the reset, so it cannot empty the buffer
        # underneath a worker already reading from it.
        self.pipe.start()
        self.feed(3.0)
        self.assertTrue(self.pipe.alive())
        self.pipe.start()
        self.assertAlmostEqual(self.pending(), 3.0)

    def test_restart_resets_too(self):
        # restart() is stop() then start(), and the cases its own docstring
        # names are a device unplugged and a model swapped. Audio recorded
        # through the thing that was just replaced is the clearest stale
        # buffer there is.
        self.pipe.start()
        self.feed(3.0)
        self.pipe.restart()
        self.assertAlmostEqual(self.pending(), 0.0)

    def test_the_session_clock_keeps_running(self):
        # reset() drops the audio WITHOUT rewinding the consumed count, so
        # decisions after a restart carry timestamps that continue the
        # transcript instead of starting again at zero. The transcript
        # survives a restart, so its timestamps have to as well - two
        # captions at 00:00 in one file describe nothing.
        self.pipe.start()
        self.feed(3.0)
        self.pipe.restart()
        self.assertAlmostEqual(self.pipe.strategy.next().t_start, 3.0)


class TheTextOfTheLastWindow(RestartCase):
    """The sliding window's other piece of session state."""

    def test_a_start_forgets_it(self):
        self.pipe.start()
        self.pipe.strategy.filter(segments(LINE))
        self.assertTrue(self.pipe.strategy._prev_keys)
        self.pipe.stop()
        self.pipe.start()
        self.assertEqual(self.pipe.strategy._prev_keys, [])

    def test_the_first_caption_of_the_new_session_is_not_trimmed(self):
        # The payoff, and why forgetting is not merely tidy. Say the same
        # sentence again after a restart and it has to print in full: the
        # two windows are however long the stop lasted apart, so the words
        # they share are not an overlap to trim, they are a thing somebody
        # said twice. Left in place, the whole caption disappears.
        self.pipe.start()
        kept, _dropped = self.pipe.strategy.filter(segments(LINE))
        self.assertEqual([s.text for s in kept], [LINE])
        self.pipe.stop()
        self.pipe.start()
        kept, dropped = self.pipe.strategy.filter(segments(LINE))
        self.assertEqual([s.text for s in kept], [LINE])
        self.assertEqual(dropped, [])


class WhatAMidSessionChangeKeeps(RestartCase):
    """
    The same two things from the other side.

    These are what would break if the reset moved into _build_strategy: a
    rebuild that is not a new session must keep the audio and the history,
    because the previous window is still adjacent to the next one.
    """

    def test_an_explicit_rebuild_keeps_the_audio_and_the_history(self):
        self.pipe._build_strategy()
        self.feed(3.0)
        self.pipe.strategy.filter(segments(LINE))
        self.pipe.rebuild("strategy")
        self.assertAlmostEqual(self.pending(), 3.0)
        self.assertTrue(self.pipe.strategy._prev_keys)

    def test_a_settings_patch_keeps_them_too(self):
        # Nudging the speech gate rebuilds the strategy alongside it, since
        # the strategy holds the gate. Somebody watching the meter and
        # moving a threshold is mid-sentence by definition.
        self.pipe._build_strategy()
        self.feed(3.0)
        self.pipe.strategy.filter(segments(LINE))
        applied, errors = self.pipe.apply({"vad_threshold": 0.3})
        self.assertEqual(errors, [])
        self.assertEqual(applied, {"vad_threshold": 0.3})
        self.pipe._drain_pending()
        self.assertAlmostEqual(self.pending(), 3.0)
        self.assertTrue(self.pipe.strategy._prev_keys)


class TheOtherStrategy(RestartCase):
    """
    silence_at_end_of_chunk holds a different second thing, and start()
    has to reach that one too - so the call is strategy.reset(), not the
    base class's, and not an inlined buffer clear.
    """

    strategy_name = "silence_at_end_of_chunk"

    def test_a_start_drops_the_audio_and_disarms_the_retry(self):
        self.pipe.start()
        self.feed(8.0)
        # Where the strategy had decided to look again for the end of the
        # talking. Against an emptied buffer it means "wait for 8.40 s of
        # audio that no longer exists".
        self.pipe.strategy._retry_at = int(8.4 * SAMPLERATE)
        self.pipe.stop()
        self.pipe.start()
        self.assertAlmostEqual(self.pending(), 0.0)
        self.assertEqual(self.pipe.strategy._retry_at, 0)

    def test_it_has_no_dedup_history_to_forget(self):
        # Its windows are cut in the gaps and never overlap, so it runs no
        # duplicate filter at all. Named here so that a reset() reaching for
        # _prev_keys on every strategy fails instead of passing quietly.
        self.pipe.start()
        self.assertFalse(hasattr(self.pipe.strategy, "_prev_keys"))
        self.assertEqual(self.pipe.strategy.filter(segments(LINE))[1], [])


if __name__ == "__main__":
    unittest.main()
