"""
The sliding window's overlap filter, on segments a real whisper-server gave.

Every window in here was captured from `_whisper.cpp\\whisper-server.exe` over
tests/fixtures/speech_sample.wav, so the wording is the model's and not a
convenient invention - including its mistakes, which is the point: "speech"
comes back as "beach" on one side of a cut and the filter still has to
recognise the repeat. The geometry each case came from is named in the test.

No model, no server and no audio: filter() is handed the segments directly,
which is exactly what pipeline.py does with what the backend returned.
"""
import unittest

import settings
from buffering import OVERLAP_SLACK, SlidingWindow, _overlap_length
from whisper_backends import Segment

# Sub-word tokens as whisper-server emits them: the leading space marks a new
# word, "Silero" arrives in three pieces, and the full stop belongs to none.
# From window 2 of --buffer 4 --slide 2, verbatim.
SILERO_WORDS = [
    {"word": "Sil", "start": 0.18, "end": 0.24, "probability": 0.41},
    {"word": "er", "start": 0.24, "end": 0.39, "probability": 0.55},
    {"word": "ow", "start": 0.39, "end": 0.54, "probability": 0.62},
    {"word": " decides", "start": 0.54, "end": 1.08, "probability": 0.93},
    {"word": " whether", "start": 1.09, "end": 1.62, "probability": 0.97},
    {"word": " this", "start": 1.65, "end": 1.82, "probability": 0.98},
    {"word": " buffer", "start": 2.21, "end": 2.44, "probability": 0.88},
    {"word": " contains", "start": 2.44, "end": 3.07, "probability": 0.96},
    {"word": " speech", "start": 3.07, "end": 3.47, "probability": 0.95},
    {"word": ".", "start": 3.54, "end": 3.66, "probability": 0.71},
]


def segments(*texts):
    """One window, in the shape ServerBackend returns."""
    return [Segment(text, "en", translated=False) for text in texts]


def strategy(**overrides):
    cfg = dict(settings.DEFAULTS, strategy="sliding_window")
    cfg.update(overrides)
    return SlidingWindow(cfg, None)


def captions(windows, **overrides):
    """The text kept for each window in turn."""
    filt = strategy(**overrides)
    return [[_text(s) for s in filt.filter(w)[0]] for w in windows]


def _text(segment):
    return segment if isinstance(segment, str) else segment.text


def words(text):
    """A word list whose tokens spell `text`, one token per word."""
    out = []
    for i, word in enumerate(text.split()):
        out.append({"word": (" " if i else "") + word,
                    "start": i * 0.5, "end": i * 0.5 + 0.4,
                    "probability": 0.9})
    return out


class TheOverlapArrivesInsideASegment(unittest.TestCase):
    """
    The reason a keep-or-drop filter could not work.

    whisper-server cuts segments where its own caption wrapping says, which
    has nothing to do with where the audio was cut, so the repeated speech
    comes back fused to the new speech in one segment.
    """

    def test_the_fused_segment_keeps_only_its_new_half(self):
        # --buffer 4 --slide 2, windows 1 and 2. The first four words of
        # window 2 were printed by window 1; the rest had never been seen.
        out = captions([
            segments("over the lazy dog.", "Silerow decides whether this"),
            segments("Silerow decides whether this buffer contains speech."),
        ])
        self.assertEqual(out[1], ["buffer contains speech."])

    def test_a_segment_that_is_all_overlap_is_dropped_whole(self):
        out = captions([
            segments("The quick brown fox jumps over the lazy dog."),
            segments("over the lazy dog.", "Silerow decides whether this"),
        ])
        self.assertEqual(out[1], ["Silerow decides whether this"])

    def test_the_whole_segment_test_alone_could_not_have_caught_either(self):
        # Why the old filter fired zero times over twelve windows. A pure
        # repeat scores 0.57 against the line it repeats, because difflib's
        # ratio is over the sum of BOTH lengths and the previous window's
        # line is the longer one - and the shipped threshold is 0.80.
        import difflib
        from buffering import _key
        score = difflib.SequenceMatcher(
            None,
            _key("over the lazy dog."),
            _key("The quick brown fox jumps over the lazy dog.")).ratio()
        self.assertAlmostEqual(score, 0.567, places=3)
        self.assertLess(score, settings.DEFAULTS["dedup_threshold"])

    def test_the_untrimmed_transcript_is_the_bug(self):
        # 12 windows of --buffer 4 --slide 2 printed 98 words where 56 were
        # spoken. This is the same shape in miniature: without the trim the
        # second window reprints all four of the first window's last words.
        out = captions([
            segments("The quick brown fox jumps over the lazy dog."),
            segments("over the lazy dog. Silerow decides."),
        ])
        self.assertEqual(out[1], ["Silerow decides."])


