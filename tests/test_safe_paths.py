r"""
Read paths: what an untrusted caller may name, and that both doors ask.

The companion to test_remote_locked.py, and the half that a per-key lock could
not do. REMOTE_LOCKED settles the WRITE side by naming keys; a wav path arrives
by two doors and only one of them is a settings patch, so this side is settled
by shape at the sink instead:

    door 1  browser -> Pipeline.apply(remote=True) -> settings["file_path"]
                    -> FileSource               -> wave.open
    door 2  browser -> _command "benchmark"     -> Pipeline.benchmark(wav=...)
                    -> benchmark.load_wav       -> wave.open

Two of these tests are about ORDER rather than outcome, and they are the ones
that matter most: on Windows, resolving `\\host\share\x.wav` IS an outbound
authentication, so a check that runs after something has already stat-ed the
value has already done the thing it then refuses.

Needs no WPF, no model, no audio and no network - and the UNC cases are
written so that a regression fails the assertion rather than reaching for the
network to find out.
"""
import os
import shutil
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_sources  # noqa: E402
import benchmark as bench  # noqa: E402
import pipeline as pipeline_mod  # noqa: E402
import safe_paths  # noqa: E402
import settings as settings_mod  # noqa: E402

UNC = r"\\attacker.example\share\payload.wav"
UNC_SLASH = "//attacker.example/share/payload.wav"


def short_name(path):
    """The 8.3 form of `path`, or None where Windows does not offer one."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024):
            return buf.value
    except (AttributeError, OSError):
        pass
    return None


def write_wav(path, seconds=0.1):
    """A real 16-bit PCM WAV, so a refusal cannot be confused with a bad file."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


class TheRules(unittest.TestCase):
    """check_read_path itself. No filesystem, no pipeline."""

    def refuse(self, path, **kw):
        with self.assertRaises(safe_paths.PathRefused) as caught:
            safe_paths.check_read_path(path, **kw)
        return str(caught.exception)

    def test_unc_is_refused_in_both_spellings(self):
        # Backslashes and forward slashes are the same path to Windows, and a
        # check written against only the one people type is not a check.
        for spelling in (UNC, UNC_SLASH):
            self.assertIn("network", self.refuse(spelling).lower())

    def test_device_and_extended_prefixes_are_refused(self):
        for path in (r"\\.\pipe\anything.wav", r"\\?\C:\x.wav"):
            self.assertIn("network", self.refuse(path).lower())

    def test_reserved_device_names_are_refused_with_an_extension_on(self):
        # The trap: CON.wav is not a file called CON. Windows resolves the
        # reserved word before the suffix, in every directory, so an extension
        # check alone lets the console through.
        for name in ("CON.wav", "nul.wav", r"C:\audio\COM1.wav", "CON .wav"):
            self.assertIn("reserved", self.refuse(name).lower())

    def test_an_alternate_data_stream_is_refused(self):
        self.assertIn("stream", self.refuse(r"C:\audio\ok.wav:hidden").lower())

    def test_anything_but_wav_is_refused(self):
        # Both readers want 16-bit PCM WAV regardless, so this costs an
        # untrusted caller nothing and removes "read any file" as a shape.
        for name in ("secrets.txt", "id_rsa", "notes.wav.txt"):
            self.assertIn(".wav", self.refuse(name))

    def test_empty_and_null_bytes_are_refused(self):
        self.assertIn("No WAV file", self.refuse("   "))
        self.assertIn("null byte", self.refuse("ok.wav\x00.txt"))

    def test_a_relative_path_comes_back_absolute(self):
        got = safe_paths.check_read_path("clip.wav")
        self.assertTrue(os.path.isabs(got), got)

    def test_trusted_keeps_the_paths_a_person_is_allowed_to_name(self):
        # The whole point of the trust parameter. Someone at the keyboard may
        # transcribe a WAV on a NAS; refusing that would be enforcing a threat
        # model that does not describe them.
        #
        # Safe to assert on a UNC string here because trusted returns after
        # abspath, which is GetFullPathNameW - lexical, and no connection.
        got = safe_paths.check_read_path(UNC, trusted=True)
        self.assertTrue(got.endswith("payload.wav"), got)
        self.assertEqual(safe_paths.check_read_path("notes.txt", trusted=True),
                         os.path.abspath("notes.txt"))

    def test_the_message_says_how_to_widen_the_folder_rule(self):
        # The person who hits the roots rule is usually the machine's owner,
        # who needs the name of the way out rather than a refusal.
        msg = self.refuse(r"C:\elsewhere\x.wav", roots=[r"C:\allowed"])
        self.assertIn(safe_paths.ROOTS_ENV, msg)


