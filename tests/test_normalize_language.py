r"""
One spelling of "detect it", and the two backends that spell it differently.

normalize_language() is the only place that decides what "auto" means, and
both backends depend on it agreeing with itself: the HTTP API wants the
literal string "auto" and faster-whisper raises on it, so every backend stores
None and re-spells at the call site. Nothing pinned either half - not the
collapse to None, not the re-spelling, not the idempotence the docstring
promises so a backend can normalize again in its own __init__.

It is a pure function with no dependencies, which is why leaving it untested
was cheap and why fixing that is too. The re-spelling tests cost a little more
because they need a backend, but not a server, a model or any audio: the
server's request form and faster-whisper's kwargs are both built before
anything leaves the process.

    .venv\Scripts\python.exe -m unittest tests.test_normalize_language -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import whisper_backends as wb  # noqa: E402


#: Every way the app is allowed to say "work it out per request".
DETECT_SPELLINGS = (None, "", "   ", "auto", "AUTO", "Auto", "  auto  ",
                    "\tAuTo\n")

#: A spread across the table rather than one code, so a mangled lookup shows.
REAL_CODES = ("en", "es", "fr", "de", "ja", "zh", "ms", "id")


class TheDetectSpellings(unittest.TestCase):
    """Everything that means "no pin" collapses to exactly None."""

    def test_none_stays_none(self):
        self.assertIsNone(wb.normalize_language(None))

    def test_empty_and_whitespace_are_none(self):
        self.assertIsNone(wb.normalize_language(""))
        self.assertIsNone(wb.normalize_language("   "))
        self.assertIsNone(wb.normalize_language("\t\n"))

    def test_auto_in_any_case_or_padding_is_none(self):
        for spelling in DETECT_SPELLINGS:
            self.assertIsNone(wb.normalize_language(spelling),
                              "{0!r} should mean detect".format(spelling))

    def test_none_is_none_and_not_merely_falsey(self):
        # The distinction is load-bearing downstream: faster-whisper is handed
        # this value directly, and "" is not the same request as None.
        for spelling in DETECT_SPELLINGS:
            got = wb.normalize_language(spelling)
            self.assertIs(got, None, "{0!r} -> {1!r}".format(spelling, got))


class RealCodes(unittest.TestCase):

    def test_a_spread_of_the_table_survives(self):
        for code in REAL_CODES:
            self.assertEqual(wb.normalize_language(code), code)

    def test_case_is_folded(self):
        self.assertEqual(wb.normalize_language("EN"), "en")
        self.assertEqual(wb.normalize_language("Zh"), "zh")

    def test_padding_is_stripped(self):
        self.assertEqual(wb.normalize_language("  en  "), "en")
        self.assertEqual(wb.normalize_language("\tja\n"), "ja")

    def test_the_result_is_always_in_the_table(self):
        for code in REAL_CODES:
            self.assertIn(wb.normalize_language(code), wb.WHISPER_LANGUAGES)


class Idempotence(unittest.TestCase):
    """The docstring promises it, and four __init__s rely on it."""

    def test_normalizing_twice_changes_nothing(self):
        for value in DETECT_SPELLINGS + REAL_CODES + ("EN", "  ms  "):
            once = wb.normalize_language(value)
            self.assertEqual(wb.normalize_language(once), once,
                             "not idempotent for {0!r}".format(value))

    def test_a_normalized_value_never_raises_on_the_way_back_through(self):
        # This is the failure the idempotence exists to prevent: a backend
        # constructed directly rather than through create_backend() normalizes
        # an already-normalized value in its own __init__.
        for value in DETECT_SPELLINGS + REAL_CODES:
            wb.normalize_language(wb.normalize_language(value))


class RejectedInput(unittest.TestCase):

    def test_full_names_are_refused(self):
        # Deliberate, and the docstring says so: "english" is not a Whisper
        # language code, and quietly mapping it would make the app accept a
        # spelling the server would not.
        for name in ("english", "English", "spanish", "malay"):
            with self.assertRaises(wb.BackendError):
                wb.normalize_language(name)

    def test_an_unknown_code_is_refused(self):
        for code in ("xx", "eng", "en-US", "zz"):
            with self.assertRaises(wb.BackendError):
                wb.normalize_language(code)

    def test_the_message_names_the_input_and_offers_auto(self):
        try:
            wb.normalize_language("english")
        except wb.BackendError as e:
            message = str(e)
        else:
            self.fail("'english' was accepted")
        self.assertIn("english", message)
        self.assertIn("auto", message)

    def test_it_raises_backend_error_not_a_bare_value_error(self):
        # Callers catch BackendError to turn a bad setting into a message
        # rather than a traceback; a ValueError would go straight through.
        with self.assertRaises(wb.BackendError):
            wb.normalize_language("nope")


class _Info(object):
    """What faster-whisper returns beside its segment generator."""

    language = "en"
    language_probability = 1.0
    all_language_probs = None
    transcription_options = None


class _FakeModel(object):
    """Records the kwargs LocalBackend.transcribe builds, and runs nothing."""

    def __init__(self):
        self.kwargs = None

    def transcribe(self, buffer, **kwargs):
        self.kwargs = kwargs
        return [], _Info()


def _server(language):
    """A ServerBackend handle without a server: check=False skips the ping."""
    return wb.ServerBackend("http://127.0.0.1:8080", check=False,
                            language=language)


def _local(language):
    """A LocalBackend without the model load, which is the expensive half."""
    backend = object.__new__(wb.LocalBackend)
    backend.language = wb.normalize_language(language)
    backend.last_detection = None
    backend.model = _FakeModel()
    return backend


class BothBackendsSpellDetectDifferently(unittest.TestCase):
    """The reason the helper exists: None is stored, never sent."""

    def test_the_server_sends_the_literal_auto_when_unpinned(self):
        form = _server(None)._form(False, {})
        self.assertEqual(form["language"], "auto")

    def test_the_server_sends_the_code_when_pinned(self):
        form = _server("EN")._form(False, {})
        self.assertEqual(form["language"], "en")

    def test_the_server_sends_auto_for_every_detect_spelling(self):
        for spelling in DETECT_SPELLINGS:
            form = _server(spelling)._form(False, {})
            self.assertEqual(form["language"], "auto",
                             "{0!r} reached the wire wrong".format(spelling))

    def test_faster_whisper_is_handed_none_when_unpinned(self):
        backend = _local(None)
        backend.transcribe(b"", options={})
        # Not "auto": faster-whisper rejects the string outright, so the two
        # backends cannot share a spelling and the store-None rule is what
        # keeps them from trying.
        self.assertIsNone(backend.model.kwargs["language"])

    def test_faster_whisper_is_handed_the_code_when_pinned(self):
        backend = _local("  ZH ")
        backend.transcribe(b"", options={})
        self.assertEqual(backend.model.kwargs["language"], "zh")

    def test_faster_whisper_never_sees_the_string_auto(self):
        for spelling in DETECT_SPELLINGS:
            backend = _local(spelling)
            backend.transcribe(b"", options={})
            self.assertIsNone(backend.model.kwargs["language"],
                              "{0!r} reached the model wrong".format(spelling))

    def test_the_two_backends_agree_on_what_is_pinned(self):
        # Same setting in, same decision out - which is what makes a mid-
        # session fallback from GPU to CPU keep transcribing the same language.
        for value in ("en", "EN", " ja "):
            served = _server(value)
            local = _local(value)
            self.assertEqual(served.language, local.language)


class ConstructorsNormalize(unittest.TestCase):

    def test_the_server_backend_normalizes_in_init(self):
        self.assertIsNone(_server("AUTO").language)
        self.assertEqual(_server(" En ").language, "en")

    def test_a_bad_code_fails_the_server_backend_at_construction(self):
        with self.assertRaises(wb.BackendError):
            _server("english")

    def test_auto_backend_validates_before_building_anything(self):
        # whisper_backends.py:1173 puts this ahead of the try block on
        # purpose, so a bad code is a startup error even when the server is
        # down and the constructor would otherwise go load the CPU model.
        with mock.patch.object(wb, "ServerBackend") as server:
            with self.assertRaises(wb.BackendError):
                wb.AutoBackend("http://127.0.0.1:8080", "model.bin",
                               language="english")
        self.assertFalse(server.called,
                         "a rejected language still built a backend")


class RepinningKeepsTheRunningPin(unittest.TestCase):
    """set_language revalidates, and assigns only once it returns."""

    def test_the_server_backend_keeps_its_pin_on_a_bad_code(self):
        backend = _server("en")
        with self.assertRaises(wb.BackendError):
            backend.set_language("english")
        # Not None: blanking it here would silently drop a session into
        # auto-detect halfway through, which is worse than refusing.
        self.assertEqual(backend.language, "en")

    def test_the_local_backend_keeps_its_pin_on_a_bad_code(self):
        backend = _local("en")
        with self.assertRaises(wb.BackendError):
            wb.LocalBackend.set_language(backend, "xx")
        self.assertEqual(backend.language, "en")

    def test_a_good_repin_takes_effect_on_the_wire(self):
        backend = _server("en")
        backend.set_language("AUTO")
        self.assertIsNone(backend.language)
        self.assertEqual(backend._form(False, {})["language"], "auto")


if __name__ == "__main__":
    unittest.main()
