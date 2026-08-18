"""
speech_gate.py - is there speech in this buffer, and where did it stop?

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
LSTM trained to answer the actual question, runs on CPU in under 20 ms per 8
seconds of audio (an 8 s buffer measured 16 - 19 ms across runs, 11.3 s
21 - 28 ms, 49.3 s 90 - 111 ms, of which the segment scan below is 0.39 ms
over 1540 frames) - well under 1% of one Whisper pass - and separates
cleanly where a level check cannot:

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

Which weights get loaded
------------------------
The newest silero_vad*.onnx in _models\\ wins, and the copy packaged inside
faster-whisper is the fallback, so a fresh clone still runs - _models\\ is
gitignored, exactly as it is for the GGML weights. Dropping in
_models\\silero_vad_v6.2.onnx measures strictly better than the packaged
export. Frames over 0.5:

    speech, 11.3 s                       271 -> 277
    speech, 49.3 s                      1270 -> 1298
    room tone                              0 -> 0
    fan hum                                2 -> 0    (fewer false positives)
    speech under hum 6 dB louder than it  61 -> 141  (buried speech found)

"Newest" means the version numbers in the filename, not the name itself:
sorted() puts "silero_vad_v6.2.onnx" BEFORE "silero_vad_v6.onnx", because
'2' < 'o', so a lexical max would load the older weights the day both files
sit in the same folder.

Where the speech stopped, not just how much of it there was
-----------------------------------------------------------
A frame count answers "is this buffer worth an inference". The
silence_at_end_of_chunk buffering strategy asks a different question: has
the talker finished, or does the window merely end in the middle of a
sentence? That needs the position of the last speech frame - and the pass
already computed it, because a per-frame probability array is precisely what
gets reduced to a count. speech_frames() threw the rest away; analyze()
returns it as `segments`, (start, end) in seconds, and `last_speech_end`, so
the strategy can hold a chunk back until the last speech sits at least
chunk_offset seconds before the end of the buffer. This is a wider report
from the one pass that was already being paid for, not a second model and
not a second inference.

Finding those segments is a state machine, not a run-finder
------------------------------------------------------------
Thresholding the probabilities and taking runs of True is the obvious way
and it reports the wrong end. _speech_runs() is a port of
faster-whisper's get_speech_timestamps (faster_whisper/vad.py), which has
three things a run-finder has not, each one moving the reported end of
speech - the number silence_at_end_of_chunk cuts on:

  * HYSTERESIS. Speech starts above `threshold` and only ends below
    `neg_threshold` (0 derives it as threshold - 0.15). With one threshold
    for both, a frame dipping to 0.49 inside a word reads as the end of a
    sentence. How much it moves depends on how often the probabilities
    actually visit the band between the two, and Silero is confident: at
    threshold 0.5 only 2 frames of 352 (11.3 s) and 9 of 1540 (49.3 s) land
    in [0.35, 0.5), so the segment count does not change there and the end
    of speech moves one frame later - 10.528 -> 10.560 s and 48.544 ->
    48.576 s - which is precisely the number the strategy cuts on. Turn the
    threshold down to 0.2, where 52 of those 1540 frames land in the band,
    and the counts move too: 13 segments -> 9 on the 49.3 s sample and
    4 -> 2 on the 11.3 s one, hysteresis off against on.
  * min_silence_ms. A gap only ends speech once it has lasted this long.
    Segment counts and mean segment length at threshold 0.5:

        min_silence_ms       100          160          500
        speech, 11.3 s    4 / 2.23s    4 / 2.23s    2 / 4.77s
        speech, 49.3 s   13 / 3.21s   13 / 3.21s    6 / 7.36s

    100 and 160 tie here because this speaker leaves 0.29 - 0.42 s between
    words and 0.83 - 0.86 s between sentences, so both settings split at all
    12 gaps and neither is anywhere near the sentences. 6 segments is the
    sentence count. faster-whisper defaults this to 2000 ms, which is right
    for splitting a recording and far too slow for live captions;
    settings.py defaults it to 160 ms, which is the "do not split on a
    breath" setting rather than the "split on sentences" one.
  * min_speech_ms. Runs shorter than this are dropped, so one hot frame from
    a door or a click no longer registers as speech - and no longer defines
    where speech "ended". At 1000 ms the 0.896 s segment at 42.66 s in the
    49.3 s sample stops being reported at all, 13 segments -> 12.

Gaps are counted in whole 32 ms frames from the frame after the one that
started them, which is upstream's arithmetic: 100 ms asks for 3 frames and
so closes on the 4th quiet frame (128 ms), 160 ms closes on the 6th
(192 ms), 500 ms on the 17th (544 ms).

speech_pad_ms is deliberately NOT ported. Upstream pads each segment because
it then CUTS the audio at those timestamps, where a hard edge clips the
first phoneme; we only ever use the timestamps to decide something about a
buffer we then send to Whisper whole. Padding here would push
last_speech_end later than the speech actually was and make the
trailing-silence test lie. Please do not "restore" it.

Cutting in the silence instead of at the deadline
--------------------------------------------------
When the force-cut length is reached mid-sentence, something has to give.
Cutting at the deadline lands wherever the deadline falls, which is usually
inside a word: mean level over +/- 50 ms at the 8.0 s mark of the 49.3 s
sample measured 0.0756, and 0.0656 - 0.0756 at the other deadlines tried.
GateResult.pauses lists the silences between speech runs longest first, and
best_cut() returns the middle of the longest one that fits: at limit_s=8
that is 3.408 s, where the same window measures 0.0000 - the pauses in this
sample run 0.00001 - 0.0006 inside and this one is digital silence - i.e.
two to five orders of magnitude quieter. The midpoint rather than either
edge, because both halves need the quiet: the earlier chunk gets trailing
silence, which is what lets Whisper finish the sentence instead of trailing
off, and the later one gets leading silence instead of opening mid-word.

That is upstream's max_speech_duration_s handling, moved to where the
deadline lives. Upstream keeps `possible_ends` inside the scan and, on
reaching the limit, splits at the longest of them rather than at the limit
itself; our limit is not a property of the VAD at all - it is buffering.py's
force-cut length, applied after the pass - so the same choice is offered as
best_cut(limit_s=...) over the pauses the scan reported.

No new dependency: onnxruntime and a bundled silero model both arrive with
faster-whisper. If neither is present this degrades to None and the caller
falls back to its loudness threshold, so a server-only install still runs.
"""