class TheAnchorIsThePreviousWindowsEnd(unittest.TestCase):
    """
    A shared run is the overlap when it reaches the previous window's END.

    Anchoring on both ends instead is what does not work, and these are the
    measured cases that decide each rule.
    """

    @staticmethod
    def cut(prev, new, **kw):
        return _overlap_length(prev.split(), new.split(), **kw)

    def test_a_run_split_by_a_re_decode_still_anchors(self):
        # --buffer 4 --slide 1, window 8. The model spelled the overlap
        # "ends speech wants it has" first and "ends beach once it has"
        # second, so difflib splits the match into "a gap only ends" at the
        # head and "it has" at the tail. Requiring one run to touch both
        # ends found neither and trimmed nothing.
        self.assertEqual(
            self.cut("speech a gap only ends speech wants it has",
                     "a gap only ends beach once it has lasted long enough"),
            8)

    def test_an_interior_one_word_match_is_not_evidence(self):
        # --buffer 3 --slide 2, window 4. The only thing shared is one
        # "speech", sitting in the middle of both lines. Believing it ate
        # "a gap only in speech" - four words the previous window never
        # printed, because it rendered them "agapo".
        self.assertEqual(
            self.cut("buffer contains speech agapo",
                     "a gap only in speech once it has been made"),
            0)

    def test_a_corner_one_word_match_is_evidence(self):
        # --buffer 3 --slide 2, window 6: the ordinary case of a window
        # opening on the last word the previous one ended with.
        self.assertEqual(
            self.cut("beach once it has lasted long enough",
                     "enough 400 milise consists"),
            1)

    def test_one_word_in_only_one_corner_is_still_not_evidence(self):
        # Same word, same tail anchor, but not at the head of the new
        # window - so it is a word that happens to recur, not the overlap.
        self.assertEqual(
            self.cut("beach once it has lasted long enough",
                     "400 milise enough consists"),
            0)

    def test_slack_forgives_a_mangled_tail(self):
        # The last word of a window is the one the cut went through, so it
        # is the one most likely to come back spelled differently. Two of
        # them may go unmatched and the run still counts.
        self.assertEqual(
            self.cut("this buffer contains speech agap",
                     "this buffer contains speech a gap only ends"),
            4)

    def test_without_slack_that_duplicate_would_be_printed(self):
        # The measurement behind OVERLAP_SLACK: at 0 the run has to reach
        # the very last word, which a re-decoded tail never does.
        self.assertEqual(OVERLAP_SLACK, 2)
        self.assertEqual(
            self.cut("this buffer contains speech agap",
                     "this buffer contains speech a gap only ends", slack=0),
            0)

    def test_nothing_in_common_trims_nothing(self):
        self.assertEqual(self.cut("the quick brown fox",
                                  "entirely different words here"), 0)

    def test_the_first_window_has_nothing_to_match(self):
        self.assertEqual(self.cut("", "the quick brown fox"), 0)


class WhatTheNextWindowRemembers(unittest.TestCase):
    """
    The remembered keys are what the window DECODED, not what it printed.

    A window's own trimmed-off head was still spoken inside it, so the next
    window overlaps that too.
    """

    def test_a_window_trimmed_to_nothing_is_still_remembered(self):
        out = captions([
            segments("the cat sat on the mat"),
            segments("sat on the mat"),            # all overlap, prints none
            segments("sat on the mat and yawned"),
        ])
        self.assertEqual(out[1], [])
        # If window 1 had been forgotten for printing nothing, window 2
        # would have had only window 0's words to match and "sat on the mat"
        # would have come back a second time.
        self.assertEqual(out[2], ["and yawned"])

    def test_a_deliberate_repeat_across_a_window_survives(self):
        # Only the IMMEDIATELY previous window can overlap this one. A line
        # said again after an intervening window is a repeat the speaker
        # made, and scores 1.00 against itself at any threshold.
        out = captions([
            segments("Thank you."),
            segments("and now for something else"),
            segments("Thank you."),
        ])
        self.assertEqual(out[2], ["Thank you."])

    def test_a_word_said_twice_inside_the_overlap_is_trimmed_once(self):
        out = captions([
            segments("no no no"),
            segments("no no no no"),
        ])
        self.assertEqual(out[1], ["no"])


