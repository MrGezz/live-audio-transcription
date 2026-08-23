"""
silence_at_end_of_chunk against a scripted talker, fed in 128 ms blocks the
way capture delivers it. The gate is the fake from fakes.py, so every cut
here is decided by the state machine and the strategy alone - and every
number below is in whole 32 ms frames so the block cadence is exact.
"""
import unittest

import numpy as np

import settings
from buffering import create_strategy, SAMPLERATE
from speech_gate import HOP
from tests.fakes import fake_gate, script, FRAME, SPEECH, QUIET

F = FRAME
BLOCK = int(0.128 * SAMPLERATE)         # four frames, one capture block


def run(audio, gate, **overrides):
    cfg = dict(settings.DEFAULTS, strategy="silence_at_end_of_chunk",
               chunk_length=1.0)
    cfg.update(overrides)
    strategy = create_strategy(cfg, gate)
    out = []
    for i in range(0, len(audio), BLOCK):
        strategy.feed(audio[i:i + BLOCK])
        while True:
            d = strategy.next()
            if d.action == "wait":
                break
            out.append(d)
    return out


def chunk_ends(decisions):
    return [round(d.t_start + d.duration, 3)
            for d in decisions if d.action == "transcribe"]


class WhenTheTalkerStopped(unittest.TestCase):
    # A sentence, a gap, a sentence, and quiet to the end. chunk_offset
    # stays at its 0.4 s default throughout.
    TALKER = ((63 * F, SPEECH), (19 * F, QUIET),      # 0.608 s gap
              (31 * F, SPEECH), (31 * F, QUIET))
    GAP = (63 * F, 82 * F)
    LAST_WORD_ENDS = 113 * F

    def test_min_silence_under_the_gap_cuts_in_it(self):
        # 400 ms closes the run on the 13th quiet frame, inside the 19.
        ends = chunk_ends(run(script(*self.TALKER),
                              fake_gate(min_silence_ms=400)))
        self.assertEqual(len(ends), 2, ends)
        self.assertGreater(ends[0], self.GAP[0])
        self.assertLessEqual(ends[0], self.GAP[1])

    def test_min_silence_over_the_gap_holds_the_cut(self):
        # Told to wait 832 ms, the gate must not report a 608 ms gap as the
        # end of speech - whatever chunk_offset would have accepted. This
        # is where the old tail cut after 416 ms regardless.
        ends = chunk_ends(run(script(*self.TALKER),
                              fake_gate(min_silence_ms=800)))
        self.assertEqual(len(ends), 1, ends)
        self.assertGreaterEqual(ends[0], self.LAST_WORD_ENDS)


class AnOpenRunIsNotAClick(unittest.TestCase):
    def test_the_next_word_is_never_cut_through(self):
        # A sentence, a 0.32 s breath, a second sentence. With min_silence
        # under the breath the gate closes the first run there; when the
        # buffer then ends a few frames into the second sentence, that open
        # run is shorter than min_speech_ms. Dropping it as a click put
        # last_speech_end at the end of the first sentence, 0.54 s back,
        # and the strategy cut 0.22 s into the second.
        talker = ((63 * F, SPEECH), (10 * F, QUIET),
                  (31 * F, SPEECH), (31 * F, QUIET))
        last_word_ends = 104 * F
        for ms in (100, 160, 250):
            with self.subTest(min_silence_ms=ms):
                ends = chunk_ends(run(script(*talker),
                                      fake_gate(min_silence_ms=ms)))
                self.assertEqual(len(ends), 1, ends)
                self.assertGreaterEqual(
                    ends[0], last_word_ends,
                    "cut at %.3f s, %.3f s before the talker stopped"
                    % (ends[0], last_word_ends - ends[0]))