import glob
import os
import re

import numpy as np

SAMPLERATE = 16000
HOP = 512               # 32 ms at 16 kHz - Silero's frame size
CONTEXT = 64            # extra left-context samples the v6 export expects
MIN_PAUSE_MS = 98       # upstream's min_silence_at_max_speech default

_MODELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_models")


def _version_key(path):
    """
    Sort key for silero filenames: the version numbers, then the name.

    Numeric and not lexical because "silero_vad_v6.2.onnx" sorts BEFORE
    "silero_vad_v6.onnx" as text ('2' < 'o'), so max() over the raw names
    would load the older weights out of a folder holding both.
    """
    name = os.path.basename(path)
    return ([int(n) for n in re.findall(r"\d+", name)], name)


def _newest(pattern):
    """The highest-versioned file matching `pattern`, or None."""
    found = glob.glob(pattern)
    return max(found, key=_version_key) if found else None


def _bundled_model():
    """
    Newest silero_vad*.onnx in _models\\, else the one in faster-whisper.

    _models\\ is where this project already expects you to drop weights (the
    GGML models live there) and it is gitignored, so preferring it makes an
    upgrade a file copy while a fresh clone still starts on the packaged
    export. That fallback is a real one, not a formality: it is the
    difference between 271 and 277 speech frames on the same 11.3 s sample,
    not between working and not.
    """
    local = _newest(os.path.join(_MODELS, "silero_vad*.onnx"))
    if local:
        return local
    try:
        import faster_whisper
    except ImportError:
        return None
    assets = os.path.join(os.path.dirname(faster_whisper.__file__), "assets")
    # Globbed rather than hardcoded: the filename has already changed once
    # across faster-whisper versions (silero_vad.onnx -> silero_vad_v6.onnx).
    return _newest(os.path.join(assets, "silero_vad*.onnx"))


