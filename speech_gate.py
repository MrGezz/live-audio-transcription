"""
speech_gate.py - is there actually speech in this buffer?

Why a gate at all
-----------------
Whisper does not stay quiet when handed audio with no speech in it. Fed room
tone or music it invents plausible text - "Thank you.", subtitle credits,
whatever the training data had in the quiet parts - and that lands in the
transcript looking exactly like something that was said. Every such buffer
also costs a full inference, which on a modest GPU is seconds of real time
spent producing a hallucination.

A loudness threshold is the obvious filter and a poor one: it fires on fans,
hum, keyboards and music just as readily as on speech. Silero VAD is a small
LSTM trained to answer the actual question, runs on CPU in about 16 ms per 8
seconds of audio - well under 1% of one Whisper pass - and separates cleanly
where a level check cannot:

    audio                     mean level    level gate    Silero
    real speech                  0.0824     passes         57% speech
    quiet room, peak 0.03        0.2214*    passes          0% speech
    fan hum, peak 0.05           0.6366*    passes          0% speech
    (* after the per-chunk normalization the capture path used to apply)

Singing is not speech to this model
-----------------------------------
Silero is trained on speech, and sung vocals over music score nowhere near
it. Frames over the 0.5 threshold, per 4-second window:

    speech           66 70 66 67 74 78 36 83 74 35 97
    music + vocals    2  3  2 11  2  6  7 15  0  5
    quiet room        0  0  0  0  0  0
    fan hum           0  0  0  0  0  0

Two things follow. Music sits an order of magnitude below speech, so the
default 250 ms (8 frames) rejects most of it - correctly, for a speech tool,
but not if you are trying to caption lyrics. And the knob that fixes it is
min_speech_ms, not threshold: lowering the threshold moves every source
toward passing at once (at 0.05 music reaches 3/6 windows, but room tone
reaches 2/6), whereas steady noise scores exactly zero frames at 0.5, so
asking for fewer of them admits singing and still cannot admit noise.

No new dependency: onnxruntime and a bundled silero model both arrive with
faster-whisper. If neither is present this degrades to None and the caller
falls back to its loudness threshold, so a server-only install still runs.
"""

import glob
import os

import numpy as np

SAMPLERATE = 16000
HOP = 512               # 32 ms at 16 kHz - Silero's frame size
CONTEXT = 64            # extra left-context samples the v6 export expects


def _bundled_model():
    """The silero ONNX that ships inside faster-whisper, if it is installed."""
    try:
        import faster_whisper
    except ImportError:
        return None
    assets = os.path.join(os.path.dirname(faster_whisper.__file__), "assets")
    # Globbed rather than hardcoded: the filename has already changed once
    # across faster-whisper versions (silero_vad.onnx -> silero_vad_v6.onnx).
    found = sorted(glob.glob(os.path.join(assets, "silero_vad*.onnx")))
    return found[-1] if found else None