class TheRoots(unittest.TestCase):
    """Containment: the one rule with a real cost, so the one that is conditional."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roots-")
        self.inside = write_wav(os.path.join(self.tmp, "inside.wav"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inside_a_root_is_allowed(self):
        self.assertEqual(
            safe_paths.check_read_path(self.inside, roots=[self.tmp]),
            os.path.realpath(self.inside))

    def test_outside_every_root_is_refused(self):
        other = tempfile.mkdtemp(prefix="roots-other-")
        try:
            outside = write_wav(os.path.join(other, "outside.wav"))
            with self.assertRaises(safe_paths.PathRefused):
                safe_paths.check_read_path(outside, roots=[self.tmp])
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_dot_dot_cannot_climb_out(self):
        escape = os.path.join(self.tmp, "sub", "..", "..", "escaped.wav")
        with self.assertRaises(safe_paths.PathRefused):
            safe_paths.check_read_path(escape, roots=[self.tmp])

    def test_a_sibling_prefix_is_not_inside(self):
        # The string test everyone writes first: startswith() says
        # C:\tmp\roots-1-evil is inside C:\tmp\roots-1. commonpath says it is
        # not, which is why _within uses it.
        with self.assertRaises(safe_paths.PathRefused):
            safe_paths.check_read_path(self.tmp + "-evil\\x.wav",
                                       roots=[self.tmp])

    def test_a_short_8_3_root_still_contains_its_files(self):
        # Found by this suite, not by reading the code: %TEMP% is commonly
        # C:\Users\ICECRE~1\... and realpath expands the FILE to its long
        # form, so comparing the two refused everything under such a root.
        # Containment failing SHUT, which from the outside is indistinguishable
        # from containment working - the owner of the machine would have been
        # told their own audio folder was off limits.
        short = short_name(self.tmp)
        if not short or short == self.tmp:
            self.skipTest("no 8.3 short name for this temp directory")
        self.assertEqual(
            safe_paths.check_read_path(self.inside, roots=[short]),
            os.path.realpath(self.inside))

    def test_the_env_var_adds_folders(self):
        before = os.environ.get(safe_paths.ROOTS_ENV)
        os.environ[safe_paths.ROOTS_ENV] = self.tmp
        try:
            roots = safe_paths.audio_roots()
            self.assertEqual(roots[0], safe_paths.APP_DIR)
            self.assertIn(os.path.abspath(self.tmp), roots)
        finally:
            if before is None:
                os.environ.pop(safe_paths.ROOTS_ENV, None)
            else:
                os.environ[safe_paths.ROOTS_ENV] = before


class ThePolicy(unittest.TestCase):
    """Pipeline._audio_roots - the only judgement call, and where it belongs."""

    def pipe(self, **settings):
        return pipeline_mod.Pipeline(settings, emit=lambda k, d: None)

    def test_no_listener_means_no_folder_rule(self):
        self.assertIsNone(self.pipe(web=False)._audio_roots())

    def test_a_loopback_listener_means_no_folder_rule(self):
        # The browser on the other end of 127.0.0.1 is almost always the owner
        # of the machine, and confining THEM to the checkout would break
        # capture="file" for the common case to defend against nobody.
        for host in ("127.0.0.1", "::1", "localhost"):
            self.assertIsNone(self.pipe(web=True, web_host=host)._audio_roots(),
                              host)

    def test_an_exposed_listener_turns_the_folder_rule_on(self):
        roots = self.pipe(web=True, web_host="0.0.0.0")._audio_roots()
        self.assertIsNotNone(roots)
        self.assertEqual(list(roots or [])[0], safe_paths.APP_DIR)


class DoorOneTheSettingsPatch(unittest.TestCase):
    """file_path arriving in a patch, and the order the check runs in."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="door1-")
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self.pipe = pipeline_mod.Pipeline({}, emit=lambda k, d: None)

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_browser_cannot_name_a_unc_path(self):
        changed, errors = self.pipe.apply({"capture": "file",
                                           "file_path": UNC}, remote=True)

        self.assertNotIn("file_path", changed)
        self.assertTrue(any("network" in e.lower() for e in errors), errors)
        self.assertEqual(self.pipe.settings["file_path"], "")

    def test_the_check_runs_BEFORE_anything_stats_the_value(self):
        # The load-bearing one. validate()'s capture="file" rule calls
        # os.path.exists(file_path), and for a UNC value that call is the
        # attack: Windows connects to the host and offers the current user's
        # credentials while merely resolving the name. Asserting on the
        # refusal alone would pass just as happily with the check bolted on
        # after validate(), by which point the connection has been made.
        seen = []
        real = settings_mod.os.path.exists

        def spy(path):
            seen.append(str(path))
            return real(path)

        allowed = write_wav(os.path.join(self.tmp, "allowed.wav"))
        settings_mod.os.path.exists = spy
        try:
            self.pipe.apply({"capture": "file", "file_path": UNC}, remote=True)
            refused = list(seen)
            # Positive control, and not optional. An assertion that a spy saw
            # NOTHING passes just as well when the spy was never wired to
            # anything - which is how a test that proves an ordering ends up
            # proving nothing at all. So prove the spy fires, on a patch of
            # exactly the same shape that is allowed through.
            self.pipe.apply({"capture": "file", "file_path": allowed},
                            remote=True)
        finally:
            settings_mod.os.path.exists = real

        self.assertEqual([p for p in refused if "attacker" in p], [],
                         "the refused path was resolved before being refused")
        self.assertTrue(any("allowed.wav" in p for p in seen),
                        "validate() never stats file_path - this test is "
                        "watching the wrong call and proves nothing")

    def test_a_local_wav_still_works_from_the_browser(self):
        # The feature that locking file_path would have cost, kept.
        wav = write_wav(os.path.join(self.tmp, "clip.wav"))
        changed, errors = self.pipe.apply({"file_path": wav}, remote=True)

        self.assertEqual(errors, [])
        self.assertEqual(changed["file_path"], os.path.realpath(wav))

    def test_clearing_the_box_is_not_an_attack(self):
        wav = write_wav(os.path.join(self.tmp, "clip.wav"))
        self.pipe.apply({"file_path": wav}, remote=True)
        changed, errors = self.pipe.apply({"file_path": ""}, remote=True)

        self.assertEqual(errors, [])
        self.assertEqual(changed, {"file_path": ""})

    def test_the_desktop_panel_may_still_name_anything(self):
        changed, errors = self.pipe.apply({"file_path": UNC}, remote=False)

        self.assertEqual(errors, [])
        self.assertTrue(changed["file_path"].endswith("payload.wav"))