class AnOpenRunSurvivesTheSkip(unittest.TestCase):
    """
    The no-speech skip drops a buffer with too few speech frames. A buffer
    that is quiet until a word starts in its last few frames is one of
    those - and dropping it throws the start of the word away with the
    quiet, so the next chunk opens mid-word. chunk_length 5.0 s is 156.25
    frames: the buffer is first analysed at 160, the 40th block.
    """

    def test_the_word_start_is_kept_and_opens_the_next_chunk(self):
        # 153 frames of quiet, then a word that has been going for seven
        # frames when the buffer is first analysed - under the 8 has_speech
        # needs. The word goes on for 64 frames, then quiet.
        talker = ((153 * F, QUIET), (64 * F, SPEECH), (100 * F, QUIET))
        decisions = run(script(*talker), fake_gate(), chunk_length=5.0)
        self.assertEqual([d.action for d in decisions],
                         ["skip", "transcribe"])
        skip, chunk = decisions
        self.assertEqual(skip.reason, "no-speech")
        self.assertAlmostEqual(skip.duration, 153 * F)      # the quiet only
        self.assertIn("word starting", skip.hint)
        self.assertAlmostEqual(chunk.t_start, 153 * F)      # from its 1st frame
        self.assertAlmostEqual(float(np.abs(chunk.audio[:HOP]).max()),
                               SPEECH, places=6)     # float32 audio
        self.assertAlmostEqual(chunk.t_start + chunk.duration,
                               skip.duration + len(chunk.audio) / SAMPLERATE)

    def test_a_long_open_run_that_is_not_speech_is_still_dropped(self):
        # One frame over the threshold, then twenty idling in the hysteresis
        # band: the run never closes (nothing under 0.35) but scores one
        # speech frame. Twenty frames is over min_speech_ms, so this is not
        # a word starting - it is what "audible but not speech" looks like,
        # and keeping it would be the leak the skip exists to close.
        talker = ((139 * F, QUIET), (1 * F, SPEECH), (20 * F, 0.4),
                  (100 * F, QUIET))
        decisions = run(script(*talker), fake_gate(), chunk_length=5.0)
        self.assertEqual(decisions[0].action, "skip")
        self.assertAlmostEqual(decisions[0].duration, 160 * F)   # all of it

    def test_a_silent_stream_still_cannot_grow_the_buffer(self):
        # Quiet with a blip at the end of every chunk. The blip is kept once,
        # closed by the quiet that follows, dropped as the click it was, and
        # nothing accumulates across cycles.
        cfg = dict(settings.DEFAULTS, strategy="silence_at_end_of_chunk")
        strategy = create_strategy(cfg, fake_gate())
        audio = script(*(((153 * F, QUIET), (4 * F, SPEECH)) * 4))
        worst = 0.0
        for i in range(0, len(audio), BLOCK):
            strategy.feed(audio[i:i + BLOCK])
            while strategy.next().action != "wait":
                pass
            worst = max(worst, strategy.pending_seconds())
        self.assertLess(worst, cfg["chunk_length"] + 0.5)


class ForceCut(unittest.TestCase):
    def test_lands_in_the_middle_of_the_longest_pause(self):
        # Continuous speech past chunk_max_length with one 0.32 s pause in
        # it: the forced cut pulls back to the middle of that pause rather
        # than landing wherever the clock says.
        talker = ((100 * F, SPEECH), (10 * F, QUIET), (200 * F, SPEECH))
        decisions = run(script(*talker), fake_gate(min_silence_ms=160),
                        chunk_max_length=125 * F)
        first = [d for d in decisions if d.action == "transcribe"][0]
        self.assertAlmostEqual(first.duration, 105 * F, places=6)
        self.assertIn("pulled back", first.hint)

    def test_lands_in_a_breath_the_gate_did_not_split_on(self):
        # The same talker at 1200 ms, where the 0.32 s pause is a breath
        # inside one long run rather than a gap between two. It used to be
        # invisible here, and the cut landed on the clock, mid-word.
        talker = ((100 * F, SPEECH), (10 * F, QUIET), (200 * F, SPEECH))
        decisions = run(script(*talker), fake_gate(min_silence_ms=1200),
                        chunk_max_length=125 * F)
        first = [d for d in decisions if d.action == "transcribe"][0]
        self.assertAlmostEqual(first.duration, 105 * F, places=6)


class Skips(unittest.TestCase):
    def test_audible_but_no_speech(self):
        decisions = run(script((40 * F, QUIET)), fake_gate())
        self.assertEqual([d.action for d in decisions], ["skip"])
        self.assertEqual(decisions[0].reason, "no-speech")

    def test_silence_never_reaches_the_gate(self):
        decisions = run(np.zeros(40 * SAMPLERATE // 1000 * 32,
                                 dtype=np.float32), fake_gate())
        self.assertEqual([d.action for d in decisions], ["skip"])
        self.assertEqual(decisions[0].reason, "silence")


if __name__ == "__main__":
    unittest.main()