class WordTimingsFollowTheText(unittest.TestCase):
    """A trimmed caption whose timings still describe the words shown."""

    def test_sub_word_tokens_are_cut_at_the_word_not_the_token(self):
        # "Silero" is three tokens. Counting tokens instead of words would
        # leave the caption reading "buffer contains speech." with timings
        # that still start at "ow".
        filt = strategy()
        filt.filter(segments("over the lazy dog.", "Silerow decides whether this"))
        kept, _ = filt.filter([Segment(
            "Silerow decides whether this buffer contains speech.", "en",
            words=list(SILERO_WORDS), start=0.0, end=3.84, translated=False)])
        self.assertEqual([s.text for s in kept], ["buffer contains speech."])
        self.assertEqual(
            "".join(w["word"] for w in kept[0].words).strip(),
            "buffer contains speech.")

    def test_the_start_moves_to_the_first_word_left(self):
        filt = strategy()
        filt.filter(segments("over the lazy dog.", "Silerow decides whether this"))
        kept, _ = filt.filter([Segment(
            "Silerow decides whether this buffer contains speech.", "en",
            words=list(SILERO_WORDS), start=0.0, end=3.84, translated=False)])
        self.assertAlmostEqual(kept[0].start, 2.21)
        self.assertAlmostEqual(kept[0].end, 3.84)

    def test_probability_is_recomputed_over_what_is_left(self):
        filt = strategy()
        filt.filter(segments("over the lazy dog.", "Silerow decides whether this"))
        kept, _ = filt.filter([Segment(
            "Silerow decides whether this buffer contains speech.", "en",
            words=list(SILERO_WORDS), start=0.0, end=3.84, translated=False)])
        left = SILERO_WORDS[6:]
        self.assertAlmostEqual(
            kept[0].probability,
            sum(w["probability"] for w in left) / len(left))

    def test_translated_survives_the_trim(self):
        # Invariant 16: `translated` is a measurement of what the backend
        # did with this audio. Cutting a repeated head off the caption does
        # not change that answer, and a rebuild that dropped it would
        # relabel a translated caption for no reason a reader could see.
        filt = strategy()
        filt.filter([Segment("the cat sat", "ja", translated=True)])
        kept, _ = filt.filter([Segment("the cat sat on the mat", "ja",
                                       words=words("the cat sat on the mat"),
                                       translated=True)])
        self.assertEqual([s.text for s in kept], ["on the mat"])
        self.assertTrue(kept[0].translated)
        self.assertEqual(kept[0].language, "ja")

    def test_a_segment_with_no_timings_is_still_trimmed(self):
        # word_timestamps is a setting, and the CPU backend charges real
        # time for it. The trim is text-only for exactly this reason.
        filt = strategy()
        filt.filter(segments("the cat sat"))
        kept, _ = filt.filter(segments("the cat sat on the mat"))
        self.assertEqual([s.text for s in kept], ["on the mat"])
        self.assertEqual(kept[0].words, [])


class TheShapeOfASegmentIsNotAssumed(unittest.TestCase):
    """
    filter() is handed whatever the caller's backend returns.

    _text_of has always tolerated four shapes; rewriting one has to as well.
    """

    def test_plain_strings(self):
        filt = strategy()
        filt.filter(["the cat sat"])
        kept, _ = filt.filter(["the cat sat on the mat"])
        self.assertEqual(kept, ["on the mat"])

    def test_dicts_keep_their_other_keys(self):
        filt = strategy()
        filt.filter([{"text": "the cat sat"}])
        kept, _ = filt.filter([{"text": "the cat sat on the mat",
                                "words": words("the cat sat on the mat"),
                                "language": "en"}])
        self.assertEqual(kept[0]["text"], "on the mat")
        self.assertEqual(kept[0]["language"], "en")
        self.assertEqual("".join(w["word"] for w in kept[0]["words"]).strip(),
                         "on the mat")

    def test_bare_two_tuples(self):
        filt = strategy()
        filt.filter([("the cat sat", "en")])
        kept, _ = filt.filter([("the cat sat on the mat", "en")])
        self.assertEqual(kept, [("on the mat", "en")])