class WhoChoseIt(unittest.TestCase):
    """The taint mark: identical strings, different callers, different rules."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="taint-")
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self.wav = write_wav(os.path.join(self.tmp, "clip.wav"))
        self.pipe = pipeline_mod.Pipeline({"capture": "file"},
                                          emit=lambda k, d: None)
        self.calls = []
        self._real = pipeline_mod.audio_sources.create_source
        pipeline_mod.audio_sources.create_source = self.record

    def tearDown(self):
        pipeline_mod.audio_sources.create_source = self._real
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def record(self, settings, on_audio, on_log=None, trusted=False,
               roots=None):
        self.calls.append({"trusted": trusted, "roots": roots})

        class Stub(object):
            name = "stub"

            def start(self):
                pass

            def stop(self):
                pass

            def alive(self):
                # _build_source ends in _push_state -> status(), which asks.
                return True
        return Stub()

    def test_a_browsers_value_builds_an_untrusted_source(self):
        self.pipe.apply({"file_path": self.wav}, remote=True)
        self.pipe._build_source()
        self.assertFalse(self.calls[-1]["trusted"])

    def test_the_panel_typing_over_it_makes_it_trusted_again(self):
        # The mark belongs to the VALUE in the dict, not to the history of the
        # key: once the local panel has set it, it is the panel's value and
        # carrying the browser's mark forward would refuse a path the owner of
        # the machine just typed.
        # A real second file: capture is "file" here, so validate() stats the
        # new value and drops a patch naming a WAV that is not there - which
        # would leave the mark in place and pass this test for the wrong
        # reason.
        other = write_wav(os.path.join(self.tmp, "other.wav"))
        self.pipe.apply({"file_path": self.wav}, remote=True)
        self.pipe.apply({"file_path": other}, remote=False)
        self.pipe._build_source()
        self.assertTrue(self.calls[-1]["trusted"])

    def test_a_value_from_the_command_line_is_trusted_from_the_start(self):
        pipe = pipeline_mod.Pipeline({"capture": "file",
                                      "file_path": self.wav},
                                     emit=lambda k, d: None)
        pipe._build_source()
        self.assertTrue(self.calls[-1]["trusted"])


class DoorTwoTheBenchmarkCommand(unittest.TestCase):
    """The wav argument: a command, not a patch, so no lock was ever going to reach it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="door2-")
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self.events = []
        self.pipe = pipeline_mod.Pipeline(
            {}, emit=lambda kind, data: self.events.append((kind, data)))
        self.pipe.backend = object()      # past the "no backend" early return

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def errors(self):
        return [d.get("msg", "") for k, d in self.events
                if k == "benchmark" and d.get("status") == "error"]

    def test_a_unc_wav_is_refused(self):
        self.assertIsNone(self.pipe.benchmark(buffers=[4], wav=UNC))
        self.assertTrue(any("network" in m.lower() for m in self.errors()),
                        self.errors())

    def test_a_refused_wav_does_not_pause_live_captions(self):
        # pause(True) stops the worker for the length of a benchmark. Doing it
        # before the path is judged hands an attacker a captions-off switch
        # they do not otherwise have, for a run that is never going to start.
        self.pipe.benchmark(buffers=[4], wav=UNC)
        self.assertFalse(self.pipe._paused.is_set())

    def test_the_sink_refuses_it_too(self):
        # Defence in depth, and the assertion that stays true if someone adds
        # a third caller: load_wav is where wave.open actually happens.
        with self.assertRaises(safe_paths.PathRefused):
            bench.load_wav(UNC)

    def test_the_command_line_is_trusted_at_the_sink(self):
        # benchmark.py main() passes trusted=True, so the refusal a person
        # sees for a real file is about the FORMAT, not about permission.
        eight_bit = os.path.join(self.tmp, "eight.wav")
        with wave.open(eight_bit, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(1)
            w.setframerate(16000)
            w.writeframes(b"\x80" * 1600)
        with self.assertRaises(SystemExit) as caught:
            bench.load_wav(eight_bit, trusted=True)
        self.assertIn("16-bit", str(caught.exception))


class TheOtherSink(unittest.TestCase):
    """FileSource: the same helper, reached through create_source."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="source-")
        self.wav = write_wav(os.path.join(self.tmp, "clip.wav"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, path, **kw):
        return audio_sources.create_source(
            {"capture": "file", "file_path": path}, lambda chunk: None, **kw)

    def test_an_untrusted_unc_path_never_reaches_wave_open(self):
        with self.assertRaises(audio_sources.AudioSourceError) as caught:
            self.build(UNC)
        # AudioSourceError, not PathRefused: create_source promises one error
        # type, and a ValueError out of a constructor would bypass every
        # caller's "could not capture" handling.
        self.assertIn("network", str(caught.exception).lower())

    def test_a_wav_outside_the_roots_is_refused(self):
        with self.assertRaises(audio_sources.AudioSourceError):
            self.build(self.wav, roots=[safe_paths.APP_DIR])

    def test_a_wav_inside_the_roots_opens(self):
        source = self.build(self.wav, roots=[self.tmp])
        try:
            self.assertEqual(source.info["kind"], "file")
        finally:
            source.stop()

    def test_a_trusted_caller_is_not_confined(self):
        source = self.build(self.wav, trusted=True,
                            roots=[safe_paths.APP_DIR])
        try:
            self.assertEqual(source.info["kind"], "file")
        finally:
            source.stop()

    def test_the_other_capture_modes_do_not_take_the_policy(self):
        # A device index cannot be pointed at a network share, so only
        # FileSource is handed the kwargs - and passing them to a source whose
        # __init__ does not accept them would be a TypeError at capture time.
        with self.assertRaises(audio_sources.AudioSourceError) as caught:
            audio_sources.create_source({"capture": "nonsense"},
                                        lambda chunk: None)
        self.assertIn("Unknown capture mode", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
