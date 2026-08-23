"""
The gate's state machine, its end-of-buffer rules and its retuning, on
scripted probabilities. See fakes.py for how a script becomes audio.
"""
import inspect
import unittest

import settings
from speech_gate import SpeechGate
from tests.fakes import fake_gate, script, FRAME, SPEECH, QUIET

F = FRAME


class FramesFor(unittest.TestCase):
    def test_upstream_arithmetic(self):
        # Whole frames, and round() sends 12.5 to even: the 400 ms default
        # is 12 frames, closing on the 13th quiet one (416 ms), not 13.
        for ms, frames in ((100, 3), (160, 5), (250, 8), (400, 12),
                           (500, 16), (2000, 62)):
            with self.subTest(ms=ms):
                self.assertEqual(SpeechGate._frames_for(ms), frames)

    def test_never_zero(self):
        self.assertEqual(SpeechGate._frames_for(0), 1)


class Defaults(unittest.TestCase):
    """
    settings.py and create() must agree. They did not: create() still said
    160 ms after the default was moved to 400, and because pipeline.py never
    passed the value, 160 is what actually ran.
    """

    def test_create_matches_the_schema(self):
        params = inspect.signature(SpeechGate.create).parameters
        for key, arg in (("vad_threshold", "threshold"),
                         ("vad_min_speech_ms", "min_speech_ms"),
                         ("vad_neg_threshold", "neg_threshold"),
                         ("vad_min_silence_ms", "min_silence_ms")):
            with self.subTest(key=key):
                self.assertEqual(settings.DEFAULTS[key], params[arg].default)


class MinSilence(unittest.TestCase):
    """A gap ends speech only once it has lasted min_silence_ms."""

    def test_a_breath_is_not_a_boundary(self):
        audio = script((30 * F, SPEECH), (10 * F, QUIET),
                       (30 * F, SPEECH), (20 * F, QUIET))
        # 400 ms is 12 frames: a 10-frame gap is a breath.
        self.assertEqual(len(fake_gate(min_silence_ms=400)
                             .analyze(audio).segments), 1)
        # 160 ms is 5: the same gap is the end of a sentence.
        self.assertEqual(len(fake_gate(min_silence_ms=160)
                             .analyze(audio).segments), 2)

    def test_closes_on_the_frame_after_min_silence_frames(self):
        # Upstream's arithmetic, counted from the frame after the one the
        # gap started on: 5 frames close on the 6th quiet frame, 12 on the
        # 13th. The smallest gap that splits is therefore frames + 1.
        for ms, frames in ((160, 5), (400, 12)):
            with self.subTest(ms=ms):
                gate = fake_gate(min_silence_ms=ms)
                splits_at = None
                for gap in range(1, 20):
                    audio = script((20 * F, SPEECH), (gap * F, QUIET),
                                   (20 * F, SPEECH), (20 * F, QUIET))
                    if len(gate.analyze(audio).segments) == 2:
                        splits_at = gap
                        break
                self.assertEqual(splits_at, frames + 1)

    def test_segment_ends_where_the_quiet_began(self):
        audio = script((30 * F, SPEECH), (20 * F, QUIET))
        r = fake_gate(min_silence_ms=160).analyze(audio)
        self.assertAlmostEqual(r.segments[0][1], 30 * F)
        self.assertAlmostEqual(r.last_speech_end, 30 * F)


class Hysteresis(unittest.TestCase):
    def test_zero_derives_the_end_threshold_and_keeps_following(self):
        gate = fake_gate(threshold=0.5, neg_threshold=0.0)
        self.assertAlmostEqual(gate.neg_threshold, 0.35)
        gate.configure(threshold=0.3)           # derived value moves with it
        self.assertAlmostEqual(gate.neg_threshold, 0.15)
        self.assertAlmostEqual(fake_gate(threshold=0.1).neg_threshold, 0.01)
        self.assertAlmostEqual(fake_gate(threshold=0.5, neg_threshold=0.4)
                               .neg_threshold, 0.4)

    def test_a_dip_into_the_band_does_not_end_speech(self):
        # Twenty frames at 0.4: under the 0.5 start threshold, over the
        # derived 0.35 end threshold.
        audio = script((20 * F, SPEECH), (20 * F, 0.4),
                       (20 * F, SPEECH), (20 * F, QUIET))
        hysteresis = fake_gate(threshold=0.5, neg_threshold=0.0,
                               min_silence_ms=160).analyze(audio)
        self.assertEqual(len(hysteresis.segments), 1)
        # Collapse the band and the same dip reads as the end of a sentence.
        single = fake_gate(threshold=0.5, neg_threshold=0.5,
                           min_silence_ms=160).analyze(audio)
        self.assertEqual(len(single.segments), 2)
        # Either way the band frames are not counted as speech: .frames is
        # the strict count every --vad-min-speech-ms was chosen against.
        self.assertEqual(hysteresis.frames, 40)


