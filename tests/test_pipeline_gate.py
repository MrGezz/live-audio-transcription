"""
The two things that made vad_neg_threshold and vad_min_silence_ms inert: a
setting that validated, logged and echoed to both panels but reached
neither call site in Pipeline._build_gate, and a create() default that
disagreed with settings.py. Both call sites are exercised against a
recording stand-in for SpeechGate.create, so no model is needed.
"""
import unittest
from unittest import mock

import pipeline as pipeline_mod
import settings
import speech_gate
from tests.fakes import fake_gate

# Every gate setting, and the create()/configure() keyword it must arrive as.
GATE_ARGS = {"vad_threshold": "threshold",
             "vad_min_speech_ms": "min_speech_ms",
             "vad_neg_threshold": "neg_threshold",
             "vad_min_silence_ms": "min_silence_ms"}


class RecordingCreate(object):
    """Stands in for SpeechGate.create: remembers its kwargs, returns a fake."""

    def __init__(self):
        self.calls = []

    def __call__(self, model_path=None, **kwargs):
        self.calls.append(dict(kwargs, model_path=model_path))
        return fake_gate(**kwargs)


class BuildGate(unittest.TestCase):
    def setUp(self):
        self.create = RecordingCreate()
        patcher = mock.patch.object(speech_gate.SpeechGate, "create",
                                    self.create)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.events = []
        self.pipe = pipeline_mod.Pipeline(
            {}, emit=lambda kind, data: self.events.append((kind, data)))

    def test_create_site_passes_every_gate_setting(self):
        self.pipe._build_gate()
        self.assertEqual(len(self.create.calls), 1)
        call = self.create.calls[0]
        for key, arg in GATE_ARGS.items():
            with self.subTest(key=key):
                self.assertIn(arg, call)
                self.assertEqual(call[arg], settings.DEFAULTS[key])
        self.assertEqual(self.pipe.gate.min_silence_ms, 400.0)
        self.assertAlmostEqual(self.pipe.gate.neg_threshold, 0.35)

    def test_configure_site_passes_every_gate_setting(self):
        self.pipe._build_gate()
        gate = self.pipe.gate
        self.pipe.settings.update(vad_threshold=0.3, vad_min_speech_ms=64,
                                  vad_neg_threshold=0.2,
                                  vad_min_silence_ms=900)
        self.pipe._build_gate()
        self.assertIs(self.pipe.gate, gate)             # retuned, not rebuilt
        self.assertEqual(len(self.create.calls), 1)
        self.assertEqual(gate.threshold, 0.3)
        self.assertEqual(gate.min_frames, 2)
        self.assertAlmostEqual(gate.neg_threshold, 0.2)
        self.assertEqual(gate.min_silence_ms, 900.0)

    def test_the_log_line_reports_both_halves_of_the_tuning(self):
        self.pipe._build_gate()
        lines = [d["msg"] for k, d in self.events
                 if k == "log" and d["msg"].startswith("Speech gate")]
        self.assertEqual(len(lines), 1)
        self.assertIn("needs 8 frames = 250 ms of speech", lines[0])
        self.assertIn("below 0.35 after 400 ms of silence", lines[0])

    def test_off_drops_the_gate(self):
        self.pipe._build_gate()
        self.pipe.settings["vad"] = False
        self.pipe._build_gate()
        self.assertIsNone(self.pipe.gate)

    def test_the_panel_path(self):
        """apply() validates and queues; the worker drains; the gate moves."""
        self.pipe._build_gate()
        applied, errors = self.pipe.apply(
            {"vad_min_silence_ms": 900, "vad_neg_threshold": 0.25},
            remote=True)
        self.assertEqual(errors, [])
        self.assertEqual(applied, {"vad_min_silence_ms": 900.0,
                                   "vad_neg_threshold": 0.25})
        self.pipe._drain_pending()      # a no-op here: nothing is running
        self.assertEqual(self.pipe.gate.min_silence_ms, 900.0)
        self.assertAlmostEqual(self.pipe.gate.neg_threshold, 0.25)
        echoed = [d for k, d in self.events if k == "settings"][-1]
        self.assertEqual(echoed["vad_min_silence_ms"], 900.0)

    def test_out_of_range_is_refused_and_the_gate_is_left_alone(self):
        self.pipe._build_gate()
        applied, errors = self.pipe.apply({"vad_min_silence_ms": 99999},
                                          remote=True)
        self.assertEqual(applied, {})
        self.assertEqual(len(errors), 1)
        self.pipe._drain_pending()
        self.assertEqual(self.pipe.gate.min_silence_ms, 400.0)

    def test_both_knobs_rebuild_the_gate(self):
        for key in ("vad_neg_threshold", "vad_min_silence_ms"):
            with self.subTest(key=key):
                self.assertIn("gate", settings.rebuilds_for({key: 1}))


class CommandLine(unittest.TestCase):
    def test_flags_reach_the_gate(self):
        parser = settings.build_parser("test")
        cfg, _args = settings.from_args(
            parser, ["--vad-min-silence-ms", "700",
                     "--vad-neg-threshold", "0.20"])
        with mock.patch.object(speech_gate.SpeechGate, "create",
                               RecordingCreate()):
            pipe = pipeline_mod.Pipeline(cfg, emit=lambda k, d: None)
            pipe._build_gate()
        self.assertEqual(pipe.gate.min_silence_ms, 700.0)
        self.assertAlmostEqual(pipe.gate.neg_threshold, 0.20)


if __name__ == "__main__":
    unittest.main()