def _speech_runs(probs, threshold, neg_threshold, min_silence_frames):
    """
    Runs of speech as (start_frame, end_frame), upstream's state machine.

    A port of get_speech_timestamps in faster_whisper/vad.py, counted in
    whole 32 ms frames rather than samples because one frame is what one
    probability covers. `triggered` is "inside speech"; `temp_end` is the
    frame the pending silence started at, and 0 doubles as "no silence
    pending" exactly as upstream's sample offset does - frame 0 can never be
    a temp_end, because reaching that branch needs an earlier frame to have
    triggered speech first.

    Frames rather than seconds, and every run rather than only the long
    ones, because both of the things built from this need the raw article:
    GateResult drops the short runs from .segments, _pauses() does not.
    """
    out = []
    triggered = False
    start = 0
    temp_end = 0

    for i in range(len(probs)):
        prob = probs[i]

        if prob >= threshold and temp_end:
            # Speech came back before the gap lasted long enough to count,
            # so it was a breath inside a sentence, not the end of one.
            temp_end = 0

        if prob >= threshold and not triggered:
            triggered = True
            start = i
            continue

        if prob < neg_threshold and triggered:
            if not temp_end:
                temp_end = i
            # Measured from the frame the gap started on, not including it,
            # which is upstream's arithmetic: min_silence_frames of 5 (the
            # 160 ms default) closes on the 6th quiet frame, 192 ms in.
            if i - temp_end < min_silence_frames:
                continue
            # Ends where the quiet started, not where it was confirmed:
            # min_silence_frames is how long we wait before believing the
            # talker stopped, not part of how long they talked.
            out.append((start, temp_end))
            triggered = False
            temp_end = 0

    if triggered:
        # The buffer ran out mid-segment. End it at temp_end when a silence
        # was already pending: upstream ends at the end of the audio because
        # it is about to cut there and pads the edge anyway, but that would
        # report speech across a stretch every frame of which scored below
        # neg_threshold - and "how long has it been quiet at the end" is the
        # entire question silence_at_end_of_chunk asks this function. Report
        # the buffer end and chunk_offset can never be satisfied early.
        out.append((start, temp_end or len(probs)))
    return out


def _pauses(runs):
    """
    The silences between consecutive speech runs, as (start, end, duration).

    Between the RUNS, not between the segments left after min_speech_ms
    threw the short ones out: a discarded run is still audio with a voice in
    it, and merging the quiet on both sides of it into one long "pause"
    invites best_cut() to cut in the middle of it. Measured on the 49.3 s
    sample at --vad-min-speech-ms 1000, where the 0.896 s run at 42.66 s is
    the one dropped: over segments that reads as a single 1.632 s gap and
    best_cut() answers 43.152 s, where the mean level over +/- 50 ms is
    0.0514 - inside a word, and no better than the 0.0656 - 0.0756 blunt cut
    this whole path exists to avoid. Over runs the same file reports 0.320 s
    and 0.416 s there, neither of them the longest, and the cut stays in
    real silence.

    Sorted longest first rather than left in time order because every caller
    wants the biggest one, and sorted on whole frames rather than on the
    float duration because that float cannot tie: every pause is a whole
    number of 32 ms frames, but end - start over values built as i * 0.032
    differs in the last bit between pauses of the SAME length. The four
    27-frame pauses in the 49.3 s sample came out 0.8639999999999999 to
    0.8640000000000043 and sorted 12.224, 20.480, 2.976, 40.032 - by
    rounding error. Frame counts are integers and do tie, the sort is
    stable, so equal pauses stay in the order they were spoken and
    best_cut() picks the earliest of them.
    """
    step = float(HOP) / SAMPLERATE
    gaps = [(runs[i][1], runs[i + 1][0]) for i in range(len(runs) - 1)]
    gaps.sort(key=lambda gap: gap[1] - gap[0], reverse=True)
    return [(a * step, b * step, (b - a) * step) for a, b in gaps]


