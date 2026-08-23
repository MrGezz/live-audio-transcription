"""
The "-> EN" tag has to be a measurement, not an echo of the checkbox.

It used to be `bool(settings["translate"])` (pipeline.py), so a backend that
ignored the translate request produced a caption tagged "ja -> EN" with
Japanese in it and nothing in the app could tell - which is exactly what a
*-turbo model does, since it is a transcription-only distillation that accepts
the translate task and decodes the audio anyway. These tests pin both pieces of
evidence that replaced the checkbox: what the backend says it did, and what
came back in the text.

No model and no server: the server path is driven through a fake `requests`,
and the labelling through the real Pipeline._publish.
"""
import copy
import inspect
import json
import pickle
import unittest
from unittest import mock

import pipeline as pipeline_mod
import whisper_backends as wb
from buffering import Decision


class LooksUntranslated(unittest.TestCase):
    """Only ever says YES with certainty."""

    def test_source_scripts_are_proof_a_translation_did_not_happen(self):
        for text in ("あの石の城",      # ja, kana + kanji
                     "on にここで",         # the user's line
                     "中文",                        # zh
                     "한국어",                  # ko
                     "Привет",  # ru
                     "مرحبا"):     # ar
            self.assertTrue(wb.looks_untranslated(text), text)

    def test_english_is_never_flagged(self):
        for text in ("Here is the thing about the stone castle.",
                     "Café naïve résumé",   # Latin-1
                     "、",          # CJK punctuation alone is not evidence
                     "", None):
            self.assertFalse(wb.looks_untranslated(text), repr(text))


class SegmentCarriesTheAnswer(unittest.TestCase):
    def test_it_is_still_the_two_tuple_everything_unpacks(self):
        # live_transcription.py, the lite build and benchmark.py all do
        # `for text, lang in results`; a third element breaks every one.
        segment = wb.Segment("hi", "ja", translated=True)
        self.assertEqual(tuple(segment), ("hi", "ja"))
        text, language = segment
        self.assertEqual((text, language), ("hi", "ja"))

    def test_unanswered_is_none_not_false(self):
        # "the backend did not say" and "the backend said no" are different
        # answers, and only the second one is evidence.
        self.assertIsNone(wb.Segment("hi", "ja").translated)


class TheAnswerSurvivesBeingRebuilt(unittest.TestCase):
    """
    A Segment is a tuple subclass, so it cannot be edited: anything that
    wants a changed one builds a new one. `translated` has to come along
    every time. It is a measurement of what the backend did, and the rule
    above it is that the answer may only ever be downgraded on evidence -
    so a rebuild that hands back None instead of the True the backend
    reported is exactly the silent relabelling that rule exists to stop.

    Three routes reach __new__ a second time. copy and pickle go through
    __getnewargs__; anyone doing it by hand uses those same args directly.
    The first two turn out not to need the tail of that list at all - they
    overwrite __new__'s work with the instance __dict__ - which is why an
    incomplete __getnewargs__ was invisible for as long as it was. The
    third has nothing to overwrite it with.
    """

    def segment(self, translated):
        return wb.Segment(
            "the stone castle", "ja",
            words=[{"word": "the", "probability": 0.9, "start": 0.0},
                   {"word": " stone", "probability": 0.8, "start": 0.4}],
            start=0.0, end=1.2, no_speech_prob=0.01, avg_logprob=-0.31,
            translated=translated)

    def test_copy_keeps_the_answer(self):
        for answer in (True, False, None):
            with self.subTest(translated=answer):
                self.assertIs(copy.copy(self.segment(answer)).translated,
                              answer)
                self.assertIs(copy.deepcopy(self.segment(answer)).translated,
                              answer)

    def test_pickle_keeps_it_at_every_protocol(self):
        # Protocols 0 and 1 do not use __getnewargs__ at all; 2 and up do.
        # Both routes have to arrive at the same answer, so all of them are
        # walked rather than whichever one happens to be the default.
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            for answer in (True, False, None):
                with self.subTest(protocol=protocol, translated=answer):
                    blob = pickle.dumps(self.segment(answer), protocol)
                    self.assertIs(pickle.loads(blob).translated, answer)

    def test_rebuilding_from_the_new_args_keeps_it(self):
        # The route with nothing to overwrite a short argument list, and the
        # one this class was written for: while translated was missing from
        # __getnewargs__, this returned None for all three answers.
        for answer in (True, False, None):
            with self.subTest(translated=answer):
                original = self.segment(answer)
                self.assertIs(wb.Segment(*original.__getnewargs__()).translated,
                              answer)

    def test_the_new_args_are_the_whole_of_new(self):
        # Checked against the signature rather than a written-down count, so
        # that adding a ninth thing to a Segment fails here, saying which
        # one, instead of quietly dropping out of every rebuild.
        params = list(inspect.signature(wb.Segment.__new__).parameters)[1:]
        self.assertEqual(params[-1], "translated")
        self.assertEqual(len(self.segment(True).__getnewargs__()), len(params))

    def test_a_rebuilt_segment_is_still_the_two_tuple_and_still_measured(self):
        original = self.segment(True)
        rebuilt = {"deepcopy": copy.deepcopy(original),
                   "pickle": pickle.loads(pickle.dumps(original)),
                   "by hand": wb.Segment(*original.__getnewargs__())}
        for route, clone in rebuilt.items():
            with self.subTest(route=route):
                self.assertEqual(tuple(clone), ("the stone castle", "ja"))
                self.assertIs(clone.translated, True)
                self.assertEqual(clone.language, "ja")
                self.assertEqual(clone.words, original.words)
                self.assertEqual((clone.start, clone.end), (0.0, 1.2))
                self.assertEqual(clone.no_speech_prob, 0.01)
                self.assertEqual(clone.avg_logprob, -0.31)
                # Not carried; recomputed from the words that are there,
                # which is what lets a trimmed segment report an honest
                # confidence rather than the whole window's.
                self.assertAlmostEqual(clone.probability, 0.85)