class EndOfBuffer(unittest.TestCase):
    """
    What last_speech_end says when the audio stops before the talker does.
    These are the rules silence_at_end_of_chunk cuts by - see the module
    docstring, "What the end of the buffer means".
    """

    def test_an_open_run_ends_at_the_buffer_edge(self):
        r = fake_gate().analyze(script((30 * F, SPEECH)))
        self.assertEqual(r.segments, [(0.0, r.duration)])
        self.assertEqual(r.last_speech_end, r.duration)

    def test_a_pending_silence_shorter_than_min_silence_keeps_it_open(self):
        # Eight quiet frames, 256 ms, under the 416 the gate was told to
        # wait: by its own rule the talker has not stopped.
        audio = script((30 * F, SPEECH), (8 * F, QUIET))
        r = fake_gate(min_silence_ms=400).analyze(audio)
        self.assertEqual(r.last_speech_end, r.duration,
                         "a breath was reported as the end of speech")
        # Told to wait 160 ms, the same gap closes it where the quiet began.
        r = fake_gate(min_silence_ms=160).analyze(audio)
        self.assertAlmostEqual(r.last_speech_end, 30 * F)

    def test_min_silence_decides_when_the_talker_stopped(self):
        # Grow the quiet tail a frame at a time, as live capture does: the
        # run closes on exactly the frame after min_silence_frames, and not
        # one frame sooner whatever chunk_offset would have accepted.
        for ms, frames in ((160, 5), (400, 12), (800, 25)):
            with self.subTest(ms=ms):
                gate = fake_gate(min_silence_ms=ms)
                closed_at = None
                for quiet in range(1, 40):
                    r = gate.analyze(script((30 * F, SPEECH),
                                            (quiet * F, QUIET)))
                    if r.last_speech_end < r.duration:
                        closed_at = quiet
                        break
                self.assertEqual(closed_at, frames + 1)
                self.assertAlmostEqual(r.last_speech_end, 30 * F)

    def test_an_open_run_is_never_dropped_as_too_short(self):
        # A sentence, a breath the gate (at 160 ms) treats as a boundary,
        # then six frames of the next word - still being spoken when the
        # buffer ends. Six is under min_frames 8, but it is not a click: its
        # length is not known yet. Dropped, last_speech_end fell back to the
        # previous segment and the strategy cut through the word.
        audio = script((40 * F, SPEECH), (9 * F, QUIET), (6 * F, SPEECH))
        r = fake_gate(min_silence_ms=160, min_speech_ms=250).analyze(audio)
        self.assertEqual(len(r.segments), 2)
        self.assertEqual(r.last_speech_end, r.duration)
        # The same six frames, closed by quiet after them, ARE dropped: that
        # is what min_speech_ms is for.
        audio = script((40 * F, SPEECH), (9 * F, QUIET),
                       (6 * F, SPEECH), (9 * F, QUIET))
        r = fake_gate(min_silence_ms=160, min_speech_ms=250).analyze(audio)
        self.assertEqual(len(r.segments), 1)
        self.assertAlmostEqual(r.last_speech_end, 40 * F)

    def test_an_open_run_ends_at_the_audio_length_not_a_whole_frame(self):
        # Thirty frames and a half: the half must not read as silence for a
        # zero chunk_offset to cut on.
        r = fake_gate().analyze(script((30.5 * F, SPEECH)))
        self.assertGreater(r.duration, 30 * F)
        self.assertEqual(r.last_speech_end, r.duration)

    def test_nothing_said(self):
        r = fake_gate().analyze(script((30 * F, QUIET)))
        self.assertIsNone(r.last_speech_end)
        self.assertEqual(r.segments, [])
        self.assertFalse(r.has_speech)

    def test_too_short_to_feed(self):
        r = fake_gate().analyze(script((0.5 * F, SPEECH)))
        self.assertEqual(r.frames, 0)
        self.assertIsNone(r.last_speech_end)