class GateResult(object):
    """
    Everything one Silero pass over one buffer knows about it.

    The thresholds and the frame requirements are copied in at construction
    rather than read back off the gate later, so a configure() arriving from
    the web UI thread cannot leave .frames and .has_speech disagreeing about
    which settings produced them.
    """

    __slots__ = ("probs", "threshold", "neg_threshold", "min_frames",
                 "frames", "has_speech", "segments", "last_speech_end",
                 "pauses", "duration")

    def __init__(self, probs, threshold, min_frames, n_samples,
                 neg_threshold, min_silence_frames):
        self.probs = probs
        self.threshold = threshold
        self.neg_threshold = neg_threshold
        self.min_frames = min_frames
        self.duration = n_samples / float(SAMPLERATE)
        # Strictly greater, where the state machine uses upstream's >=. The
        # two differ only for a probability landing exactly on the threshold,
        # and this count is the number every --vad-min-speech-ms setting was
        # chosen against: 271 frames on the 11.3 s sample at 0.5. Moving it
        # by a frame would silently retune every configuration in the field.
        self.frames = int((probs > threshold).sum())
        self.has_speech = self.frames >= min_frames
        runs = _speech_runs(probs, threshold, neg_threshold,
                            min_silence_frames)
        step = float(HOP) / SAMPLERATE
        # min_speech_ms decides what counts as speech, not what counts as a
        # boundary: the short runs leave .segments and stay in .pauses.
        self.segments = [(a * step, b * step)
                         for a, b in runs if b - a >= min_frames]
        self.pauses = _pauses(runs)
        # None rather than 0.0 when nothing was said: to the caller asking
        # "is last_speech_end far enough back to transcribe", 0.0 reads as
        # "someone stopped talking at the very start", which is exactly the
        # answer that sends a buffer of pure room tone to Whisper.
        self.last_speech_end = self.segments[-1][1] if self.segments else None

    def best_cut(self, limit_s=None, min_pause_ms=MIN_PAUSE_MS):
        """
        Where to cut this buffer so the cut lands in silence, or None.

        Considers the pauses ending at or before limit_s (None = anywhere in
        the buffer) that last at least min_pause_ms, and returns the middle
        of the longest one. The middle, not an edge, because the two chunks
        need opposite halves of the quiet: the earlier one needs trailing
        silence for Whisper to close the sentence instead of trailing off,
        the later one needs leading silence so it does not open mid-word.

        None means no pause worth cutting in - continuous speech. What to do
        then cannot be decided here: "no good cut" and "no cut needed" want
        opposite answers, and only the caller knows which it is asking.
        """
        floor = min_pause_ms / 1000.0
        for start_s, end_s, duration_s in self.pauses:      # longest first
            if limit_s is not None and end_s > limit_s:
                continue
            if duration_s >= floor:
                return (start_s + end_s) / 2.0
        return None