class WhatIsReported(unittest.TestCase):
    """pipeline.py counts and shows every dropped item, so it must be true."""

    def test_the_trimmed_head_is_reported_as_dropped(self):
        filt = strategy()
        filt.filter(segments("the cat sat"))
        kept, dropped = filt.filter(segments("the cat sat on the mat"))
        self.assertEqual([_text(s) for s in kept], ["on the mat"])
        self.assertEqual([_text(s) for s in dropped], ["the cat sat"])

    def test_a_leading_punctuation_fragment_is_not_a_caption(self):
        # A window opens mid-utterance, so the model answers the clipped
        # audio with a stray token. It has nothing to print and nothing to
        # count, so it goes without being reported as a duplicate.
        filt = strategy()
        filt.filter(segments("the cat sat"))
        kept, dropped = filt.filter(segments('" the cat sat on the mat'))
        self.assertEqual([_text(s) for s in kept], ["on the mat"])
        # The stray token goes out with the overlap it sat in front of, and
        # is reported with it rather than as a caption of its own.
        self.assertEqual([_text(s) for s in dropped], ['" the cat sat'])

    def test_a_punctuation_only_segment_is_dropped(self):
        filt = strategy()
        kept, dropped = filt.filter(segments("."))
        self.assertEqual(kept, [])
        self.assertEqual([_text(s) for s in dropped], ["."])

    def test_the_first_window_is_untouched(self):
        filt = strategy()
        kept, dropped = filt.filter(segments("The quick brown fox."))
        self.assertEqual([_text(s) for s in kept], ["The quick brown fox."])
        self.assertEqual(dropped, [])

    def test_an_empty_window_answers_empty(self):
        self.assertEqual(strategy().filter([]), ([], []))


class TheThresholdStillReachesSomething(unittest.TestCase):
    """
    --dedup-threshold survives the change, on what the trim leaves behind.

    It fired on nothing in any of the five geometries measured - the trim
    gets there first - but a setting that changes nothing is worse than no
    setting, so this pins that it is still wired to the whole-segment test.
    """

    # A speaker who says the same line twice inside one slide. The overlap
    # is the FIRST copy, at the head of the window where the previous
    # window's tail landed; the second is speech the trim cannot touch,
    # because it is not part of the overlap.
    WINDOWS = [["alpha bravo charlie"],
               ["alpha bravo charlie", "delta echo", "alpha bravo charlie"]]

    def windows(self):
        return [segments(*texts) for texts in self.WINDOWS]

    def test_the_trim_takes_the_head_and_the_threshold_takes_the_rest(self):
        self.assertEqual(captions(self.windows())[1], ["delta echo"])

    def test_raising_it_lets_the_repeat_through(self):
        # What the setting is for: at 1.00 only an identical line is a
        # duplicate, and the second "alpha bravo charlie" is one the speaker
        # actually said.
        self.assertEqual(
            captions(self.windows(), dedup_threshold=1.0)[1],
            ["delta echo", "alpha bravo charlie"])


class ResetDropsTheHistory(unittest.TestCase):
    """reset() throws the audio away, so the words describing it go too."""

    def test_the_next_window_is_not_matched_against_the_old_one(self):
        filt = strategy()
        filt.filter(segments("the cat sat"))
        filt.reset()
        kept, _ = filt.filter(segments("the cat sat on the mat"))
        self.assertEqual([_text(s) for s in kept], ["the cat sat on the mat"])

    def test_configure_does_not_drop_it(self):
        # The other half of the same rule, stated the other way round: a
        # setting changed mid-sentence leaves the previous window still
        # adjacent to the next one.
        filt = strategy()
        filt.filter(segments("the cat sat"))
        filt.configure(dict(settings.DEFAULTS, strategy="sliding_window"), None)
        kept, _ = filt.filter(segments("the cat sat on the mat"))
        self.assertEqual([_text(s) for s in kept], ["on the mat"])


class GeometryChangesKeepTheHistory(unittest.TestCase):
    """configure() drops buffered audio, never the dedup history."""

    def test_the_previous_windows_words_survive_a_reconfigure(self):
        filt = strategy()
        filt.filter(segments("the cat sat"))
        filt.configure(dict(settings.DEFAULTS, strategy="sliding_window",
                            buffer=6, slide=3), None)
        kept, _ = filt.filter(segments("the cat sat on the mat"))
        self.assertEqual([_text(s) for s in kept], ["on the mat"])


if __name__ == "__main__":
    unittest.main()
