"""
The real Silero weights on real speech, when both are on this machine.

Skipped otherwise: _models/ is gitignored, and the fixture is generated into
tests/fixtures/ by make_speech_sample.py (Windows SAPI). The sample is six
sentences spliced together with KNOWN gaps - 0.30 s inside each sentence,
0.85 s between them - so the ladder below has a ground truth: 6.
"""
import json
import os
import unittest
import wave

import numpy as np

import settings
from buffering import create_strategy, SAMPLERATE
from speech_gate import SpeechGate

HERE = os.path.dirname(os.path.abspath(__file__))
WAV = os.path.join(HERE, "fixtures", "speech_sample.wav")
META = os.path.join(HERE, "fixtures", "speech_sample.json")
BLOCK = int(0.128 * SAMPLERATE)

AUDIO = None
META_DATA = None


def setUpModule():
    global AUDIO, META_DATA
    if not (os.path.exists(WAV) and os.path.exists(META)):
        raise unittest.SkipTest("no fixture - run tests\\make_speech_sample.py")
    if SpeechGate.create() is None:
        raise unittest.SkipTest("no Silero model / onnxruntime on this machine")
    with wave.open(WAV, "rb") as w:
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    # A second and a half of quiet after the last word, so the strategy can
    # see the talker stop instead of the file running out on them.
    AUDIO = np.concatenate([audio, np.zeros(int(1.5 * SAMPLERATE), np.float32)])
    with open(META) as f:
        META_DATA = json.load(f)


def gate(**kw):
    return SpeechGate.create(**kw)


def chunk_ends(min_silence_ms, **overrides):
    cfg = dict(settings.DEFAULTS, strategy="silence_at_end_of_chunk",
               chunk_length=2.0, chunk_max_length=30.0)
    cfg.update(overrides)
    strategy = create_strategy(cfg, gate(min_silence_ms=min_silence_ms))
    ends = []
    for i in range(0, len(AUDIO), BLOCK):
        strategy.feed(AUDIO[i:i + BLOCK])
        while True:
            d = strategy.next()
            if d.action == "wait":
                break
            if d.action == "transcribe":
                ends.append(round(d.t_start + d.duration, 3))
    return ends


class Ladder(unittest.TestCase):
    def test_segments_against_the_known_gaps(self):
        counts = dict((ms, len(gate(min_silence_ms=ms).analyze(AUDIO).segments))
                      for ms in (100, 160, 250, 400, 500, 900, 1200))
        sentences = META_DATA["sentences"]
        self.assertEqual(counts[400], sentences, counts)
        self.assertEqual(counts[500], sentences, counts)
        self.assertGreater(counts[160], sentences, counts)   # splits words
        self.assertLess(counts[900], sentences, counts)      # merges sentences
        ladder = [counts[ms] for ms in sorted(counts)]
        self.assertEqual(ladder, sorted(ladder, reverse=True), counts)


class Pauses(unittest.TestCase):
    def test_breaths_are_cut_candidates_whatever_min_silence_is(self):
        # Eleven constructed gaps of 0.30 and 0.85 s. At 1200 ms none of
        # them ends a run, and the pause list used to be empty - so the
        # force-cut landed wherever the clock said.
        r = gate(min_silence_ms=1200).analyze(AUDIO)
        self.assertEqual(len(r.segments), 1)
        self.assertGreaterEqual(len(r.pauses), 11)
        # The longest pause that fits an 8 s limit is the 0.85 s sentence
        # gap at 3.29 s, and the cut lands inside it - in quiet.
        cut = r.best_cut(limit_s=8.0)
        self.assertIsNotNone(cut)
        gap = META_DATA["long_gap_s"]
        self.assertTrue(any(g < cut < g + gap
                            for g in META_DATA["long_gaps_start"]), cut)


class LiveCuts(unittest.TestCase):
    def test_min_silence_under_chunk_offset_changes_nothing(self):
        # Both floor at the 0.4 s trailing silence, which no 0.30 s word gap
        # can satisfy. They used to differ - the 160 ms run cut into words,
        # because short open runs were dropped as clicks.
        self.assertEqual(chunk_ends(160), chunk_ends(400))

    def test_min_silence_over_every_gap_waits_for_the_end(self):
        # 1200 ms is longer than the 0.85 s sentence gaps: one chunk, cut
        # only once the talker has been quiet that long.
        ends = chunk_ends(1200)
        self.assertEqual(len(ends), 1, ends)
        self.assertGreater(ends[0], META_DATA["speech_ends"] + 1.2)

    def test_chunk_offset_under_the_word_gaps_exposes_min_silence(self):
        # Drop the trailing-silence floor below the 0.30 s word gaps and the
        # gate's own rule is all that is left: 160 ms cuts at word gaps,
        # 400 ms does not.
        self.assertGreater(len(chunk_ends(160, chunk_offset=0.1)),
                           len(chunk_ends(400, chunk_offset=0.1)))


if __name__ == "__main__":
    unittest.main()
