"""
Transcription backends for live-audio-transcription.

Why this exists
---------------
Two ASR engines have to look like one thing to the worker loop. whisper.cpp's
HTTP server is fast but only reachable while a separate process is alive;
faster-whisper is always available and several times slower. Everything above
this module wants a single call that returns text, and wants it to keep
returning text when the server dies at minute forty of a meeting.

So the three classes below share one interface:

  ServerBackend  -> whisper.cpp `whisper-server` over HTTP (GPU; the server
                    can be a Vulkan build for AMD/Intel or a CUDA build for
                    NVIDIA - this layer does not care which).
  LocalBackend   -> faster-whisper on CPU (int8). Fallback / no server.
  AutoBackend    -> both of the above: runs on the server, drops to CPU if it
                    dies mid-session, and returns to the server when it comes
                    back. What "--backend auto" builds.

    transcribe(buffer, translate=False, options=None) -> [Segment, ...]
    set_language(code_or_auto)
    .last_detection   what the engine thought the language was, this pass
    .active_name      which engine actually served

`options` is the decode block of settings.py, passed straight through - see
decode_options(). Segment is a 2-tuple subclass, so the older
`for text, lang in results` call sites keep working unchanged while the newer
ones read .words / .probability off the same object.

Usage:
    from whisper_backends import create_backend
    backend = create_backend("auto", server_url="http://127.0.0.1:8080",
                             model_path=r"_models\faster-whisper-medium",
                             language="auto")
    results = backend.transcribe(float32_mono_16k_buffer, translate=False)
    for text, lang in results:
        ...
"""

import io
import struct
import time
import wave

import numpy as np

SAMPLERATE = 16000

# Whisper's fixed language set (openai/whisper tokenizer LANGUAGES, plus the
# large-v3 "yue"), as code -> full name.
#
# Kept as a mapping rather than a bare set because the two backends speak
# different halves of it. Codes are what everything accepts as INPUT:
# whisper.cpp also tolerates full names, faster-whisper accepts codes only and
# raises ValueError otherwise, so an unvalidated "--language english" would run
# on the GPU and then kill transcription the moment the server died and the CPU
# fallback engaged. Names are what whisper-server hands back as OUTPUT, where
# faster-whisper hands back a code - so captions would read [english] on GPU
# and [en] on CPU for the same audio. The reverse map below settles that.
WHISPER_LANGUAGES = {
    "en": "english", "zh": "chinese", "de": "german", "es": "spanish",
    "ru": "russian", "ko": "korean", "fr": "french", "ja": "japanese",
    "pt": "portuguese", "tr": "turkish", "pl": "polish", "ca": "catalan",
    "nl": "dutch", "ar": "arabic", "sv": "swedish", "it": "italian",
    "id": "indonesian", "hi": "hindi", "fi": "finnish", "vi": "vietnamese",
    "he": "hebrew", "uk": "ukrainian", "el": "greek", "ms": "malay",
    "cs": "czech", "ro": "romanian", "da": "danish", "hu": "hungarian",
    "ta": "tamil", "no": "norwegian", "th": "thai", "ur": "urdu",
    "hr": "croatian", "bg": "bulgarian", "lt": "lithuanian", "la": "latin",
    "mi": "maori", "ml": "malayalam", "cy": "welsh", "sk": "slovak",
    "te": "telugu", "fa": "persian", "lv": "latvian", "bn": "bengali",
    "sr": "serbian", "az": "azerbaijani", "sl": "slovenian", "kn": "kannada",
    "et": "estonian", "mk": "macedonian", "br": "breton", "eu": "basque",
    "is": "icelandic", "hy": "armenian", "ne": "nepali", "mn": "mongolian",
    "bs": "bosnian", "kk": "kazakh", "sq": "albanian", "sw": "swahili",
    "gl": "galician", "mr": "marathi", "pa": "punjabi", "si": "sinhala",
    "km": "khmer", "sn": "shona", "yo": "yoruba", "so": "somali",
    "af": "afrikaans", "oc": "occitan", "ka": "georgian", "be": "belarusian",
    "tg": "tajik", "sd": "sindhi", "gu": "gujarati", "am": "amharic",
    "yi": "yiddish", "lo": "lao", "uz": "uzbek", "fo": "faroese",
    "ht": "haitian creole", "ps": "pashto", "tk": "turkmen", "nn": "nynorsk",
    "mt": "maltese", "sa": "sanskrit", "lb": "luxembourgish", "my": "myanmar",
    "bo": "tibetan", "tl": "tagalog", "mg": "malagasy", "as": "assamese",
    "tt": "tatar", "haw": "hawaiian", "ln": "lingala", "ha": "hausa",
    "ba": "bashkir", "jw": "javanese", "su": "sundanese", "yue": "cantonese",
}

_NAME_TO_CODE = dict((name, code) for code, name in WHISPER_LANGUAGES.items())

# How many runners-up last_detection keeps. The readout exists to answer "is
# it wavering, and between what?"; past about five the tail is noise.
DETECTION_CANDIDATES = 5

# The shortest buffer ServerBackend will put on the wire. whisper.cpp reads 200
# samples past samples[1] while reflect-padding, before its own length check
# runs, so a buffer under 201 samples is a heap over-read in the server
# process - measured here as a hard segfault at 16 samples. One quarter second
# is far above that and matches buffering.MIN_FLUSH_SAMPLES, so the two floors
# agree rather than each having their own opinion.
MIN_SERVER_SAMPLES = SAMPLERATE // 4