class Pauses(unittest.TestCase):
    def test_longest_first_with_ties_in_spoken_order(self):
        audio = script((20 * F, SPEECH), (6 * F, QUIET), (20 * F, SPEECH),
                       (10 * F, QUIET), (20 * F, SPEECH), (6 * F, QUIET),
                       (20 * F, SPEECH), (10 * F, QUIET))
        r = fake_gate(min_silence_ms=160).analyze(audio)
        self.assertEqual([round(d / F) for _, _, d in r.pauses], [10, 6, 6])
        starts = [s for s, _, _ in r.pauses]
        self.assertLess(starts[1], starts[2])

    def test_short_runs_stay_in_pauses(self):
        # A 3-frame blip between two gaps leaves .segments but must not let
        # the quiet on both sides of it merge into one long "pause".
        audio = script((20 * F, SPEECH), (8 * F, QUIET), (3 * F, SPEECH),
                       (8 * F, QUIET), (20 * F, SPEECH), (20 * F, QUIET))
        r = fake_gate(min_silence_ms=160).analyze(audio)
        self.assertEqual(len(r.segments), 2)
        self.assertEqual([round(d / F) for _, _, d in r.pauses], [8, 8])

    def test_best_cut_is_the_middle_of_the_longest_pause_within_limit(self):
        audio = script((20 * F, SPEECH), (6 * F, QUIET), (20 * F, SPEECH),
                       (10 * F, QUIET), (20 * F, SPEECH), (20 * F, QUIET))
        r = fake_gate(min_silence_ms=160).analyze(audio)
        self.assertAlmostEqual(r.best_cut(), 51 * F)                # 46..56
        self.assertAlmostEqual(r.best_cut(limit_s=40 * F), 23 * F)  # 20..26
        self.assertIsNone(r.best_cut(limit_s=10 * F))
        self.assertIsNone(r.best_cut(min_pause_ms=400))

    def test_a_breath_the_gate_did_not_split_on_is_still_a_pause(self):
        # At 800 ms (25 frames) a 10-frame breath does not end the run; at
        # 160 ms it does. Either way it is a silence a force-cut can land
        # in, and the pause list must not depend on which.
        audio = script((20 * F, SPEECH), (10 * F, QUIET),
                       (20 * F, SPEECH), (30 * F, QUIET))
        split = fake_gate(min_silence_ms=160).analyze(audio)
        kept = fake_gate(min_silence_ms=800).analyze(audio)
        self.assertEqual(len(split.segments), 2)
        self.assertEqual(len(kept.segments), 1)
        self.assertEqual(kept.pauses, split.pauses)
        self.assertEqual([round(d / F) for _, _, d in kept.pauses], [10])
        self.assertAlmostEqual(kept.best_cut(), 25 * F)

    def test_a_dip_under_min_pause_is_not_a_pause(self):
        # MIN_PAUSE_MS is 98: three frames (96 ms) is a dip, four (128 ms)
        # a breath - upstream's min_silence_at_max_speech arithmetic, and
        # the same line best_cut()'s floor draws, so nothing reported here
        # is ever refused there.
        for frames, pauses in ((3, 0), (4, 1)):
            with self.subTest(frames=frames):
                audio = script((20 * F, SPEECH), (frames * F, QUIET),
                               (20 * F, SPEECH), (30 * F, QUIET))
                r = fake_gate(min_silence_ms=400).analyze(audio)
                self.assertEqual(len(r.pauses), pauses)
                if pauses:
                    self.assertAlmostEqual(r.best_cut(),
                                           (20 + frames / 2.0) * F)

    def test_the_pending_quiet_at_the_buffer_end_is_a_pause(self):
        # Eight quiet frames the gate (at 400 ms) has not yet accepted as
        # the end of speech: the run is still open, but a force-cut can
        # still land in that quiet rather than behind the last word.
        r = fake_gate(min_silence_ms=400).analyze(
            script((20 * F, SPEECH), (8 * F, QUIET)))
        self.assertEqual(r.last_speech_end, r.duration)
        self.assertEqual([round(d / F) for _, _, d in r.pauses], [8])
        self.assertAlmostEqual(r.best_cut(), 24 * F)

    def test_many_breaths_under_a_long_min_silence(self):
        # The case that used to come back empty: a long chunk whose every
        # pause is shorter than min_silence_ms.
        audio = script(*(((30 * F, SPEECH), (10 * F, QUIET)) * 5))
        r = fake_gate(min_silence_ms=1200).analyze(audio)
        self.assertEqual(len(r.segments), 1)
        self.assertEqual(len(r.pauses), 5)
        # All the same length, so spoken order: the earliest that fits.
        self.assertAlmostEqual(r.best_cut(), 35 * F)
        self.assertAlmostEqual(r.best_cut(limit_s=100 * F), 35 * F)


class Configure(unittest.TestCase):
    def test_a_retune_is_partial_and_leaves_the_rest_alone(self):
        gate = fake_gate()
        gate.configure(min_silence_ms=800)
        self.assertEqual(gate.min_silence_ms, 800.0)
        self.assertEqual(gate.threshold, 0.5)
        self.assertEqual(gate.min_frames, 8)
        gate.configure(min_speech_ms=64)
        self.assertEqual(gate.min_frames, 2)
        self.assertEqual(gate.min_silence_ms, 800.0)

    def test_a_result_keeps_the_tuning_it_was_judged_by(self):
        gate = fake_gate(min_silence_ms=160)
        r = gate.analyze(script((30 * F, SPEECH), (8 * F, QUIET)))
        gate.configure(min_silence_ms=400)
        self.assertAlmostEqual(r.last_speech_end, 30 * F)
        self.assertEqual(gate.min_silence_ms, 400.0)


if __name__ == "__main__":
    unittest.main()