class ServerReadsTheTask(unittest.TestCase):
    """verbose_json's "task" is the server's own answer, after its overrides."""

    @staticmethod
    def _segments(payload):
        backend = wb.ServerBackend.__new__(wb.ServerBackend)
        backend.language = "ja"
        return backend._read_segments(payload, "ja")

    def test_translate_is_carried_onto_every_segment(self):
        got = self._segments({"task": "translate",
                              "segments": [{"text": " Hello there."},
                                           {"text": " And again."}]})
        self.assertEqual([s.translated for s in got], [True, True])

    def test_transcribe_is_carried_too(self):
        # whisper-server sets translate=false ITSELF for an English-only
        # model and logs it to its own console, where nothing here can see
        # it - the response is the only channel that reaches us.
        got = self._segments({"task": "transcribe",
                              "segments": [{"text": " Hello there."}]})
        self.assertEqual([s.translated for s in got], [False])

    def test_a_response_without_the_field_says_nothing(self):
        got = self._segments({"segments": [{"text": " Hello there."}]})
        self.assertEqual([s.translated for s in got], [None])

    def test_the_no_segments_shape_carries_it_as_well(self):
        got = self._segments({"task": "translate", "text": "Hello there.",
                              "duration": 4.0})
        self.assertEqual([s.translated for s in got], [True])


class FakeResponse(object):
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(json.dumps(self._payload))


class FakeRequests(object):
    """Records the form fields and answers with a canned payload."""

    def __init__(self, payload):
        self.payload = payload
        self.posts = []

    def post(self, url, data=None, files=None, timeout=None):
        self.posts.append({"url": url, "data": dict(data or {})})
        return FakeResponse(self.payload)


class TheRequestStillAsksCorrectly(unittest.TestCase):
    def test_translate_goes_on_the_wire_lowercase(self):
        # whisper.cpp's parse_str_to_bool accepts "true"/"1"/"yes"/"y" and
        # nothing else - it is case SENSITIVE, so a client sending Python's
        # str(True) would be silently transcribed. Ours does not.
        backend = wb.ServerBackend.__new__(wb.ServerBackend)
        backend.language = "ja"
        self.assertEqual(backend._form(True, {})["translate"], "true")
        self.assertEqual(backend._form(False, {})["translate"], "false")


