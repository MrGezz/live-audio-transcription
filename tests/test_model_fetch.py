"""
The model catalog, and the gate in front of it.

No network and no downloads: every test here stops at `resolve()`, which is
the function that decides what a name is allowed to become. That is the part
worth pinning, because `download()` is reachable from the browser panel and
the panel can be bound off loopback - so "fetch this and write it into the
program's folder" is a remote-write primitive the moment the caller gets to
influence the URL or the destination.

The rule being tested: a caller supplies a NAME, and every other value -
repo id, filename, destination - is derived here from the catalog row that
name matched. A name that matches no row produces FetchError and nothing
else. This is the same shape as App._engine_start, which basenames a model
and checks it against the real listing rather than trusting it (invariant 19),
and the tests are written the same way - the attack is spelled out in the
case, so a regression fails an assertion instead of reaching the network to
find out.
"""

import os
import shutil
import tempfile
import unittest

import model_fetch


class Catalog(unittest.TestCase):
    """What the pickers are offered."""

    def test_both_kinds_are_listed_with_sizes(self):
        cat = model_fetch.catalog()
        self.assertIn("ggml", cat)
        self.assertIn("faster_whisper", cat)
        self.assertTrue(cat["ggml"], "the GGML catalog is empty")
        self.assertTrue(cat["faster_whisper"], "the faster-whisper catalog is empty")
        for kind in ("ggml", "faster_whisper"):
            for row in cat[kind]:
                self.assertTrue(row["name"], "a catalog row with no name")
                # The size is in the label because for these two settings the
                # number IS the decision - see settings.py on server_model.
                self.assertGreater(row["size_mb"], 0,
                                   "{0}: {1} has no published size".format(
                                       kind, row["name"]))
                self.assertIn("installed", row)

    def test_the_shipped_defaults_are_both_in_the_catalog(self):
        """
        The two model settings default to names this can fetch.

        Otherwise a fresh clone offers a default it cannot obtain, and the
        first thing the download card is used for is the one thing it cannot
        do.
        """
        ggml = [m["name"] for m in model_fetch.catalog()["ggml"]]
        self.assertIn("ggml-base-q5_1.bin", ggml)
        faster = [m["name"] for m in model_fetch.catalog()["faster_whisper"]]
        self.assertIn("medium", faster)

    def test_faster_whisper_names_are_sizes_not_folders(self):
        """
        The catalog names a SIZE; the folder name is derived from it.

        The panels send the name back verbatim, so a catalog that named
        folders would make the local layout part of the wire format.
        """
        for row in model_fetch.catalog()["faster_whisper"]:
            self.assertNotIn("/", row["name"])
            self.assertNotIn("\\", row["name"])
            self.assertEqual(row["dir"], "faster-whisper-" + row["name"])


