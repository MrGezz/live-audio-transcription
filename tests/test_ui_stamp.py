r"""
ui/runtime is committed, so a stale one is invisible. This is the gate.

Every panel test loads the shipped assembly rather than building one. That is
the right call - a clone with no .NET SDK still gets a working panel - and it
is also why the suite cannot tell a stale binary from a fresh one: edit a .cs,
skip build_ui.cmd, and all 17 of them still pass against the previous build,
green and meaningless.

build_ui.cmd now writes ui/runtime/sources.sha256 after a successful publish.
These tests compare it to the working tree, so forgetting the rebuild is a red
suite instead of nothing at all.

They need no pythonnet, no .NET and no ui/runtime, and that is deliberate:
they have to run on the machines where tests/test_panel.py skips, because
those are exactly the machines that would otherwise never notice.

    .venv\Scripts\python.exe -m unittest tests.test_ui_stamp -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOLS = os.path.join(REPO, "ui", "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import uihash  # noqa: E402


class _FakeTree(object):
    """A throwaway ui/ tree, so the digest's rules can be tested on inputs.

    Pointing uihash at a temp directory rather than asserting against the real
    ui/ keeps these tests from restating whatever the real tree happens to
    contain - which would pass no matter what the rules were.
    """

    def __init__(self):
        self.repo = tempfile.mkdtemp(prefix="uihash-")
        self.ui = os.path.join(self.repo, "ui")
        os.makedirs(self.ui)

    def write(self, rel, body):
        path = os.path.join(self.ui, rel.replace("/", os.sep))
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "wb") as fh:
            fh.write(body)

    def _pointed_at_me(self):
        return mock.patch.multiple(uihash, REPO=self.repo, UI_DIR=self.ui)

    def digest(self):
        with self._pointed_at_me():
            return uihash.source_hash()

    def files(self):
        with self._pointed_at_me():
            return uihash.source_files()

    def close(self):
        shutil.rmtree(self.repo, ignore_errors=True)


class TheCommittedStamp(unittest.TestCase):
    """The gate itself, against the real tree."""

    def test_a_stamp_is_committed(self):
        self.assertTrue(
            os.path.isfile(uihash.STAMP_FILE),
            "ui/runtime/sources.sha256 is missing - run build_ui.cmd")
        stamp = uihash.read_stamp()
        self.assertIsNotNone(stamp)
        self.assertEqual(len(stamp), 64, stamp)
        int(stamp, 16)      # raises if it is not hex

    def test_the_stamp_matches_the_sources(self):
        # The whole point of the file. If this fails, ui/ was edited and
        # ui/runtime was not rebuilt, so every panel test is loading the
        # previous build.
        self.assertEqual(
            uihash.read_stamp(), uihash.source_hash(),
            "ui/runtime is stale - ui/ sources have changed since the "
            "assembly was built. Run build_ui.cmd, which rebuilds and "
            "restamps, then commit ui/runtime with the source change.")

    def test_is_current_agrees(self):
        self.assertTrue(uihash.is_current())

    def test_the_digest_covers_the_tracked_sources(self):
        # The set is the assertion, not a count. A file kind dropping out of
        # SOURCE_SUFFIXES would leave the gate green while a whole category
        # of edit went unnoticed.
        found = set(uihash.source_files())
        self.assertIn("ui/LiveTranscription.Ui.csproj", found)
        self.assertIn("ui/ViewModels/MainVm.cs", found)
        self.assertIn("ui/Themes/Charcoal.xaml", found)
        self.assertIn("ui/Views/MainWindow.xaml.cs", found)
        self.assertTrue(all(f.startswith("ui/") for f in found))


class WhatTheDigestCovers(unittest.TestCase):

    def setUp(self):
        self.tree = _FakeTree()
        self.addCleanup(self.tree.close)
        self.tree.write("App.cs", b"class A {}\n")
        self.tree.write("Themes/Dark.xaml", b"<ResourceDictionary/>\n")
        self.tree.write("Ui.csproj", b"<Project/>\n")
        self.base = self.tree.digest()

    def test_a_source_edit_changes_it(self):
        self.tree.write("App.cs", b"class A { int x; }\n")
        self.assertNotEqual(self.base, self.tree.digest())

    def test_a_new_source_changes_it(self):
        self.tree.write("ViewModels/Vm.cs", b"class Vm {}\n")
        self.assertNotEqual(self.base, self.tree.digest())

    def test_a_xaml_edit_changes_it(self):
        self.tree.write("Themes/Dark.xaml", b"<ResourceDictionary x/>\n")
        self.assertNotEqual(self.base, self.tree.digest())

    def test_a_rename_changes_it(self):
        # Length-prefixing earns its keep here: move the same bytes to another
        # name and the digest has to move too, or a rename reads as no change.
        os.remove(os.path.join(self.tree.ui, "App.cs"))
        self.tree.write("Renamed.cs", b"class A {}\n")
        self.assertNotEqual(self.base, self.tree.digest())

    def test_generated_sources_are_not_hashed(self):
        # WPF regenerates .g.cs and GeneratedInternalTypeHelper.cs under obj/
        # on every build. Hashing those would tie the digest to whether the
        # tree had been built, and the gate would never settle.
        self.tree.write("obj/Release/App.g.cs", b"// generated\n")
        self.tree.write("obj/Release/GeneratedInternalTypeHelper.cs", b"//\n")
        self.tree.write("bin/Release/Leftover.cs", b"// stale\n")
        self.assertEqual(self.base, self.tree.digest())

    def test_the_published_output_is_not_hashed(self):
        # runtime/ is the thing being stamped. Including it would make the
        # stamp depend on itself.
        self.tree.write("runtime/Whatever.cs", b"// shipped\n")
        self.assertEqual(self.base, self.tree.digest())

    def test_the_python_tools_are_not_hashed(self):
        # soak.py, shot.py and gen_theme.py drive the panel from outside.
        # They are not compile inputs, and rebuilding for an edit to one of
        # them would be a rebuild for nothing.
        self.tree.write("tools/soak.py", b"print('measure')\n")
        self.assertEqual(self.base, self.tree.digest())


class PortabilityOfTheDigest(unittest.TestCase):

    def test_line_endings_do_not_change_it(self):
        # .gitattributes normalizes to LF in the repo and checks CRLF back
        # out here. A clone with core.autocrlf off holds LF, compiles to the
        # same assembly, and must not fail this gate - otherwise the stamp
        # reports a mistake nobody made, on every machine but this one.
        crlf = _FakeTree()
        self.addCleanup(crlf.close)
        lf = _FakeTree()
        self.addCleanup(lf.close)
        for tree, eol in ((crlf, b"\r\n"), (lf, b"\n")):
            tree.write("App.cs", eol.join([b"class A", b"{", b"}", b""]))
            tree.write("Themes/D.xaml", eol.join([b"<R/>", b""]))
        self.assertEqual(crlf.digest(), lf.digest())

    def test_the_location_of_the_checkout_does_not_change_it(self):
        # Paths go into the digest repo-relative, so moving the folder - which
        # this project supports everywhere else - must not move the hash.
        one = _FakeTree()
        self.addCleanup(one.close)
        two = _FakeTree()
        self.addCleanup(two.close)
        for tree in (one, two):
            tree.write("App.cs", b"class A {}\n")
        self.assertNotEqual(one.repo, two.repo)
        self.assertEqual(one.digest(), two.digest())

    def test_the_file_list_is_sorted(self):
        # os.walk yields filesystem order. Two clones can hold the same files
        # in a different order on disk, and the digest is sorted so the two
        # agree anyway.
        tree = _FakeTree()
        self.addCleanup(tree.close)
        tree.write("B.cs", b"b\n")
        tree.write("A.cs", b"a\n")
        tree.write("Views/Z.xaml", b"<Z/>\n")
        listed = tree.files()
        self.assertEqual(listed, sorted(listed))
        self.assertTrue(all("\\" not in f for f in listed), listed)


class ReadingTheStamp(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uistamp-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "sources.sha256")
        patch = mock.patch.object(uihash, "STAMP_FILE", self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_missing_stamp_reads_as_none(self):
        # Not an exception: a tree that has never been built is a legitimate
        # state, and it should report as stale rather than crash the suite.
        self.assertIsNone(uihash.read_stamp())
        self.assertFalse(uihash.is_current())

    def test_the_header_comments_are_skipped(self):
        with open(self.path, "w") as fh:
            fh.write("# a comment\n#another\n" + ("a" * 64) + "\n")
        self.assertEqual(uihash.read_stamp(), "a" * 64)

    def test_a_written_stamp_reads_back(self):
        written = uihash.write_stamp()
        self.assertEqual(uihash.read_stamp(), written)
        self.assertTrue(uihash.is_current())


if __name__ == "__main__":
    unittest.main()
