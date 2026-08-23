"""
A SpeechGate with no model behind it, for tests about everything downstream
of the model.

LevelSession stands in for the onnxruntime session: the probability it
returns for each 32 ms hop is the peak level of that hop. So a test writes
the probabilities it wants directly, as a script of (seconds, probability)
spans, and framing, the state machine, GateResult and configure() all run
on them unchanged. Nothing here imports onnxruntime or opens a model file,
so the suite runs on a machine with neither.
"""
import numpy as np

from speech_gate import SpeechGate, SAMPLERATE, HOP, CONTEXT

FRAME = float(HOP) / SAMPLERATE        # 0.032 s, one probability's worth
SPEECH, QUIET = 0.9, 0.02              # clear of 0.5 / 0.35 on either side


class LevelSession(object):
    """Looks enough like an onnxruntime InferenceSession for SpeechGate."""

    class _Input(object):
        def __init__(self, name):
            self.name = name

    def get_inputs(self):
        return [self._Input(n) for n in ("input", "h", "c")]

    def run(self, _output_names, feed):
        frames = feed["input"]                          # [n, CONTEXT + HOP]
        return [np.abs(frames[:, CONTEXT:]).max(axis=1).astype(np.float32)]


def fake_gate(threshold=0.5, min_speech_ms=250, neg_threshold=0.0,
              min_silence_ms=400):
    """A SpeechGate over a LevelSession, same keywords as create()."""
    return SpeechGate(LevelSession(), "batched", "<fake>", threshold,
                      min_speech_ms, neg_threshold, min_silence_ms)


def script(*spans):
    """
    Audio whose per-frame probabilities are the script.

    script((30 * FRAME, SPEECH), (8 * FRAME, QUIET)) is thirty frames
    scoring 0.9 followed by eight scoring 0.02. Spans are constant levels,
    so every hop wholly inside one reads back as exactly its probability; a
    hop straddling a boundary reads as the higher of the two. Write span
    lengths in whole frames wherever the test counts them.
    """
    parts = [np.full(int(round(sec * SAMPLERATE)), float(p), dtype=np.float32)
             for sec, p in spans]
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