class SpeechGate(object):
    """
    Wraps a Silero VAD ONNX session. Build it with create(), which returns
    None rather than raising when VAD simply is not available.
    """

    def __init__(self, session, style, model_path, threshold, min_speech_ms,
                 neg_threshold, min_silence_ms):
        self._session = session
        self._style = style          # "batched" (h/c) or "streaming" (state/sr)
        # Which optional inputs this particular export actually declares. The
        # upstream repo ships streaming models both with and without `sr`
        # (silero_vad_half and silero_vad_openvino_16k omit it), and feeding an
        # input a model does not declare is a hard InvalidArgument at run time.
        self._inputs = set(i.name for i in session.get_inputs())
        self.model_path = model_path
        # Every tuning number lives in one tuple, and the properties below
        # only read it back out - see configure() for the race that separate
        # attributes could not close.
        self._tuning = self._tune(threshold, self._frames_for(min_speech_ms),
                                  neg_threshold, min_silence_ms)

    @staticmethod
    def _tune(threshold, min_frames, neg_threshold, min_silence_ms):
        """
        One tuning tuple from four settings, built in a single expression.

        (threshold, min_frames, neg_requested, neg_effective, min_silence_ms)
        - the requested and the effective negative threshold are both kept
        because 0 means "derive it from the threshold", and that derivation
        has to follow a later threshold change rather than freeze at whatever
        the threshold happened to be when the 0 was stored.
        """
        threshold = float(threshold)
        neg_threshold = float(neg_threshold)
        # Upstream's derivation, from faster_whisper/vad.py: speech ends 0.15
        # below where it starts, floored at 0.01 so a very low threshold
        # cannot derive a negative one that no probability can drop under.
        effective = (neg_threshold if neg_threshold > 0
                     else max(threshold - 0.15, 0.01))
        return (threshold, min_frames, neg_threshold, effective,
                float(min_silence_ms))

    @property
    def threshold(self):
        return self._tuning[0]

    @property
    def min_frames(self):
        return self._tuning[1]

    @property
    def neg_threshold(self):
        """The value actually used - derived when it was configured as 0."""
        return self._tuning[3]

    @property
    def min_silence_ms(self):
        return self._tuning[4]

    @staticmethod
    def _frames_for(ms):
        """
        Milliseconds as a frame count, at least one.

        32 ms per frame, so this is how many frames have to look like speech
        before the buffer counts (a single hot frame is usually a click or a
        door), and how many have to look like silence before the talker has
        stopped rather than drawn breath. Whole frames, so 100 ms of silence
        asks for 3 of them and 500 ms for 16 - see _speech_runs() for the
        frame the gap is measured from.
        """
        return max(1, int(round(ms / 1000.0 * SAMPLERATE / HOP)))

    @classmethod
    def create(cls, model_path=None, threshold=0.5, min_speech_ms=250,
               neg_threshold=0.0, min_silence_ms=160):
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

        gate = cls(session, style, path, threshold, min_speech_ms,
                   neg_threshold, min_silence_ms)

        # Actually run it once before handing it over. Input names identify the
        # export style but do not prove the call succeeds, and the worker calls
        # into the gate outside the try/except that guards transcribe() - so a
        # model that loads and then throws would kill the worker thread and
        # leave the app running with no captions and nothing in the log. Two
        # frames, roughly a millisecond, converts that into the documented
        # fallback.
        try:
            gate.analyze(np.zeros(HOP * 2, dtype=np.float32))
        except Exception as e:
            print("[vad] '{0}' loaded but could not be run ({1}) - falling back "
                  "to the loudness threshold.".format(path, e))
            return None
        # Which weights these captions came from, once. Two exports of the
        # same model disagree by enough to matter - 271 speech frames against
        # 277 on the same 11.3 s sample - so "VAD is on" is not a full answer
        # when someone reports the gate behaving differently than it used to.
        print("[vad] silero model: {0}".format(path))
        return gate

    def configure(self, threshold=None, min_speech_ms=None,
                  neg_threshold=None, min_silence_ms=None):
        """
        Retune a live gate, without rebuilding anything.

        The onnxruntime session does not depend on any of these numbers - all
        of them are applied to the probabilities it returns - so a retune is
        one attribute store rather than the ~100 ms of reloading a session
        rebuild would cost for no change in what the model computes.

        No lock, and none is missing for the reader, because the whole set is
        published in one store: analyze() unpacks _tuning once, so the
        settings it judges a buffer by are always settings somebody actually
        configured together. Plain attributes could not promise that -
        reading each of them once is not the same as reading them together,
        and a retune landing between two reads produced results carrying the
        old threshold against the new frame requirement in 166 of 240
        analyze() calls when measured against a thread retuning in a loop.
        Writers are still assumed to be one thread, which is what the caller
        does: settings patches are applied on the worker.
        """
        now = self._tuning
        frames = (now[1] if min_speech_ms is None
                  else self._frames_for(min_speech_ms))
        self._tuning = self._tune(
            now[0] if threshold is None else threshold, frames,
            now[2] if neg_threshold is None else neg_threshold,
            now[4] if min_silence_ms is None else min_silence_ms)

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

    def analyze(self, buffer):
        """
        One forward pass, reported in full. See GateResult.

        The only place inference happens: speech_frames() and has_speech()
        are views onto this same single pass, so asking for the segments and
        the pauses as well as the count costs nothing over the count alone.
        """
        # One read of one immutable tuple, so everything this result says is
        # judged by settings that were configured together - see configure()
        # for why separate reads were not enough.
        threshold, min_frames, _, neg_threshold, min_silence_ms = self._tuning

        sig = np.asarray(buffer, dtype=np.float32).reshape(-1)
        if len(sig) < HOP:
            # Under one whole frame there is nothing to feed the model, and
            # an empty batch is an onnxruntime error rather than a zero.
            probs = np.zeros(0, dtype=np.float32)
        elif self._style == "batched":
            probs = self._probs_batched(sig)
        else:
            probs = self._probs_streaming(sig)
        return GateResult(probs, threshold, min_frames, len(sig),
                          neg_threshold, self._frames_for(min_silence_ms))

    def speech_frames(self, buffer):
        """
        How many 32 ms frames look like speech. 0 for a too-short buffer.

        A plain count over the probabilities, deliberately independent of the
        segment logic: callers compare it against --vad-min-speech-ms, and
        the state machine's discards would move that number without anyone
        having changed a threshold.
        """
        return self.analyze(buffer).frames

    def has_speech(self, buffer):
        """True if the buffer is worth sending to Whisper."""
        return self.analyze(buffer).has_speech