class TheGate(unittest.TestCase):
    """resolve() is the only way in, and it only accepts catalog rows."""

    def test_a_known_ggml_file_resolves_into_models(self):
        repo, filename, dest = model_fetch.resolve("ggml", "ggml-base-q5_1.bin")
        self.assertEqual(repo, model_fetch.GGML_REPO)
        self.assertEqual(filename, "ggml-base-q5_1.bin")
        self.assertEqual(os.path.normpath(dest),
                         os.path.normpath(model_fetch.MODEL_DIR))

    def test_a_known_faster_whisper_size_resolves_to_a_folder(self):
        repo, filename, dest = model_fetch.resolve("faster_whisper", "large-v3")
        self.assertEqual(repo, "Systran/faster-whisper-large-v3")
        # None, not a filename: a faster-whisper model is a folder of files
        # and takes snapshot_download, which is what this None selects.
        self.assertIsNone(filename)
        self.assertEqual(os.path.basename(dest), "faster-whisper-large-v3")

    def test_every_destination_stays_under_models(self):
        """
        Not one catalog row can resolve outside _models.

        Checked over the whole catalog rather than on a sample, because this
        is the property the design rests on: the destination is derived, so
        there is no row that can be added later which quietly escapes.
        """
        root = os.path.normcase(os.path.abspath(model_fetch.MODEL_DIR))
        cat = model_fetch.catalog()
        for kind in ("ggml", "faster_whisper"):
            for row in cat[kind]:
                _repo, _name, dest = model_fetch.resolve(kind, row["name"])
                dest = os.path.normcase(os.path.abspath(dest))
                self.assertTrue(dest == root or dest.startswith(root + os.sep),
                                "{0} resolved to {1}, outside _models".format(
                                    row["name"], dest))

    def test_a_traversal_is_refused_rather_than_joined(self):
        for evil in ("../../etc/passwd", r"..\..\Windows\System32\x.bin",
                     "/etc/passwd", r"C:\Windows\System32\drivers\etc\hosts",
                     ".."):
            for kind in ("ggml", "faster_whisper"):
                with self.assertRaises(model_fetch.FetchError):
                    model_fetch.resolve(kind, evil)

    def test_a_url_is_not_a_name(self):
        """
        The whole point. A caller never gets to say where the bytes come from.
        """
        for evil in ("https://evil.example/x.bin",
                     "http://127.0.0.1:8771/x.bin",
                     r"\\attacker.example\share\x.bin",
                     "evil/repo"):
            with self.assertRaises(model_fetch.FetchError):
                model_fetch.resolve("ggml", evil)

    def test_a_repo_id_is_not_a_name_either(self):
        """
        Naming the real repo is still refused: the row is matched on the NAME
        column, so the repo id is an output of resolve and never an input.
        """
        with self.assertRaises(model_fetch.FetchError):
            model_fetch.resolve("faster_whisper", "Systran/faster-whisper-large-v3")

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(model_fetch.FetchError):
            model_fetch.resolve("shell", "large-v3")
        # And a name that IS valid for the other kind.
        with self.assertRaises(model_fetch.FetchError):
            model_fetch.resolve("ggml", "large-v3")
        with self.assertRaises(model_fetch.FetchError):
            model_fetch.resolve("faster_whisper", "ggml-base-q5_1.bin")

    def test_the_empty_name_is_refused(self):
        for kind in ("ggml", "faster_whisper"):
            with self.assertRaises(model_fetch.FetchError):
                model_fetch.resolve(kind, "")


class Installed(unittest.TestCase):
    """
    "already here" is read off the disk, not remembered.

    MODEL_DIR is redirected into a temp tree for the whole class: a test that
    writes into the real _models changes what the next launch starts on, which
    is the same reason the settings tests redirect STATE_FILE.
    """

    def setUp(self):
        self._real = model_fetch.MODEL_DIR
        self.tmp = tempfile.mkdtemp(prefix="lat-models-")
        model_fetch.MODEL_DIR = self.tmp

    def tearDown(self):
        model_fetch.MODEL_DIR = self._real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_ggml_file_counts_when_the_file_is_there(self):
        self.assertFalse(model_fetch.installed("ggml", "ggml-base-q5_1.bin"))
        open(os.path.join(self.tmp, "ggml-base-q5_1.bin"), "wb").close()
        self.assertTrue(model_fetch.installed("ggml", "ggml-base-q5_1.bin"))

    def test_a_faster_whisper_folder_needs_its_contents(self):
        """
        An empty folder is not a model.

        A part-finished or hand-made directory would otherwise read as
        installed, and the picker would offer a folder faster-whisper cannot
        open - which fails much later, as a backend that will not build.
        """
        folder = os.path.join(self.tmp, "faster-whisper-small")
        os.makedirs(folder)
        self.assertFalse(model_fetch.installed("faster_whisper", "small"))
        open(os.path.join(folder, "model.bin"), "wb").close()
        self.assertTrue(model_fetch.installed("faster_whisper", "small"))

    def test_config_json_alone_also_counts(self):
        """
        The same two-file test _list_models uses, kept deliberately in step:
        a picker and this card disagreeing about what a model IS would show a
        folder in one and not the other.
        """
        folder = os.path.join(self.tmp, "faster-whisper-base")
        os.makedirs(folder)
        open(os.path.join(folder, "config.json"), "wb").close()
        self.assertTrue(model_fetch.installed("faster_whisper", "base"))


if __name__ == "__main__":
    unittest.main()