def is_marker(text):
    """
    Is this segment a non-speech marker rather than something anyone said?

    Whisper narrates what it hears when it hears no words: "[BLANK_AUDIO]",
    "(indistinct)", "[MUSIC PLAYING]", "(upbeat music)". Those are annotations,
    not transcription, and nothing downstream distinguished them - a silent
    buffer that reached the server came back as the caption "[BLANK_AUDIO]",
    which the overlay showed and the transcript file kept. Reproduced with a
    quarter second of digital silence.

    Deliberately narrow: only text that is ENTIRELY one bracketed group
    counts. A caption that merely contains a bracket - someone reading an
    address, a segment ending "(laughs)" after real words - is left alone,
    because dropping half a sentence to remove an annotation is the worse
    error of the two.
    """
    stripped = text.strip()
    if len(stripped) < 3:
        return False
    pairs = (("[", "]"), ("(", ")"), ("*", "*"), ("<", ">"))
    for opener, closer in pairs:
        if stripped[0] == opener and stripped[-1] == closer:
            # Whole-string only: "[a] and [b]" closes and reopens, and is two
            # markers around real text rather than one marker.
            return closer not in stripped[1:-1]
    return False


def language_code(value):
    """
    Whatever a backend reported, expressed as a code: "english" -> "en".

    Already-a-code and unknown values pass through untouched, so a model that
    reports something outside the table still shows up in the caption rather
    than being swallowed into "??".
    """
    if not value:
        return value
    key = value.strip().lower()
    if key in WHISPER_LANGUAGES:
        return key
    return _NAME_TO_CODE.get(key, value)


class BackendError(RuntimeError):
    """Raised when a backend cannot be created or reached."""


def normalize_language(language):
    """
    One spelling of "detect it" for backends that disagree about the word.

    None / "" / "auto" (any case) -> None. The HTTP API wants the literal
    string "auto" and faster-whisper wants None, so every backend stores None
    and re-spells it at the call site instead of passing the flag through raw.

    Idempotent, so backends can normalize again in their own __init__ and stay
    correct when constructed directly rather than through create_backend().
    """
    if language is None:
        return None
    language = language.strip().lower()
    if language in ("", "auto"):
        return None
    if language not in WHISPER_LANGUAGES:
        raise BackendError(
            "Unknown language '{0}'. Use a Whisper language code - mostly two "
            "letters, e.g. en, es, fr, de, ja, zh, ms, id - or 'auto' to "
            "detect it per request. Full names such as 'english' are not "
            "accepted.".format(language)
        )
    return language


# -------------------------------
# Per-request decode options
# -------------------------------
# The decode keys of settings.py, spelled exactly as settings.py spells them
# so a whole settings dict can be handed over without a translation table in
# between - a translation table is where the three-copies drift started.
DECODE_OPTIONS = (
    "temperature", "temperature_inc", "beam_size", "best_of", "audio_ctx",
    "initial_prompt", "carry_initial_prompt", "no_speech_thold",
    "entropy_thold", "logprob_thold", "max_len", "split_on_word",
    "suppress_nst", "max_context", "language_probabilities",
    "word_timestamps",
)

# word_thold is deliberately absent and should stay absent. It is the word
# timestamp probability threshold, and it changes no number this app reads:
# measured on the same 36-word window at 0.01, 0.5 and 0.9, the reported
# per-word probabilities came back identical at all three. Exposing it would
# add a slider that moves nothing.

# Values settings.py uses to mean "leave the backend's own default alone".
# Mostly because they are NOT the same request when sent literally:
# faster-whisper raises on beam_size=0, and audio_ctx=0 would hand the encoder
# no context at all. max_len=0 is the exception - whisper-server rewrites a
# zero max_len to its own 60-character wrap, so sending it would be a field
# that changes nothing - and it is dropped here anyway so that "no cap" is one
# absent field rather than a value each backend has to re-interpret.
_KEEP_DEFAULT = {"beam_size": 0, "best_of": 0, "audio_ctx": 0,
                 "max_context": -1, "max_len": 0}


def decode_options(source=None, **overrides):
    """
    A settings dict (or any subset of one) reduced to the decode fields that
    actually say something, with the "keep the default" sentinels dropped.

    Filtering here rather than at each call site means both backends see the
    same question - "was this asked for?" - instead of each re-deriving what a
    0 means from a different direction.
    """
    merged = dict(source or {})
    merged.update(overrides)
    out = {}
    for key in DECODE_OPTIONS:
        if key not in merged:
            continue
        value = merged[key]
        if value is None:
            continue
        if key in _KEEP_DEFAULT and value == _KEEP_DEFAULT[key]:
            continue
        if key == "initial_prompt" and not str(value).strip():
            continue
        out[key] = value
    return out


