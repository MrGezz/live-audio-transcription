"""
Where the faster-whisper backend runs, and why the default is not the fast one.

`device="cpu"` was hardcoded in LocalBackend from the days this project ran on
a Radeon Pro W5500, where it was simply true: CTranslate2 supports CUDA or CPU
and nothing else, so on that card faster-whisper could never use the GPU. On an
NVIDIA machine the same line is a lie the UI repeated - the field was labelled
"CPU model path".

The interesting rule here is not "let people pick cuda", it is which default
survives contact with the thing this backend exists for. Under backend="auto"
this is the FALLBACK, reached because whisper-server just died, and the reason
it died is very often the GPU. A fallback that needs the GPU is not a fallback,
and `AutoBackend._local_error` is sticky - one failed load and the session has
no CPU path left for as long as it runs. So cpu stays the default, cuda is a
choice, and "auto" means try-cuda-then-settle rather than "cuda if present".

Loads no model and needs no GPU: WhisperModel is replaced by a recorder, which
is the only way to assert the ORDER of attempts rather than the outcome.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import settings as settings_mod  # noqa: E402
import whisper_backends as wb  # noqa: E402

CUBLAS = "Library cublas64_12.dll is not found or cannot be loaded"


class FakeWhisperModel(object):
    """Records every construction, and fails the devices it was told to."""

    calls = []
    fail_on = []

    def __init__(self, model_path, device="cpu", compute_type=None,
                 cpu_threads=None):
        FakeWhisperModel.calls.append(
            {"path": model_path, "device": device, "compute": compute_type})
        if device in FakeWhisperModel.fail_on:
            raise RuntimeError(CUBLAS if device == "cuda" else "nope")


class FakeModule(unittest.TestCase):
    """Stands a fake faster_whisper in front of the import inside __init__."""

    def setUp(self):
        FakeWhisperModel.calls = []
        FakeWhisperModel.fail_on = []
        self._saved = sys.modules.get("faster_whisper")
        module = types.ModuleType("faster_whisper")
        setattr(module, "WhisperModel", FakeWhisperModel)
        sys.modules["faster_whisper"] = module

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = self._saved

    def devices(self):
        return [c["device"] for c in FakeWhisperModel.calls]


class TheDefault(FakeModule):

    def test_cpu_is_the_default_and_never_reaches_for_the_gpu(self):
        # The load-bearing one. If this ever starts trying cuda first, the
        # fallback has acquired a dependency on the hardware it exists to
        # survive the loss of.
        wb.LocalBackend("_models/x")
        self.assertEqual(self.devices(), ["cpu"])

    def test_the_pipeline_default_agrees_with_the_backend_default(self):
        # Two defaults for one behaviour is how they drift apart.
        self.assertEqual(settings_mod.DEFAULTS["local_device"], "cpu")
        self.assertEqual(settings_mod.DEFAULTS["local_compute"], "auto")


class TheOrder(FakeModule):

    def test_auto_tries_cuda_first_then_settles_for_cpu(self):
        FakeWhisperModel.fail_on = ["cuda"]
        backend = wb.LocalBackend("_models/x", device="auto")

        self.assertEqual(self.devices(), ["cuda", "cpu"])
        self.assertEqual(backend.device, "cpu")

    def test_auto_stops_at_cuda_when_cuda_works(self):
        backend = wb.LocalBackend("_models/x", device="auto")

        self.assertEqual(self.devices(), ["cuda"])
        self.assertEqual(backend.device, "cuda")

    def test_cuda_does_not_secretly_fall_back(self):
        # Asking for the GPU and silently getting the CPU is how you spend an
        # afternoon wondering why "GPU" transcription runs at CPU speed. auto
        # exists for people who want the fallback; cuda means cuda.
        FakeWhisperModel.fail_on = ["cuda"]
        with self.assertRaises(wb.BackendError):
            wb.LocalBackend("_models/x", device="cuda")

        self.assertEqual(self.devices(), ["cuda"])


class ThePrecision(FakeModule):

    def test_auto_precision_follows_the_device(self):
        wb.LocalBackend("_models/x", device="cpu")
        self.assertEqual(FakeWhisperModel.calls[-1]["compute"], "int8")

        FakeWhisperModel.calls = []
        wb.LocalBackend("_models/x", device="cuda")
        self.assertEqual(FakeWhisperModel.calls[-1]["compute"], "float16")

    def test_an_explicit_precision_is_passed_straight_through(self):
        # Not validated here on purpose: CTranslate2 owns that enum and its
        # error names the accepted values, which a copy of the list here would
        # eventually contradict.
        wb.LocalBackend("_models/x", device="cuda",
                        compute_type="int8_float16")
        self.assertEqual(FakeWhisperModel.calls[-1]["compute"], "int8_float16")

    def test_the_name_says_what_actually_loaded(self):
        # It used to be the constant "faster-whisper (CPU int8)", which after
        # this change would have been wrong on every GPU load - and this
        # string is what the log line and the panel's backend label show.
        backend = wb.LocalBackend("_models/x", device="cuda")
        self.assertIn("CUDA", backend.name)
        self.assertIn("float16", backend.name)


class TheErrorMessage(FakeModule):

    def test_a_missing_cuda_runtime_names_the_fix(self):
        # The raw CTranslate2 message names cublas64_12.dll, a file the user
        # has never heard of and cannot get by installing the CUDA Toolkit -
        # a CUDA 13 toolkit ships cublas64_13.dll, which is not the same
        # library. Without this the obvious next step is the wrong one.
        FakeWhisperModel.fail_on = ["cuda"]
        with self.assertRaises(wb.BackendError) as caught:
            wb.LocalBackend("_models/x", device="cuda")

        msg = str(caught.exception)
        self.assertIn("nvidia-cublas-cu12", msg)
        self.assertIn("nvidia-cudnn-cu12", msg)
        self.assertIn("Toolkit does not supply it", msg)

    def test_a_plain_cpu_failure_does_not_babble_about_cuda(self):
        FakeWhisperModel.fail_on = ["cpu"]
        with self.assertRaises(wb.BackendError) as caught:
            wb.LocalBackend("_models/x", device="cpu")

        self.assertNotIn("nvidia-cublas-cu12", str(caught.exception))


class TheDllSearchPath(unittest.TestCase):
    """
    Finding the pip-installed CUDA libraries, which nothing else does for you.

    This is the step that looks unnecessary and is not. The wheels drop
    cublas64_12.dll into site-packages\\nvidia\\cublas\\bin, a directory on no
    search path anywhere, and a model will LOAD on cuda without it and then
    die on the first inference - so "it loaded" proves nothing, and a test
    that stopped there would pass against the broken build.
    """

    def setUp(self):
        self._path = os.environ.get("PATH", "")
        self._flag = wb._cuda_path_added
        wb._cuda_path_added = False

    def tearDown(self):
        os.environ["PATH"] = self._path
        wb._cuda_path_added = self._flag

    def test_the_wheel_directories_go_on_PATH(self):
        if os.name != "nt":
            self.skipTest("Windows DLL search order")
        import glob
        import sysconfig
        found = glob.glob(os.path.join(
            sysconfig.get_paths()["purelib"], "nvidia", "*", "bin"))
        if not found:
            self.skipTest("CUDA wheels are not installed here")

        wb._add_cuda_runtime_to_path()
        for d in found:
            self.assertIn(d, os.environ["PATH"])

    def test_it_runs_once(self):
        # Called on every cuda load attempt, and PATH is process-global: with
        # no guard a session that rebuilds the backend a few times grows its
        # own environment block until CreateProcess starts failing.
        wb._add_cuda_runtime_to_path()
        once = os.environ.get("PATH", "")
        wb._add_cuda_runtime_to_path()
        self.assertEqual(os.environ.get("PATH", ""), once)

    def test_it_is_harmless_with_no_wheels_installed(self):
        # The overwhelmingly common case - AMD machines, CPU-only setups - so
        # it must not raise, and must not leave a junk entry behind either.
        #
        # sysconfig.get_paths is patched rather than sys.prefix, and that is
        # not fussiness: sysconfig CACHES its answer, so pointing sys.prefix at
        # a fake directory poisons every later caller in the process. It did -
        # test_the_wheel_directories_go_on_PATH ran after this one, got the
        # cached fake path, found no wheels and SKIPPED itself, reporting
        # success while checking nothing.
        import sysconfig
        before = os.environ.get("PATH", "")
        wb._cuda_path_added = False
        saved = sysconfig.get_paths
        sysconfig.get_paths = lambda *a, **k: {
            "purelib": os.path.join(os.sep, "nope-not-here")}
        try:
            wb._add_cuda_runtime_to_path()
        finally:
            sysconfig.get_paths = saved
        self.assertEqual(os.environ.get("PATH", ""), before)


class TheSetting(unittest.TestCase):
    """The schema half: both panels get these for free, so validate them."""

    def test_the_three_devices_are_accepted(self):
        for value in ("cpu", "cuda", "auto"):
            clean, errors = settings_mod.validate({"local_device": value},
                                                  dict(settings_mod.DEFAULTS))
            self.assertEqual(errors, [], value)
            self.assertEqual(clean["local_device"], value)

    def test_anything_else_is_refused(self):
        _, errors = settings_mod.validate({"local_device": "rocm"},
                                          dict(settings_mod.DEFAULTS))
        self.assertTrue(errors)

    def test_both_fields_rebuild_the_backend(self):
        # Neither can be applied to a loaded model - CTranslate2 fixes the
        # device and the compute type at construction - so a change has to
        # tear the backend down. Tagged rebuild="backend"; without the tag the
        # setting would move in the UI and change nothing at all.
        rebuilds = settings_mod.rebuilds_for(
            {"local_device": "cuda", "local_compute": "float16"},
            dict(settings_mod.DEFAULTS))
        self.assertIn("backend", rebuilds)


if __name__ == "__main__":
    unittest.main()
