"""
The session log that replaced the two console windows.

console.py is what makes hiding the launcher's window an acceptable trade:
everything the window would have shown goes to logs/ instead. So the tests
that matter are about not losing output - that the tee is installed before
anything can print, that it survives a stream which throws, and that hide()
refuses outright when there is nothing catching the output.

Nothing here opens a window or hides one. hide() and restore() are tested
only at the contract they can be tested at: what they do with no console
to act on, which is what a test runner gives them.
"""
import os
import shutil
import sys
import tempfile
import unittest

import console


class ConsoleCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lat-console-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        # Every test starts with nothing installed, and leaves it that way -
        # capture() replaces sys.stdout process-wide, and a test that left it
        # replaced would tee the rest of the suite into a deleted temp file.
        self.addCleanup(console._close)
        console._close()

    def read(self):
        with open(console.path(), encoding="utf-8") as fh:
            return fh.read()

    def logs(self):
        return sorted(f for f in os.listdir(self.dir)
                      if f.startswith("session_"))


class WhatIsWritten(ConsoleCase):
    def test_print_reaches_both_the_console_and_the_file(self):
        real = sys.stdout
        path = console.capture(self.dir)
        self.assertTrue(path and os.path.exists(path))
        self.assertIsNot(sys.stdout, real)
        print("hello from the app")
        self.assertIn("hello from the app", self.read())

    def test_the_timestamp_goes_at_line_starts_only(self):
        # print() alone makes two write() calls, the text and the newline,
        # and stamping each one would put a time inside the sentence.
        console.capture(self.dir)
        print("one two three")
        line = self.read().strip()
        self.assertRegex(line, r"^\d\d:\d\d:\d\d\s+one two three$")

    def test_a_part_line_is_stamped_once_when_it_completes(self):
        console.capture(self.dir)
        sys.stdout.write("half ")
        sys.stdout.write("and half\n")
        self.assertRegex(self.read().strip(),
                         r"^\d\d:\d\d:\d\d\s+half and half$")

    def test_stderr_is_tagged_so_a_traceback_is_findable(self):
        console.capture(self.dir)
        sys.stderr.write("Traceback (most recent call last):\n")
        self.assertIn("ERR Traceback", self.read())

    def test_record_writes_without_printing(self):
        # whisper-server's per-request chatter: it belongs in the file and
        # nowhere else, so it must not travel through sys.stdout.
        console.capture(self.dir)
        seen = []
        sys.stdout.write = lambda text: seen.append(text)
        console.record("SRV", "operator (): processing 'buffer.wav'")
        self.assertEqual(seen, [])
        self.assertIn("SRV operator (): processing", self.read())

    def test_record_splits_a_multi_line_block(self):
        console.capture(self.dir)
        console.record("SRV", "first\nsecond\n")
        body = self.read()
        self.assertIn("SRV first", body)
        self.assertIn("SRV second", body)

    def test_record_before_capture_is_a_no_op(self):
        # A plain console run never calls capture(), and the server keeps its
        # own window there - so nothing should create a file behind its back.
        console.record("SRV", "nobody is listening")
        self.assertIsNone(console.path())
        self.assertEqual(self.logs(), [])


class WhenThingsGoWrong(ConsoleCase):
    def test_a_console_that_throws_does_not_cost_the_log(self):
        # The window can be closed out from under the process. Losing the
        # visible copy is not a reason to lose the logged one.
        #
        # Built directly over a stub rather than by patching the real
        # sys.stdout: _close() restores the OBJECT, so a write() patched
        # onto the underlying stream would outlive this test and take the
        # rest of the suite's printing with it. It did, once.
        class Dead(object):
            def write(self, _text):
                raise IOError("the console went away")

            def flush(self):
                raise IOError("the console went away")

        path = os.path.join(self.dir, "direct.log")
        with open(path, "w", encoding="utf-8") as handle:
            tee = console._Tee(Dead(), handle, "   ")
            tee.write("still worth keeping\n")
            tee.flush()
        with open(path, encoding="utf-8") as fh:
            self.assertIn("still worth keeping", fh.read())

    def test_isatty_answers_for_the_console_not_the_file(self):
        # Asked by anything deciding whether to colour its output. A log
        # file is never a terminal, and saying it is would put escape
        # sequences in it.
        class Stub(object):
            def isatty(self):
                return True

        self.assertTrue(console._Tee(Stub(), None, "   ").isatty())
        self.assertFalse(console._Tee(None, None, "   ").isatty())

    def test_an_unwritable_directory_installs_nothing(self):
        # A run that cannot write a log is still a run that can transcribe:
        # capture() gives up quietly and leaves printing exactly as it was.
        blocked = os.path.join(self.dir, "a-file")
        with open(blocked, "w") as fh:
            fh.write("a file where a directory would have to go")
        real_out, real_err = sys.stdout, sys.stderr
        self.assertIsNone(console.capture(blocked))
        self.assertIs(sys.stdout, real_out)
        self.assertIs(sys.stderr, real_err)
        self.assertIsNone(console.path())
        # And with nothing catching the output, the window may not be hidden.
        self.assertFalse(console.hide())

    def test_capture_twice_keeps_the_first_file(self):
        first = console.capture(self.dir)
        second = console.capture(self.dir)
        self.assertEqual(first, second)
        self.assertEqual(len(self.logs()), 1)

    def test_close_puts_the_real_streams_back(self):
        real_out, real_err = sys.stdout, sys.stderr
        console.capture(self.dir)
        console._close()
        self.assertIs(sys.stdout, real_out)
        self.assertIs(sys.stderr, real_err)
        self.assertIsNone(console.path())


class Rotation(ConsoleCase):
    def test_only_the_newest_are_kept(self):
        for i in range(6):
            open(os.path.join(self.dir, "session_2026010{0}_000000.log"
                              .format(i)), "w").close()
        console.capture(self.dir, keep=3)
        # Two old ones survive alongside the new one, which is the third.
        self.assertEqual(len(self.logs()), 3)
        self.assertIn("session_20260105_000000.log", self.logs())
        self.assertNotIn("session_20260101_000000.log", self.logs())

    def test_an_empty_directory_is_fine(self):
        self.assertTrue(console.capture(self.dir, keep=2))
        self.assertEqual(len(self.logs()), 1)


class Hiding(ConsoleCase):
    def test_it_refuses_while_nothing_is_catching_the_output(self):
        # The one rule that is not about windows: hiding a console with no
        # tee installed is output thrown away, not output moved.
        self.assertFalse(console.hide())

    def test_restore_is_safe_when_it_never_hid(self):
        console.restore()
        console.capture(self.dir)
        console.restore()

    def test_it_reports_whether_it_actually_went(self):
        # True or False depending on whether this process has a console at
        # all - a test runner may not. Either answer is correct; returning
        # None, or throwing, is not, because app.py prints on the strength
        # of it.
        console.capture(self.dir)
        went = console.hide()
        self.assertIn(went, (True, False))
        console.restore()


if __name__ == "__main__":
    unittest.main()