def _float(value, default=None):
    """float(value) that answers `default` instead of raising."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _no_detection():
    """The 'nothing has been decoded yet' shape of .last_detection."""
    return {"language": None, "probability": None, "candidates": [],
            "pinned": False, "detected": None, "disagrees": False}


def _detection(pinned, reported, probability, candidates):
    """
    One honest .last_detection, whether or not a language was pinned.

    The obvious version puts the pin in "language" and leaves the detector's
    number in "probability", which reads as confidence in the pin and is not:
    with --language ms, whisper-server still runs detection and answers
    detected_language "english" at 0.999, so the readout claimed "ms (100%)"
    while the 100% belonged to English. faster-whisper made the same shape
    worse from the other side - under a pin it skips detection entirely and
    hands back language_probability 1.0, a perfect score for a measurement
    that never happened - so the same pin read 99% on GPU and 100% on CPU.

    A pin has no confidence, so there is none to report. What the detector
    thought is kept separately, because it answers a question worth asking:
    whether the language you pinned is the one being spoken.
    """
    if not pinned:
        return {"language": reported or "??", "probability": probability,
                "candidates": candidates, "pinned": False,
                "detected": None, "disagrees": False}
    detected = None
    if reported and reported != pinned:
        detected = {"language": reported, "probability": probability}
    return {
        "language": pinned,
        "probability": None,
        "candidates": [],
        "pinned": True,
        "detected": detected,
        # Only worth surfacing when the detector is actually sure, otherwise a
        # pin over noisy audio would nag on every buffer - which is the very
        # thing pinning was chosen to stop.
        "disagrees": bool(detected and (probability or 0.0) >= 0.85),
    }


def _language_pairs(probabilities):
    """
    language_probabilities -> [(code, prob), ...], best first, all of them.

    Routed through language_code() for the same reason the caption label is:
    whisper-server names its languages and faster-whisper codes them, so an
    untranslated list would rename every entry the moment an auto fallback
    happened - the same audio reporting differently mid-session.

    Shape-tolerant on purpose: whisper.cpp has shipped this as an object and
    as an array of pairs across builds, and a readout is not worth a crash.
    """
    if not probabilities:
        return []
    if isinstance(probabilities, dict):
        items = list(probabilities.items())
    else:
        items = []
        for entry in probabilities:
            if isinstance(entry, dict):
                name = entry.get("language", entry.get("lang"))
                items.append((name, entry.get("probability", entry.get("p"))))
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                items.append((entry[0], entry[1]))
    pairs = []
    for name, probability in items:
        value = _float(probability)
        if value is None or not name:
            continue
        pairs.append((language_code(str(name)), value))
    pairs.sort(key=lambda kv: kv[1], reverse=True)
    return pairs


def _candidates(probabilities):
    """The top few of _language_pairs, for the detected-language readout."""
    return _language_pairs(probabilities)[:DETECTION_CANDIDATES]


# A distribution is treated as carrying no information once its best entry is
# within this factor of dead uniform (1/n). Real detection on real speech sits
# far higher - the same clip that produced the flat report below scores above
# 0.9 on a multilingual model.
UNIFORM_TOLERANCE = 1.5

# Fallback when the probabilities were suppressed and only the winner's number
# came back. Uniform over Whisper's 100 languages is 0.01; a genuine detection
# is not remotely this low.
DETECTION_FLOOR = 0.05


def _flat_language_report(probabilities, probability):
    """
    True when the server's language report is uniform, i.e. meaningless.

    An English-only model (ggml-*.en) has no language tokens, so there is
    nothing to detect with - but whisper-server answers the question anyway. It
    fills language_probabilities with a flat distribution over all 100
    languages and names the arg-max as the detected language. Measured here on
    ggml-small.en-q5_1 with 11 seconds of clear English: every entry 0.01002,
    and the winner reported as "serbian".

    Left alone, that random label rides along on every caption and changes from
    window to window, which looks exactly like the real "auto-detect wandered"
    problem that pinning a language exists to solve - except no setting can fix
    it, because nothing is detecting anything.

    Judged from the numbers rather than the model's file name: the name is not
    in the response, and --server-url can point at a server this process did
    not start.
    """
    pairs = _language_pairs(probabilities)
    if pairs:
        return (len(pairs) >= 10
                and pairs[0][1] <= UNIFORM_TOLERANCE / len(pairs))
    if probability is not None:
        return probability <= DETECTION_FLOOR
    return False


# -------------------------------
# One segment, two ways to read it
# -------------------------------
class Segment(tuple):
    """
    A transcribed segment that is still exactly the (text, language) tuple
    this module has always returned.

    Subclassing tuple rather than reaching for a dataclass is the whole point:
    live_transcription.py, live_transcription_lite.py and benchmark.py all
    unpack the result as `for text, lang in results`, and a third element or a
    plain object breaks every one of them. As a 2-tuple subclass the old
    unpacking is byte-for-byte what it was, while the confidence colouring the
    web client wants reads .words off the same object.

    .probability is the mean of the word probabilities, which is the number
    worth showing per line - avg_logprob is Whisper's own score, but it is a
    log, so it does not average into anything a reader can act on.
    """

    # No __slots__ here: CPython rejects a non-empty __slots__ on subclasses
    # of variable-length built-ins such as tuple.

    def __new__(cls, text, language, words=None, start=None, end=None,
                no_speech_prob=None, avg_logprob=None):
        segment = tuple.__new__(cls, (text, language))
        segment.text = text
        segment.language = language
        segment.words = list(words or [])
        segment.start = start
        segment.end = end
        segment.no_speech_prob = no_speech_prob
        segment.avg_logprob = avg_logprob
        scores = [w["probability"] for w in segment.words
                  if w.get("probability") is not None]
        segment.probability = (sum(scores) / len(scores)) if scores else None
        return segment

    def __getnewargs__(self):
        # tuple's own would hand __new__ a single 2-tuple argument, and the
        # copy would come back with text=("...", "en") and no language.
        return (self.text, self.language, self.words, self.start, self.end,
                self.no_speech_prob, self.avg_logprob)

    def as_dict(self):
        """JSON-serialisable form, for the transcript file and the web UI."""
        return {
            "text": self.text,
            "language": self.language,
            "start": self.start,
            "end": self.end,
            "probability": self.probability,
            "no_speech_prob": self.no_speech_prob,
            "avg_logprob": self.avg_logprob,
            "words": [dict(w) for w in self.words],
        }


def _word(word, start, end, probability):
    """One word in the shape both backends are normalised to."""
    return {"word": word, "start": start, "end": end,
            "probability": probability}


# -------------------------------
# whisper.cpp server backend (GPU: Vulkan or CUDA build)
# -------------------------------
class ServerBackend(object):
    name = "whisper.cpp server (GPU)"
    active_name = "server"      # see AutoBackend.active_name

    def __init__(self, url="http://127.0.0.1:8080", timeout=30, check=True,
                 language=None):
        try:
            import requests  # noqa: F401 - validated here, used per-call
        except ImportError:
            raise BackendError(
                "The 'requests' package is required for the server backend. "
                "Install it with: pip install requests"
            )
        self._requests = __import__("requests")
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.language = normalize_language(language)
        self.last_detection = _no_detection()
        # Said once per server handle, not once per buffer: an English-only
        # model reports a flat distribution on every single request, and a
        # notice repeated every two seconds is noise, not information.
        self._flat_reported = False
        # check=False builds a handle to a server that is NOT up yet, so
        # AutoBackend can keep polling it after a fallback. Without it the
        # only way to get a ServerBackend is to already have a live server.
        if check:
            self.ping()

    def set_language(self, language):
        """
        Re-pin the spoken language without rebuilding anything.

        Revalidated through normalize_language and assigned only once it
        returns, so a rejected code leaves the running pin intact instead of
        blanking it into auto-detect halfway through a session.
        """
        self.language = normalize_language(language)

    def ping(self, timeout=3):
        """Cheap liveness check. Raises BackendError if the server is not up."""
        try:
            # /health answers ~15 bytes of {"status":"ok"} where / returns the
            # 2130-byte index page, and it reports on the server rather than
            # on a static file. It only appeared in later whisper.cpp builds,
            # so a 404 falls back to the old test: any HTTP response at all
            # from / means the process is answering.
            reply = self._requests.get(self.url + "/health", timeout=timeout)
            if reply.status_code == 404:
                self._requests.get(self.url + "/", timeout=timeout)
        except Exception as e:
            raise BackendError(
                "Cannot reach whisper-server at {0} ({1}). "
                "Start it first (see start_whisper_server.cmd / SETUP_AMD.md).".format(
                    self.url, e
                )
            )

    @staticmethod
    def _to_wav_bytes(buffer):
        """float32 [-1, 1] mono 16 kHz -> in-memory 16-bit PCM WAV."""
        pcm = np.clip(buffer, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype(np.int16)
        bio = io.BytesIO()
        wf = wave.open(bio, "wb")
        try:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLERATE)
            wf.writeframes(pcm.tobytes())
        finally:
            wf.close()
        return bio.getvalue()

    def _form(self, translate, opts):
        """The multipart fields for one /inference call."""
        data = {
            "temperature": "{0}".format(_float(opts.get("temperature"), 0.0)),
            "temperature_inc": "{0}".format(
                _float(opts.get("temperature_inc"), 0.2)),
            "response_format": "verbose_json",
            # Sent on every request, so it overrides whatever -l the server was
            # started with; start_whisper_server.cmd's "-l auto" only supplies a
            # default for requests that omit the field, and this one never does.
            "language": self.language or "auto",
            "translate": "true" if translate else "false",
        }
        # Everything below is omitted unless asked for, so options=None sends
        # the exact request this module has always sent.
        if "beam_size" in opts:
            data["beam_size"] = "{0:d}".format(int(opts["beam_size"]))
        if "best_of" in opts:
            data["best_of"] = "{0:d}".format(int(opts["best_of"]))
        if "audio_ctx" in opts:
            # Truncates the encoder window: 768 measured 1.76s against 2.09s
            # on this machine, the cheapest speed lever the server exposes.
            data["audio_ctx"] = "{0:d}".format(int(opts["audio_ctx"]))
        if "max_context" in opts:
            data["max_context"] = "{0:d}".format(int(opts["max_context"]))
        if "initial_prompt" in opts:
            # The server calls this field "prompt". It is initial_prompt
            # everywhere else because that is faster-whisper's spelling and
            # settings.py has to pick one of the two.
            data["prompt"] = str(opts["initial_prompt"])
        if "carry_initial_prompt" in opts:
            # Whisper sees the prompt once, at the start of the decode. On a
            # sliding window that means the vocabulary hint stops applying
            # after the first window; this re-prepends it to every one.
            data["carry_initial_prompt"] = (
                "true" if opts["carry_initial_prompt"] else "false")
        if "no_speech_thold" in opts:
            data["no_speech_thold"] = "{0}".format(
                _float(opts["no_speech_thold"], 0.6))
        if "entropy_thold" in opts:
            # Half of whisper.cpp's decoder-failed test, the one that forces a
            # retry at the next temperature instead of captioning a repetition
            # loop. The test it feeds is `entropy < thold`, so raising this is
            # strictly stricter and strictly slower: on 11.3 s of speech at
            # -3.4 dB SNR, 1.0 and 2.4 produced the same three captions in
            # 3.27 s, while 10.0 took 9.25 s and invented a fourth caption
            # reading "Record". Worth exposing because that cost lands on the
            # noisy windows, which is where a live pipeline spends its time.
            data["entropy_thold"] = "{0}".format(
                _float(opts["entropy_thold"], 2.4))
        if "logprob_thold" in opts:
            # The other half, and the one that costs time, because every
            # failed test is a whole extra decode of the window: nearer zero
            # is stricter and retries more, measured at 4.43 s against 3.27 s
            # at the default -1.0 on the same 11 s window. Worth exposing on a
            # live pipeline for that reason alone - it is the one decode knob
            # here that can push a window past the slide interval.
            data["logprob_thold"] = "{0}".format(
                _float(opts["logprob_thold"], -1.0))
        if "max_len" in opts:
            # Caption width, not a speed lever - 8.10 s at max_len=30 against
            # 8.18 s uncapped on a 49.3 s sample, i.e. free. What it buys is
            # the overlay strip: the server's own wrap gave 17 captions
            # averaging 46 characters, max_len=30 gave 31 averaging 25.
            data["max_len"] = "{0:d}".format(int(opts["max_len"]))
        if "split_on_word" in opts:
            # Sent even when max_len is absent, because "absent" is not "off":
            # whisper-server substitutes a 60-character wrap for a missing
            # max_len, so this still decides whether that cut lands between
            # words or mid-token. At max_len=20 on the 49.3 s sample the
            # difference is a caption reading "the settings module," against
            # one reading "the settings module" followed by a caption that is
            # just ",".
            data["split_on_word"] = (
                "true" if opts["split_on_word"] else "false")
        if "suppress_nst" in opts:
            data["suppress_nst"] = "true" if opts["suppress_nst"] else "false"
        if not opts.get("language_probabilities", True):
            # Measured 1.50s against a 2.09s baseline, so switching this off
            # buys real time rather than tidiness - worth it once the language
            # has settled and nobody is reading the runners-up.
            data["no_language_probabilities"] = "true"
        if "word_timestamps" in opts:
            # The server calls this token_timestamps, and only the "on"
            # direction is ever sent. Switching word timings off is honoured
            # on the CPU path, where the alignment pass costs real time; here
            # it saved 0.07 s of a 3.35 s window and nothing measurable on a
            # 10.3 s one, and took two things with it that are not word
            # timings:
            #   - every word came back start = end = 0.0, because
            #     whisper-server emits per-word times only under this flag;
            #   - whisper.cpp wraps a segment to max_len only while token
            #     timestamps are on, so the 49.3 s sample went from 17
            #     captions of 10-58 characters to 8 of 51-142 - and with
            #     max_len=30 set it went from 32 captions to those same 8,
            #     i.e. the caption cap silently stopped existing.
            # Sending it rather than omitting it also overrides a server
            # started with -nt, the same reason `language` is sent every
            # request.
            data["token_timestamps"] = "true"
        return data

    @staticmethod
    def _words(segment):
        """A server segment's words, in the shape both backends share."""
        out = []
        for w in segment.get("words") or []:
            # t_dtw is dropped. It is the DTW alignment slot and reads -1
            # unless the server was started with a --dtw preset, so keeping it
            # would put a key in the payload that means nothing here and has
            # no counterpart at all on the CPU path.
            out.append(_word(
                w.get("word", ""),
                _float(w.get("start"), 0.0),
                _float(w.get("end"), 0.0),
                _float(w.get("probability")),
            ))
        return out

    def transcribe(self, buffer, translate=False, options=None):
        """Returns a list of Segment, each still a (text, lang_code) tuple."""
        opts = decode_options(options)
        self.last_detection = _no_detection()
        if len(buffer) < MIN_SERVER_SAMPLES:
            # Refused here rather than sent, because a short enough buffer does
            # not return an error - it takes the server process down. Measured
            # against the prebuilt whisper-server.exe in _whisper.cpp: 16
            # samples killed it outright (exit 139, a segfault), while 100 and
            # up survived. That is whisper.cpp #3956 - log_mel_spectrogram
            # reflect-pads the start of the buffer by reading 200 samples from
            # samples[1] before the length check runs, so anything under 201
            # samples reads off the end of the allocation. Fixed upstream on
            # 2026-08-06; the binary here predates it (it has the 2026-05-18
            # speaker field and not this), and we cannot know what a user
            # dropped into _whisper.cpp anyway.
            #
            # Nothing in the app should reach this - buffering.MIN_FLUSH_SAMPLES
            # is 4000 - so it is a backstop, not a code path with a purpose. It
            # returns empty rather than raising: a fragment this short carries
            # no caption worth the round trip, and raising would count against
            # AutoBackend's failure budget and eventually force a pointless
            # fallback to CPU.
            return []
        wav_bytes = self._to_wav_bytes(buffer)
        data = self._form(translate, opts)
        files = {"file": ("buffer.wav", wav_bytes, "audio/wav")}
        try:
            resp = self._requests.post(
                self.url + "/inference",
                data=data,
                files=files,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except Exception as e:
            raise BackendError("whisper-server request failed: {0}".format(e))

        payload = resp.json()
        lang = self._read_language(payload)
        return self._read_segments(payload, lang)

    def _read_language(self, payload):
        """
        The caption label for this response, and .last_detection alongside it.

        Always a code. whisper-server answers with a full name ("english") and
        faster-whisper with a code ("en"), so without this the same audio
        captions as [english] on GPU and [en] after an auto fallback - the
        label changing mid-session for no reason the viewer can see. A pinned
        language is authoritative; otherwise translate whatever the server
        said.
        """
        probabilities = payload.get("language_probabilities")
        probability = _float(payload.get("detected_language_probability"))
        reported = language_code(
            payload.get("detected_language") or payload.get("language"))

        if _flat_language_report(probabilities, probability):
            # See _flat_language_report. English is not a guess here: a model
            # with no language tokens is an English-only model, and English is
            # the only thing it can produce.
            if not self._flat_reported:
                self._flat_reported = True
                print("[backend] This server reports every language as "
                      "equally likely, which means an English-only model "
                      "(ggml-*.en). Captions are labelled 'en' and language "
                      "detection is skipped - load a multilingual model if "
                      "you need it.")
            reported, probability, probabilities = "en", None, None

        self.last_detection = _detection(self.language, reported, probability,
                                         _candidates(probabilities))
        return self.language or reported or "??"

    def _read_segments(self, payload, lang):
        """The response's segments, as Segments carrying their word timings."""
        results = []
        segments = payload.get("segments")
        if segments:
            for seg in segments:
                text = (seg.get("text") or "").strip()
                if text and not is_marker(text):
                    results.append(Segment(
                        text, lang,
                        words=self._words(seg),
                        start=_float(seg.get("start")),
                        end=_float(seg.get("end")),
                        no_speech_prob=_float(seg.get("no_speech_prob")),
                        avg_logprob=_float(seg.get("avg_logprob")),
                    ))
        else:
            text = (payload.get("text") or "").strip()
            if text and not is_marker(text):
                results.append(Segment(text, lang, start=0.0,
                                       end=_float(payload.get("duration"))))
        return results


# whisper.cpp climbs from `temperature` in steps of `temperature_inc` until it
# is past 1.0, which is six rungs at the app's defaults. The cap is here
# because each rung is a whole extra decode of the window on the slowest
# backend in the app: a step of 0.01 would ask for a hundred of them.
_MAX_TEMPERATURE_STEPS = 16


def _temperature_ladder(opts):
    """
    (temperature, temperature_inc) as the list faster-whisper wants.

    Whisper decodes a window again at a higher temperature when the first try
    trips its own compression-ratio or logprob thresholds - the retry that
    breaks a repetition loop instead of captioning it. whisper.cpp spells
    that ladder as a start plus a step; faster-whisper spells it as the list
    of temperatures to try, and handing it the bare float turns the ladder
    OFF: options.temperatures becomes [0.0] and the window is decoded exactly
    once. That is not what temperature 0 means anywhere else here - settings.py
    documents it as "Whisper climbs from here on its own when a decode fails
    its own thresholds", and the server does exactly that - so passing the
    float made the CPU fallback the one path with no guard against a
    hallucinated repeat, on the audio most likely to produce one.

    The app's own 0.0 / 0.2 rebuilds faster-whisper's stock
    [0.0, 0.2, 0.4, 0.6, 0.8, 1.0], so this restores rather than changes what
    the CPU path did before options existed.
    """
    start = _float(opts.get("temperature"), 0.0)
    step = _float(opts.get("temperature_inc"), 0.2)
    if step is None or step <= 0.0:
        # settings.py's help for temperature_inc: "0 disables it".
        return start
    ladder = []
    while len(ladder) < _MAX_TEMPERATURE_STEPS:
        # Rebuilt from the start each time rather than accumulated: += 0.2 six
        # times lands on 0.6000000000000001, and ct2 takes the value literally.
        ladder.append(round(start + step * len(ladder), 4))
        if ladder[-1] >= 1.0:
            break
    return ladder


# -------------------------------
# faster-whisper CPU fallback
# -------------------------------
class LocalBackend(object):
    name = "faster-whisper (CPU int8)"
    active_name = "local"       # see AutoBackend.active_name

    def __init__(self, model_path, language=None):
        self.language = normalize_language(language)
        self.last_detection = _no_detection()
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise BackendError(
                "faster-whisper is not installed. "
                "Install it with: pip install faster-whisper"
            )
        try:
            # compute_type and cpu_threads are two separate arguments. Merged
            # into one string - compute_type="int8, cpu_threads=4" - every
            # model load raises: compute_type is a single ctranslate2 enum and
            # that is not one of its values, so the CPU backend never loaded
            # and AutoBackend had nothing left to fall back to.
            self.model = WhisperModel(model_path, device="cpu",
                                      compute_type="int8", cpu_threads=4)
        except Exception as e:
            raise BackendError(
                "Could not load faster-whisper model at '{0}': {1}".format(
                    model_path, e
                )
            )

    def set_language(self, language):
        """
        Re-pin the spoken language without reloading the model.

        Revalidated through normalize_language and assigned only once it
        returns, so a rejected code leaves the running pin intact.
        """
        self.language = normalize_language(language)

    @staticmethod
    def _words(segment):
        """A faster-whisper segment's words, in the shared shape."""
        out = []
        for w in getattr(segment, "words", None) or []:
            out.append(_word(
                getattr(w, "word", ""),
                _float(getattr(w, "start", None), 0.0),
                _float(getattr(w, "end", None), 0.0),
                _float(getattr(w, "probability", None)),
            ))
        return out

    def transcribe(self, buffer, translate=False, options=None):
        """Returns a list of Segment, each still a (text, lang_code) tuple."""
        opts = decode_options(options)
        self.last_detection = _no_detection()
        kwargs = {
            # None means detect; faster-whisper rejects the string "auto".
            "language": self.language,
            "task": "translate" if translate else "transcribe",
            # word_timestamps runs a second cross-attention alignment pass
            # over every segment, so it is asked for only when something
            # actually reads .words. It is not free here the way the server's
            # verbose_json words are.
            "word_timestamps": bool(opts.get("word_timestamps", False)),
            # 0 already meant "keep the default" and decode_options dropped
            # it, so an absent beam_size lands on 1 - greedy, which is what
            # this path has always used. The `or 1` catches a 0 arriving by
            # some other route: faster-whisper rejects beam_size=0 outright.
            "beam_size": int(opts.get("beam_size", 1)) or 1,
        }
        if "best_of" in opts:
            kwargs["best_of"] = int(opts["best_of"])
        if "temperature" in opts or "temperature_inc" in opts:
            # Both settings feed the one faster-whisper argument, so either
            # one arriving is enough to have to build the whole ladder.
            kwargs["temperature"] = _temperature_ladder(opts)
        if "initial_prompt" in opts:
            kwargs["initial_prompt"] = str(opts["initial_prompt"])
        if "no_speech_thold" in opts:
            kwargs["no_speech_threshold"] = _float(opts["no_speech_thold"],
                                                   0.6)
        if "logprob_thold" in opts:
            # Same quantity, same default (-1.0), different spelling: this is
            # the average-logprob test that decides a decode failed and has to
            # be retried up the temperature ladder. Mapped so the ladder built
            # above is triggered by the same threshold on both backends.
            kwargs["log_prob_threshold"] = _float(opts["logprob_thold"], -1.0)
        # entropy_thold is NOT mapped. faster-whisper has no entropy test at
        # all; its second failure test is compression_ratio_threshold, which
        # happens to share whisper.cpp's 2.4 default and measures something
        # else entirely - gzip ratio of the decoded text, not the entropy of
        # the token distribution. Feeding one number into the other would look
        # like the setting was honoured while quietly changing a different
        # test, which is worse than ignoring it.
        #
        # audio_ctx, suppress_nst, max_context, language_probabilities,
        # max_len, split_on_word and carry_initial_prompt have no
        # faster-whisper equivalent either and are ignored here rather than
        # rejected: they are server-only levers - settings.py says so in their
        # help text - and refusing a whole settings dict because it carries
        # one would make the CPU fallback unusable with the app's own
        # defaults. temperature_inc does have an equivalent and is honoured -
        # see _temperature_ladder.
        segments, info = self.model.transcribe(buffer, **kwargs)
        # Already a code here, but routed through the same helper so both
        # backends are guaranteed to report in one vocabulary.
        lang = (self.language
                or language_code(getattr(info, "language", None)) or "??")
        results = []
        for segment in segments:
            # Draining this generator is what actually runs inference, so
            # info is only worth reading after the loop has finished.
            text = segment.text.strip()
            if text:
                results.append(Segment(
                    text, lang,
                    words=self._words(segment),
                    start=_float(getattr(segment, "start", None)),
                    end=_float(getattr(segment, "end", None)),
                    no_speech_prob=_float(
                        getattr(segment, "no_speech_prob", None)),
                    avg_logprob=_float(getattr(segment, "avg_logprob", None)),
                ))
        # Same shape as the server path, through the same helper, so a mid-
        # session fallback from GPU to CPU does not change what the readout
        # means. Under a pin faster-whisper reports language_probability 1.0
        # for a detection it never ran; _detection() drops that rather than
        # showing a perfect score for a measurement that did not happen.
        self.last_detection = _detection(
            self.language,
            language_code(getattr(info, "language", None)) or lang,
            _float(getattr(info, "language_probability", None)),
            _candidates(getattr(info, "all_language_probs", None)))
        return results


# -------------------------------
# auto: server-preferred, CPU fallback, self-healing
# -------------------------------
class AutoBackend(object):
    """
    Prefers the GPU server and survives it dying mid-session.

    A ServerBackend on its own checks reachability exactly once, in __init__.
    If the server goes away later, every pass fails - roughly every --slide
    seconds, forever - and there is no path back to GPU without restarting the
    script. This wrapper keeps both backends behind one transcribe() and moves
    between them:

      GPU -> CPU   after FAILURES_BEFORE_FALLBACK consecutive failures (~10 s
                   at the default 2 s slide). Deliberately not on the first
                   one: a single timeout is a hiccup, and paying for it with
                   CPU inference for the rest of the session is a bad trade.
      CPU -> GPU   as soon as a cheap probe, tried every PROBE_INTERVAL_SEC,
                   gets an answer.

    Exactly one line is printed per transition, so a flapping server cannot
    fill the console.

    Only "--backend auto" builds this. Explicit "server" / "local" get the bare
    backend - those are choices to respect, not defaults to second-guess.
    """

    name = "auto (whisper.cpp server + CPU fallback)"

    FAILURES_BEFORE_FALLBACK = 5
    PROBE_INTERVAL_SEC = 60.0
    PROBE_TIMEOUT_SEC = 3.0

    def __init__(self, server_url, model_path, language=None):
        self.server_url = server_url
        self._model_path = model_path
        # Validated here, before anything expensive is built, so a bad code is
        # a startup error even when the server is down and this constructor
        # goes straight to loading the CPU model.
        self._language = normalize_language(language)
        self._local = None          # built on first CPU pass, see _cpu()
        self._local_error = None    # sticky: set if the CPU model won't load
        self._failures = 0
        self._last_probe = time.monotonic()
        self._served = None         # who answered last, see last_detection

        try:
            self._server = ServerBackend(server_url, language=self._language)
            self._on_server = True
            print("Backend: {0} @ {1}".format(self._server.name, server_url))
            # _local stays None here: a session that never loses its server
            # never pays the faster-whisper load time or the RAM.
        except BackendError as e:
            self._on_server = False
            print("[warn] {0}".format(e))
            print("[warn] Falling back to CPU faster-whisper (slower); will retry "
                  "the server every {0:.0f}s.".format(self.PROBE_INTERVAL_SEC))
            try:
                self._server = ServerBackend(server_url, check=False,
                                             language=self._language)
            except BackendError:
                self._server = None   # no 'requests' - no server path at all
            # The server is already known-down, so CPU is needed immediately.
            # Loading it now keeps "model missing" a startup failure, the way
            # it has always been, rather than a surprise several minutes in.
            self._cpu()

    @property
    def active_name(self):
        """
        Which backend is serving RIGHT NOW ("server" or "local").

        Anything that phrases advice around the hardware has to ask, rather
        than assume: with this class in play the answer changes mid-session,
        so a hardcoded "the GPU is slow" hint can end up printed while running
        on CPU with no server at all.
        """
        return "server" if self._on_server else "local"

    @property
    def last_detection(self):
        """
        The detection from whichever backend actually served the last buffer.

        Reading self._server unconditionally would be wrong in exactly the
        case this class exists for: after a fallback the server's copy is a
        frozen snapshot from before it died, and the readout would go on
        reporting a language nothing has decoded for minutes.
        """
        if self._served is None:
            return _no_detection()
        return self._served.last_detection

    def set_language(self, language):
        """
        Re-pin on both halves, including the CPU one that does not exist yet.

        Storing it as well as pushing it is what matters: _cpu() may build the
        local backend an hour from now, and a LocalBackend constructed with
        the startup language would silently undo the change at the worst
        possible moment - the one where the server just died.
        """
        self._language = normalize_language(language)
        if self._server is not None:
            self._server.set_language(language)
        if self._local is not None:
            self._local.set_language(language)

    def _cpu(self):
        """The faster-whisper backend, constructed on first actual use."""
        if self._local is not None:
            return self._local
        if self._local_error is not None:
            raise self._local_error
        try:
            self._local = LocalBackend(self._model_path, language=self._language)
        except BackendError as e:
            # Remember the failure: retrying a missing or broken model would
            # stall the worker for seconds, on every pass, forever.
            self._local_error = e
            raise
        print("Backend: {0}".format(self._local.name))
        return self._local

    def _fall_back(self):
        """GPU -> CPU. False if there is no usable CPU backend to move to."""
        try:
            self._cpu()
        except BackendError as e:
            # Reached at most once: _local_error is now set, and transcribe()
            # stops calling us as soon as it is.
            print("[backend] whisper-server has failed {0}x in a row and the CPU "
                  "fallback is unavailable ({1}) - staying on the server."
                  .format(self._failures, e))
            return False
        self._on_server = False
        self._last_probe = time.monotonic()   # first probe one interval from now
        # No exception text here on purpose: the caller has already printed the
        # first few failures verbatim, and a requests connection error is ~400
        # characters of HTTPConnectionPool noise. This line reports the decision.
        print("[backend] whisper-server failed {0}x in a row - switched to CPU "
              "faster-whisper (slower). Retrying the server every {1:.0f}s."
              .format(self._failures, self.PROBE_INTERVAL_SEC))
        return True

    def _probe(self):
        """'Are you back yet?' - only ever called while running on CPU."""
        if self._server is None:
            return
        now = time.monotonic()
        if now - self._last_probe < self.PROBE_INTERVAL_SEC:
            return
        self._last_probe = now
        try:
            # This runs on the caller's thread, so a miss costs
            # PROBE_TIMEOUT_SEC of stalled captions once per interval - the
            # reason the probe timeout is short and the interval is long.
            self._server.ping(timeout=self.PROBE_TIMEOUT_SEC)
        except BackendError:
            return
        self._on_server = True
        self._failures = 0
        print("[backend] whisper-server is back - switched to GPU.")

    def transcribe(self, buffer, translate=False, options=None):
        if not self._on_server:
            self._probe()
        if self._on_server:
            try:
                results = self._server.transcribe(buffer, translate=translate,
                                                  options=options)
                self._failures = 0
                self._served = self._server
                return results
            except Exception:
                self._failures += 1
                # Too early to judge, or nothing to fall back to (_local_error
                # is sticky, so this never re-enters _cpu() once it has failed
                # - re-raising one stored exception every 2s would grow its
                # traceback for the rest of the session).
                if (self._failures < self.FAILURES_BEFORE_FALLBACK
                        or self._local_error is not None):
                    raise          # caller logs it, throttled
                if not self._fall_back():
                    raise          # nothing better to switch to
                # Serve this buffer from CPU rather than dropping it.
        local = self._cpu()
        results = local.transcribe(buffer, translate=translate,
                                   options=options)
        self._served = local
        return results


# -------------------------------
# Factory
# -------------------------------
def create_backend(backend="auto", server_url="http://127.0.0.1:8080",
                   model_path=r"_models\faster-whisper-medium",
                   language="auto"):
    """
    backend: "auto" | "server" | "local"
      auto   -> AutoBackend: whisper-server, with CPU fallback and recovery
      server -> whisper-server only; fail loudly if unreachable
      local  -> faster-whisper CPU only

    language: a Whisper code ("en", "ms", ...) to pin, or "auto" to detect it
      on every request. Pinning is worth it when you know the language:
      detection reruns per buffer, and on short or noisy windows it can land
      on a different answer than the one before, taking the transcript with it.
      Every backend also accepts set_language() afterwards, so this is a
      starting point rather than a commitment.
    """
    language = normalize_language(language)
    if language:
        print("Language: {0} (pinned)".format(language))
    if backend == "server":
        b = ServerBackend(server_url, language=language)
        print("Backend: {0} @ {1}".format(b.name, server_url))
        return b
    if backend == "local":
        b = LocalBackend(model_path, language=language)
        print("Backend: {0}".format(b.name))
        return b
    if backend == "auto":
        return AutoBackend(server_url, model_path, language=language)
    raise BackendError("Unknown backend '{0}' (use auto|server|local)".format(backend))