class SpeechGate(object):
    """
    Wraps a Silero VAD ONNX session. Build it with create(), which returns
    None rather than raising when VAD simply is not available.
    """

    def __init__(self, session, style, model_path, threshold, min_speech_ms):
        self._session = session
        self._style = style          # "batched" (h/c) or "streaming" (state/sr)
        # Which optional inputs this particular export actually declares. The
        # upstream repo ships streaming models both with and without `sr`
        # (silero_vad_half and silero_vad_openvino_16k omit it), and feeding an
        # input a model does not declare is a hard InvalidArgument at run time.
        self._inputs = set(i.name for i in session.get_inputs())
        self.model_path = model_path
        self.threshold = threshold
        # 32 ms per frame, so this is how many consecutive-ish frames have to
        # look like speech before the buffer counts. A single hot frame is
        # usually a click or a door.
        self.min_frames = max(1, int(round(min_speech_ms / 1000.0 * SAMPLERATE / HOP)))

    @classmethod
    def create(cls, model_path=None, threshold=0.5, min_speech_ms=250):
        """A gate, or None with one line explaining why not."""
        try:
            import onnxruntime as ort
        except ImportError:
            print("[vad] onnxruntime not installed - falling back to the loudness "
                  "threshold. Install it with: pip install onnxruntime")
            return None

        path = model_path or _bundled_model()
        if not path or not os.path.exists(path):
            print("[vad] no silero model found - falling back to the loudness "
                  "threshold. Point --vad-model at a silero_vad*.onnx to enable it.")
            return None

        try:
            # Some silero exports emit shape warnings on every single call.
            # They are harmless and would bury the captions.
            ort.set_default_logger_severity(3)
            opts = ort.SessionOptions()
            # One thread: this runs on the transcription worker, between
            # inferences, and is far too small to be worth a thread pool.
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            session = ort.InferenceSession(
                path, sess_options=opts, providers=["CPUExecutionProvider"])
        except Exception as e:
            print("[vad] could not load '{0}' ({1}) - falling back to the "
                  "loudness threshold.".format(path, e))
            return None

        names = set(i.name for i in session.get_inputs())
        if {"h", "c"} <= names:
            style = "batched"
        elif "state" in names:
            style = "streaming"
        else:
            print("[vad] unrecognized silero export (inputs: {0}) - falling back "
                  "to the loudness threshold.".format(sorted(names)))
            return None

        gate = cls(session, style, path, threshold, min_speech_ms)

        # Actually run it once before handing it over. Input names identify the
        # export style but do not prove the call succeeds, and the worker calls
        # speech_frames() outside the try/except that guards transcribe() - so
        # a model that loads and then throws would kill the worker thread and
        # leave the app running with no captions and nothing in the log. Two
        # frames, roughly a millisecond, converts that into the documented
        # fallback.
        try:
            gate.speech_frames(np.zeros(HOP * 2, dtype=np.float32))
        except Exception as e:
            print("[vad] '{0}' loaded but could not be run ({1}) - falling back "
                  "to the loudness threshold.".format(path, e))
            return None
        return gate

    # -- inference ---------------------------------------------------------
    @staticmethod
    def _frames(sig):
        """
        Split into [n, CONTEXT + HOP] frames.

        Both export styles want CONTEXT samples of history in front of each
        512-sample hop, even though their declared input shape is dynamic and
        happily accepts a bare 512. Feeding it without the context does not
        error - it just returns near-zero probability for everything, which
        reads exactly like "no speech here" and silences the transcript.
        """
        n = len(sig) // HOP
        padded = np.concatenate([np.zeros(CONTEXT, dtype=np.float32), sig])
        return np.stack([padded[i * HOP:i * HOP + CONTEXT + HOP]
                         for i in range(n)]).astype(np.float32)

    def _probs_batched(self, sig):
        """v6 export: every frame in a single call, state passed as h/c."""
        h = np.zeros((1, 1, 128), dtype=np.float32)
        c = np.zeros((1, 1, 128), dtype=np.float32)
        out = self._session.run(
            None, {"input": self._frames(sig), "h": h, "c": c})
        return np.asarray(out[0]).reshape(-1)

    def _probs_streaming(self, sig):
        """Older export: one frame per call, LSTM state carried between them."""
        state = np.zeros((2, 1, 128), dtype=np.float32)
        sr = np.array(SAMPLERATE, dtype=np.int64)
        probs = []
        for frame in self._frames(sig):
            feed = {"input": frame.reshape(1, -1), "state": state}
            if "sr" in self._inputs:
                feed["sr"] = sr
            out = self._session.run(None, feed)
            probs.append(float(np.asarray(out[0]).reshape(-1)[0]))
            state = np.asarray(out[1], dtype=np.float32)
        return np.asarray(probs, dtype=np.float32)

    def speech_frames(self, buffer):
        """How many 32 ms frames look like speech. 0 for a too-short buffer."""
        sig = np.asarray(buffer, dtype=np.float32).reshape(-1)
        if len(sig) < HOP:
            return 0
        if self._style == "batched":
            probs = self._probs_batched(sig)
        else:
            probs = self._probs_streaming(sig)
        return int((probs > self.threshold).sum())

    def has_speech(self, buffer):
        """True if the buffer is worth sending to Whisper."""
        return self.speech_frames(buffer) >= self.min_frames