class TheTag(unittest.TestCase):
    """Pipeline._publish decides what the front ends' pill can claim."""

    def setUp(self):
        self.events = []
        self.pipe = pipeline_mod.Pipeline(
            {"translate": True},
            emit=lambda kind, data: self.events.append((kind, data)))

    def publish(self, text, language="ja", translated=None):
        segment = wb.Segment(text, language, translated=translated)
        self.pipe._publish(segment, Decision("transcribe", t_start=1.0,
                                             duration=4.0, level=0.05), 1.7)
        return [d for k, d in self.events if k == "transcript"][-1]

    def warnings(self):
        return [d["msg"] for k, d in self.events
                if k == "log" and d["level"] == "warn"]

    def test_english_back_from_a_backend_that_says_it_translated(self):
        entry = self.publish("Here is the stone castle.", translated=True)
        self.assertTrue(entry["translated"])
        self.assertEqual(entry["language"], "ja")   # the AUDIO's language
        self.assertEqual(self.warnings(), [])

    def test_a_turbo_model_returning_japanese_is_not_called_translated(self):
        # The bug this file exists for: the model accepts the task, ignores
        # it, and the old code labelled the result "ja -> EN" regardless.
        entry = self.publish("あの石の城の中",
                             translated=True)
        self.assertFalse(entry["translated"])
        warned = self.warnings()
        self.assertEqual(len(warned), 1, warned)
        self.assertIn("turbo", warned[0])

    def test_a_backend_that_says_it_transcribed_is_believed(self):
        entry = self.publish("Here is the stone castle.", translated=False)
        self.assertFalse(entry["translated"])
        warned = self.warnings()
        self.assertEqual(len(warned), 1, warned)
        self.assertIn("English-only", warned[0])

    def test_no_answer_and_plausible_output_stays_translated(self):
        # An older server that does not report "task" must not turn every
        # caption into a warning: absence of evidence is not evidence.
        entry = self.publish("Here is the stone castle.", translated=None)
        self.assertTrue(entry["translated"])
        self.assertEqual(self.warnings(), [])

    def test_translation_off_is_never_labelled_translated(self):
        self.pipe.settings["translate"] = False
        entry = self.publish("あの石の城", translated=None)
        self.assertFalse(entry["translated"])
        self.assertEqual(self.warnings(), [])

    def test_the_warning_is_once_per_backend_not_once_per_caption(self):
        for _ in range(5):
            self.publish("あの石の城", translated=True)
        self.assertEqual(len(self.warnings()), 1)

        # A new backend is a new model, so it earns a fresh answer: without
        # this, switching models would never clear the warning and switching
        # to a turbo model would never raise it. Patched so the test neither
        # touches the network nor loads a model to prove a flag was reset.
        with mock.patch.object(pipeline_mod, "create_backend",
                               return_value=object()):
            self.pipe._build_backend()
        self.publish("あの石の城", translated=True)
        self.assertEqual(len(self.warnings()), 2)


class EndToEndThroughTheFakeServer(unittest.TestCase):
    """The whole path: HTTP response -> Segment -> entry, no model, no server."""

    def run_one(self, payload, translate=True):
        backend = wb.ServerBackend.__new__(wb.ServerBackend)
        backend.language = "ja"
        backend.url = "http://127.0.0.1:8080"
        backend.timeout = 5.0
        backend.last_detection = None
        backend._flat_reported = False
        backend._requests = FakeRequests(payload)
        segments = backend._read_segments(payload, "ja")

        events = []
        pipe = pipeline_mod.Pipeline(
            {"translate": translate},
            emit=lambda kind, data: events.append((kind, data)))
        pipe.backend = backend
        for segment in segments:
            pipe._publish(segment, Decision("transcribe", t_start=0.0,
                                            duration=4.0, level=0.05), 1.0)
        return [d for k, d in events if k == "transcript"]

    def test_the_users_case(self):
        # What the running whisper-server (large-v3-turbo) actually returns
        # for --translate on Japanese audio: task=translate, Japanese text.
        entries = self.run_one({
            "task": "translate",
            "language": "japanese",
            "segments": [{"text": " あ、あの石の"
                                  "城の中..."}],
        })
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["translated"])
        self.assertEqual(entries[0]["language"], "ja")

    def test_the_same_request_against_a_model_that_translates(self):
        entries = self.run_one({
            "task": "translate",
            "language": "japanese",
            "segments": [{"text": " Inside that stone castle..."}],
        })
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["translated"])


if __name__ == "__main__":
    unittest.main()
