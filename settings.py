"""
settings.py - every option this project has, declared exactly once.

Why a schema instead of three copies
------------------------------------
Before this file the option set existed in three places that drifted apart:

  * argparse in live_transcription.py  - 16 flags, the real feature set
  * run_pipeline.cmd                   - 7 of them, chosen by hand
  * nothing at all for the rest        - --buffer and --slide were the worst
                                         case: benchmark.py computes them for
                                         you and the launcher had no way to
                                         pass them on

So the guided launcher could not express what the program could do, and the
only way to reach the other flags was to type the command line the launcher
exists to avoid. Adding an option meant editing argparse, then the .cmd, then
the README, and forgetting one of the three was the normal outcome.

Here a Field is declared once and everything else is derived from it:

  build_parser()   -> the argparse CLI (the flag spellings below are the ones
                      that already existed, so old commands keep working)
  schema_json()    -> the control list the web UI renders itself from, so a
                      new Field appears in the browser with no UI edit
  validate()       -> one set of rules for CLI, UI and preset files
  rebuilds_for()   -> which live component a change touches, which is what
                      lets the UI edit almost anything mid-session instead of
                      making you stop, retype and restart

Presets are just a JSON dict of these keys, so "what I ran last time" is a
file you can keep.
"""

import argparse
import json
import os

from whisper_backends import WHISPER_LANGUAGES

# What a change to a field forces the running app to rebuild. The pipeline
# reads this to decide how much to tear down: almost every option is "none" or
# a cheap rebuild, which is why the UI can offer them live.
#
#   none      applied on the next buffer, nothing to rebuild
#   strategy  the buffering strategy object (drops the in-flight window)
#   gate      the Silero session
#   backend   the ASR backend (may reload a model - seconds)
#   source    the capture thread (re-opens the device)
#   overlay   the Tk overlay window
#   save      the transcript file handle
#   restart   needs a full process restart (only the web listener)
#   engine    needs the whisper-server process restarted (only server_model).
#             Like "restart" there is nothing here to tear down - the value is
#             spent outside this process, by start_whisper_server.cmd - so the
#             pipeline discards it and the panels say so instead.
REBUILDS = ("none", "strategy", "gate", "backend", "source", "overlay", "save",
            "restart", "engine")

GROUPS = [
    ("audio", "Audio capture", "Where the sound comes from."),
    ("chunking", "Chunking", "How audio is cut into windows for Whisper."),
    ("gate", "Speech gate", "What gets skipped before inference is paid for."),
    ("transcription", "Transcription", "Which engine, which language."),
    ("decoding", "Decoding", "Whisper decode parameters, sent per request."),
    ("output", "Output", "Transcript file and word confidence."),
    ("overlay", "Overlay", "The on-screen caption window."),
    ("web", "Control panels", "The browser panel's listener, and the "
     "desktop panel."),
]


class Field(object):
    """One option. See the module docstring for what gets derived from it."""

    __slots__ = ("key", "kind", "default", "group", "label", "help", "choices",
                 "minimum", "maximum", "step", "cli", "cli_negate",
                 "cli_choices", "rebuild", "advanced", "show_if", "unit",
                 "placeholder")

    def __init__(self, key, kind, default, group, label, help="", choices=None,
                 minimum=None, maximum=None, step=None, cli=None,
                 cli_negate=False, cli_choices=True, rebuild="none",
                 advanced=False, show_if=None, unit="", placeholder=""):
        self.key = key
        self.kind = kind
        self.default = default
        self.group = group
        self.label = label
        self.help = help
        self.choices = choices
        self.minimum = minimum
        self.maximum = maximum
        self.step = step
        self.cli = cli
        self.cli_negate = cli_negate
        self.cli_choices = cli_choices
        self.rebuild = rebuild
        self.advanced = advanced
        self.show_if = show_if or {}
        self.unit = unit
        self.placeholder = placeholder

    def as_json(self):
        return {
            "key": self.key, "kind": self.kind, "default": self.default,
            "group": self.group, "label": self.label, "help": self.help,
            "choices": self.choices, "min": self.minimum, "max": self.maximum,
            "step": self.step, "rebuild": self.rebuild,
            "advanced": self.advanced, "showIf": self.show_if,
            "unit": self.unit, "placeholder": self.placeholder,
            "cli": self.cli,
        }


def _language_choices():
    """'auto' plus every language Whisper knows, sorted by name."""
    out = [{"value": "auto", "label": "Auto-detect (per buffer)"}]
    for code, name in sorted(WHISPER_LANGUAGES.items(), key=lambda kv: kv[1]):
        out.append({"value": code,
                    "label": "{0} ({1})".format(name.title(), code)})
    return out


SCHEMA = [
    # -- audio ------------------------------------------------------------
    Field("capture", "choice", "loopback", "audio", "Capture mode",
          "loopback: whatever an OUTPUT device is playing, via WASAPI - no "
          "Stereo Mix needed. input: a recording device (Stereo Mix, mic). "
          "browser: audio streamed from the page you have open. "
          "file: transcribe a WAV from disk.",
          choices=[{"value": "loopback",
                    "label": "System audio (WASAPI loopback)"},
                   {"value": "input",
                    "label": "Recording device (Stereo Mix / mic)"},
                   {"value": "browser",
                    "label": "This browser (mic or shared tab)"},
                   {"value": "file", "label": "WAV file"}],
          cli="--capture", rebuild="source"),
    Field("device", "choice", "", "audio", "Device",
          "Blank means the Windows default. The list is filled in live from "
          "the machine running the server; on the CLI pass an index or id "
          "from --list-devices.",
          choices="devices", cli="--device", cli_choices=False,
          rebuild="source", placeholder="(system default)"),
    Field("file_path", "path", "", "audio", "WAV file",
          "16-bit PCM WAV. Anything else: convert it first, e.g. "
          "ffmpeg -i in.mp3 -ar 16000 -ac 1 -c:a pcm_s16le out.wav",
          cli="--file", rebuild="source", show_if={"capture": ["file"]}),
    Field("file_realtime", "bool", True, "audio", "Play file at real speed",
          "On, a WAV is fed at 1x so it behaves like live capture. Off, it is "
          "fed as fast as the backend will take it - useful for bulk "
          "transcription, useless for watching captions appear.",
          cli="--file-fast", cli_negate=True, rebuild="source",
          show_if={"capture": ["file"]}),

    # -- chunking ---------------------------------------------------------
    Field("strategy", "choice", "sliding_window", "chunking",
          "Chunking strategy",
          "sliding_window: fixed windows that overlap, with a fuzzy duplicate "
          "filter over the overlap. Predictable latency, occasional clipped "
          "word. silence_at_end_of_chunk: wait until the speech gate says the "
          "talking stopped before transcribing, so nothing is cut mid-word - "
          "at the cost of latency spikes through dense speech.",
          choices=[{"value": "sliding_window",
                    "label": "Sliding window (overlap + dedup)"},
                   {"value": "silence_at_end_of_chunk",
                    "label": "Silence at end of chunk"}],
          cli="--strategy", rebuild="strategy"),
    Field("buffer", "int", 4, "chunking", "Buffer length", unit="s",
          help="How much audio each pass sees. Whisper pads every window to "
               "30 s internally, so a 16 s buffer costs about what a 4 s one "
               "costs - a small buffer is not the low-latency choice, it is "
               "the one most likely to fall behind. Run the benchmark and "
               "press Apply.",
          minimum=1, maximum=30, cli="--buffer", rebuild="strategy",
          show_if={"strategy": ["sliding_window"]}),
    Field("slide", "int", 2, "chunking", "Slide", unit="s",
          help="How far the window advances per pass. Inference has to finish "
               "inside this or the app starts skipping audio to stay live. "
               "buffer minus slide is the overlap the duplicate filter needs.",
          minimum=1, maximum=30, cli="--slide", rebuild="strategy",
          show_if={"strategy": ["sliding_window"]}),
    Field("chunk_length", "float", 5.0, "chunking", "Chunk length", unit="s",
          help="Minimum audio to collect before looking for a pause to cut on.",
          minimum=0.5, maximum=30.0, step=0.5, cli="--chunk-length",
          rebuild="strategy",
          show_if={"strategy": ["silence_at_end_of_chunk"]}),
    Field("chunk_offset", "float", 0.4, "chunking", "Trailing silence",
          unit="s",
          help="How much quiet has to sit at the end of the chunk before it "
               "counts as a finished sentence, measured from where the "
               "speech gate says speech ended - so it cannot fire before "
               "the gate's own silence rule (Silence ends speech after) "
               "has, and the later of the two decides. Too small and "
               "Whisper gets no silence to close the sentence on; too "
               "large and dense speech never triggers.",
          minimum=0.0, maximum=5.0, step=0.05, cli="--chunk-offset",
          rebuild="strategy",
          show_if={"strategy": ["silence_at_end_of_chunk"]}),
    Field("chunk_max_length", "float", 20.0, "chunking", "Force-cut after",
          unit="s",
          help="Give up waiting for a pause once the chunk gets this long and "
               "transcribe it anyway, so a continuous talker still gets "
               "captions instead of silence.",
          minimum=2.0, maximum=30.0, step=1.0, cli="--chunk-max-length",
          rebuild="strategy",
          show_if={"strategy": ["silence_at_end_of_chunk"]}),
    Field("dedup_threshold", "float", 0.80, "chunking",
          "Duplicate similarity",
          "How alike two consecutive windows must read before the second is "
          "dropped as an overlap repeat. Raise it if deliberate repeats keep "
          "vanishing; lower it if the same sentence shows up twice. At 0.80, "
          "'yes' against 'yeah' already scores 0.86.",
          minimum=0.5, maximum=1.0, step=0.01, cli="--dedup-threshold",
          rebuild="none", advanced=True,
          show_if={"strategy": ["sliding_window"]}),

    # -- speech gate ------------------------------------------------------
    Field("vad", "bool", True, "gate", "Silero speech detection",
          "Off, every audible buffer reaches Whisper - including fans, music "
          "and room tone, which it answers with invented text.",
          cli="--no-vad", cli_negate=True, rebuild="gate"),
    Field("vad_threshold", "float", 0.5, "gate", "Speech probability",
          "Above this a 32 ms frame counts as speech. This is NOT the knob "
          "for singing: lowering it admits room tone at about the same point "
          "it admits music. Lower the frame requirement instead.",
          minimum=0.05, maximum=0.95, step=0.05, cli="--vad-threshold",
          rebuild="gate", show_if={"vad": [True]}),
    Field("vad_min_speech_ms", "float", 250, "gate", "Speech needed",
          unit="ms",
          help="How much speech a buffer needs before it is worth "
               "transcribing. Measured over 4 s windows: speech scores 35-97 "
               "frames, sung vocals only 0-15, fans and room tone exactly 0 - "
               "so 60 ms catches most singing and still cannot let steady "
               "noise through. This is an absolute frame count, so a longer "
               "buffer makes it proportionally looser.",
          minimum=32, maximum=2000, step=1, cli="--vad-min-speech-ms",
          rebuild="gate", show_if={"vad": [True]}),
    Field("vad_neg_threshold", "float", 0.0, "gate", "End-of-speech threshold",
          "Hysteresis, mined from faster-whisper's own VAD: speech STARTS "
          "above the probability above, and only ENDS once it drops below "
          "this. 0 derives it as threshold minus 0.15. One threshold for both "
          "makes a single dipping frame read as the end of a sentence, which "
          "is what the chunking strategy cuts on.",
          minimum=0.0, maximum=0.95, step=0.05, cli="--vad-neg-threshold",
          rebuild="gate", advanced=True, show_if={"vad": [True]}),
    Field("vad_min_silence_ms", "float", 400, "gate", "Silence ends speech "
          "after", unit="ms",
          help="How long a gap must last before the gate reports the talker "
               "as stopped rather than drawing breath. silence_at_end_of_chunk "
               "cannot cut before the gate says so, and then waits for its "
               "trailing silence on top, so the later of the two decides - at "
               "the defaults (400 ms here, 0.4 s there) they coincide to the "
               "frame. Too low splits sentences at every breath, which is "
               "where force-cuts then land; too high holds captions back. "
               "Measured on a 49 s sample whose sentences number 6, segments "
               "found: 100 ms 13, 160 ms 13, 250 ms 13, 400 ms 6, 500 ms 6, "
               "800 ms 5, 1200 ms 1. Nothing below 250 ms changes anything, "
               "because this speaker leaves about 0.3 s between words and "
               "about 0.85 s between sentences - the useful setting sits "
               "between those two, and 400 ms is the first value that finds "
               "the sentences. A faster talker needs less. faster-whisper's "
               "own default is 2000 ms, which is right for splitting a "
               "recording and far too slow for live captions.",
          minimum=32, maximum=3000, step=10, cli="--vad-min-silence-ms",
          rebuild="gate", advanced=True, show_if={"vad": [True]}),
    Field("vad_model", "path", "", "gate", "Silero model",
          "Blank uses the newest silero_vad*.onnx in _models, falling back to "
          "the one bundled with faster-whisper. The v6.2 weights shipped in "
          "_models measure strictly better on the hard cases: fan hum scored "
          "2 false-positive frames on the bundled export and 0 on v6.2, while "
          "speech under hum 6 dB louder than it went from 61 frames to 141. "
          "Do NOT swap in the silero-vad repo's own export - that one is "
          "worse once music is involved.",
          cli="--vad-model", rebuild="gate", advanced=True,
          show_if={"vad": [True]},
          placeholder="(newest in _models, else bundled)"),
    Field("silence_threshold", "float", 0.01, "gate", "Silence level",
          "Mean absolute level below which a buffer is skipped without even "
          "running the VAD. The free half of the gate.",
          minimum=0.0, maximum=0.2, step=0.001, cli="--silence-threshold",
          rebuild="none"),

    # -- transcription ----------------------------------------------------
    Field("backend", "choice", "auto", "transcription", "Backend",
          "auto runs on the GPU server, drops to CPU if it dies mid-session, "
          "and returns to GPU on its own once it answers again. server and "
          "local mean what they say and are left alone.",
          choices=[{"value": "auto",
                    "label": "Auto (GPU server, CPU fallback)"},
                   {"value": "server",
                    "label": "whisper.cpp server (GPU)"},
                   {"value": "local",
                    "label": "faster-whisper (CPU or CUDA)"}],
          cli="--backend", rebuild="backend"),
    Field("server_url", "str", "http://127.0.0.1:8080", "transcription",
          "Server URL", "Where whisper-server is listening.",
          cli="--server-url", rebuild="backend"),
    Field("server_model", "choice", "ggml-base-q5_1.bin", "transcription",
          "GPU model", "The GGML file whisper-server loads, server backend "
          "only - the counterpart of the faster-whisper model below. Filled "
          "in live from "
          "_models\\. whisper-server reads it once, at startup, so this is the "
          "one engine setting a running server cannot be told about: changing "
          "it takes effect on the next Start or Restart on the Engine tab. "
          "Remembered for next launch once a server has started on it.",
          choices="ggml_models", cli="--server-model", cli_choices=False,
          rebuild="engine"),
    Field("model", "path", r"_models\faster-whisper-medium", "transcription",
          "faster-whisper model", "Model folder for the faster-whisper "
          "backend - the counterpart of the GGML file above. CPU by default; "
          "see the device below, which is what decides whether this runs on "
          "the processor or the GPU.",
          cli="--model", rebuild="backend"),
    Field("local_device", "choice", "cpu", "transcription",
          "faster-whisper device",
          "Where the faster-whisper backend runs. It is CPU by default and "
          "that is deliberate rather than a limitation: under backend=auto "
          "this is the FALLBACK, and a fallback that needs the GPU is no "
          "fallback at all - the server usually died because something was "
          "wrong with the GPU. cuda needs an NVIDIA card and the CUDA 12 "
          "runtime CTranslate2 is built against; auto tries cuda and quietly "
          "settles for cpu if it cannot, which costs a few seconds on the one "
          "pass where the server just died.",
          choices=[{"value": "cpu", "label": "CPU"},
                   {"value": "cuda", "label": "CUDA (NVIDIA GPU)"},
                   {"value": "auto", "label": "Auto (CUDA, else CPU)"}],
          cli="--local-device", rebuild="backend",
          show_if={"backend": ["local", "auto"]}),
    Field("local_compute", "choice", "auto", "transcription",
          "faster-whisper precision",
          "CTranslate2 compute type. auto picks int8 on the CPU and float16 "
          "on CUDA, which is the right answer nearly always. Drop to "
          "int8_float16 on CUDA when a large model will not fit in VRAM.",
          choices=[{"value": "auto", "label": "Auto (int8 on CPU, float16 on GPU)"},
                   {"value": "int8", "label": "int8"},
                   {"value": "int8_float16", "label": "int8_float16"},
                   {"value": "float16", "label": "float16"},
                   {"value": "float32", "label": "float32"}],
          cli="--local-compute", rebuild="backend", advanced=True,
          show_if={"backend": ["local", "auto"]}),
    Field("language", "choice", "auto", "transcription", "Spoken language",
          "Pin it when you know it. Detection is not a one-off - it reruns on "
          "every buffer, so a short, quiet or music-backed window can decode "
          "as a different language than the one before it, and the transcript "
          "follows. Pinning also skips the detection pass.",
          choices="languages", cli="--language", cli_choices=False,
          rebuild="none"),
    Field("translate", "bool", False, "transcription", "Translate to English",
          "Sent per request, so this switches mid-session with no restart. "
          "Needs a model that can translate: a *-turbo model accepts the task "
          "and transcribes anyway, and English-only (ggml-*.en) models cannot "
          "translate at all. When that happens the caption keeps its own "
          "language tag and the log says why, rather than claiming to be "
          "English.",
          cli="--translate", rebuild="none"),

    # -- decoding ---------------------------------------------------------
    Field("temperature", "float", 0.0, "decoding", "Temperature",
          "0 is greedy and repeatable. Whisper climbs from here on its own "
          "when a decode fails its own thresholds.",
          minimum=0.0, maximum=1.0, step=0.05, cli="--temperature",
          rebuild="none", advanced=True),
    Field("temperature_inc", "float", 0.2, "decoding", "Temperature step",
          "How far that fallback climbs per retry. 0 disables it.",
          minimum=0.0, maximum=1.0, step=0.05, cli="--temperature-inc",
          rebuild="none", advanced=True),
    Field("beam_size", "int", 0, "decoding", "Beam size",
          "0 keeps each backend's default (greedy on the CPU path). Higher is "
          "more accurate and measurably slower - 5 cost about a third more on "
          "the reference machine, which is the difference between holding "
          "real time and not.",
          minimum=0, maximum=10, cli="--beam-size", rebuild="none",
          advanced=True),
    Field("best_of", "int", 0, "decoding", "Best of",
          "Candidates kept when sampling. 0 keeps the backend default.",
          minimum=0, maximum=10, cli="--best-of", rebuild="none",
          advanced=True),
    Field("audio_ctx", "int", 0, "decoding", "Audio context",
          "Truncates the encoder's context. 0 is the full 1500, which covers "
          "the padded 30 s window - roughly 50 per second of audio. Lowering "
          "it is fast and NOT free: measured on one machine, an 8 s buffer "
          "went 3.84 s at full to 2.03 s at 768, but the transcript changed "
          "at every reduction tried, so this trades accuracy for speed rather "
          "than winning it. It can also backfire badly - a 16 s buffer at 512 "
          "took 18.94 s against 4.58 s at full, because a starved encoder "
          "produces text that fails the entropy and log-probability checks "
          "and gets retried at higher temperatures. Raise the buffer before "
          "reaching for this. Server backend only.",
          minimum=0, maximum=1500, step=64, cli="--audio-ctx", rebuild="none",
          advanced=True),
    Field("initial_prompt", "str", "", "decoding", "Vocabulary hint",
          "Text fed in as context ahead of the audio. Give it the names, "
          "jargon and spellings the model keeps getting wrong - products, "
          "people in the meeting - and it biases toward them.",
          cli="--initial-prompt", rebuild="none",
          placeholder="e.g. Vulkan, WASAPI, faster-whisper"),
    Field("no_speech_thold", "float", 0.6, "decoding", "No-speech threshold",
          "Whisper's own opinion of whether a segment was speech at all. "
          "Segments above this are dropped. A second, independent guard "
          "against hallucinated captions, after the Silero gate. It only "
          "bites on audio the decoder is unsure about: clear speech scores "
          "0.0004, far under any setting, so the effect shows up on the noisy "
          "windows and nowhere else.",
          minimum=0.0, maximum=1.0, step=0.05, cli="--no-speech-thold",
          rebuild="none", advanced=True),
    Field("entropy_thold", "float", 2.4, "decoding", "Entropy threshold",
          "Above this the decode is judged to have failed and is retried at a "
          "higher temperature - the mechanism that breaks a repetition loop "
          "instead of captioning it. Measured on deliberately noisy audio: "
          "2.4 produced real text, while 1.0 and 10.0 both collapsed to "
          "'(indistinct)'. Server backend only.",
          minimum=0.0, maximum=10.0, step=0.1, cli="--entropy-thold",
          rebuild="none", advanced=True),
    Field("logprob_thold", "float", -1.0, "decoding", "Log-probability "
          "threshold",
          "The other half of that retry test. Nearer zero is stricter and "
          "retries more: -0.1 took 6.0 s against 2.7 s at the default, and "
          "-5.0 gave up in 1.6 s. Applies to both backends - it is the one "
          "fallback threshold faster-whisper spells the same way.",
          minimum=-10.0, maximum=0.0, step=0.1, cli="--logprob-thold",
          rebuild="none", advanced=True),
    Field("max_len", "int", 0, "decoding", "Caption length cap",
          "Longest a single caption may get, in characters. 0 lets Whisper "
          "segment on its own, which on one 49 s sample gave 17 captions of "
          "44-64 characters - a paragraph for the overlay to wrap. 30 gave 32 "
          "captions of 22-29. Costs nothing measurable. Server backend only.",
          minimum=0, maximum=200, step=5, cli="--max-len", rebuild="none",
          advanced=True),
    Field("split_on_word", "bool", True, "decoding", "Split on word "
          "boundaries",
          "When the cap above splits a caption, break between words rather "
          "than mid-token. Only does anything when the cap above is set. "
          "Server backend only.",
          cli="--no-split-on-word", cli_negate=True, rebuild="none",
          advanced=True),
    Field("carry_initial_prompt", "bool", False, "decoding",
          "Repeat the hint every window",
          "The vocabulary hint above is normally seen once, at the start. "
          "This re-sends it with every window, so a long session keeps "
          "spelling names the way you asked. Server backend only.",
          cli="--carry-initial-prompt", rebuild="none", advanced=True),
    Field("suppress_nst", "bool", False, "decoding",
          "Suppress non-speech tokens",
          "Blocks the tokens Whisper uses for music cues, applause and "
          "[BLANK_AUDIO]-style output. Server backend only.",
          cli="--suppress-nst", rebuild="none", advanced=True),
    Field("max_context", "int", -1, "decoding", "Carried-over context",
          "How many tokens of the previous window's text are fed back in as "
          "context. -1 is the default. 0 makes every window independent, "
          "which costs some fluency and stops one bad decode poisoning the "
          "next. Server backend only.",
          minimum=-1, maximum=224, cli="--max-context", rebuild="none",
          advanced=True),
    Field("language_probabilities", "bool", True, "decoding",
          "Report language probabilities",
          "Fills the detected-language readout with the runners-up, which is "
          "what makes the 'pin this' suggestion possible. Costs a slice of "
          "each request, so turn it off once the language is settled. Server "
          "backend only.",
          cli="--no-language-probabilities", cli_negate=True, rebuild="none",
          advanced=True),

    # -- output -----------------------------------------------------------
    Field("save", "bool", False, "output", "Save transcript",
          "Can be switched on and off mid-session.",
          cli="--save", rebuild="save"),
    Field("output", "path", "", "output", "Transcript file",
          "Blank auto-names it transcript_YYYYmmdd_HHMMSS with whatever "
          "extension the format below implies.",
          cli="--output", rebuild="save", show_if={"save": [True]},
          placeholder="(auto-named)"),
    Field("transcript_format", "choice", "txt", "output",
          "Transcript format",
          "srt and vtt are built from the word timings, so choosing one turns "
          "word timestamps on.",
          choices=[{"value": "txt", "label": "Plain text"},
                   {"value": "jsonl",
                    "label": "JSON Lines (one object per caption)"},
                   {"value": "srt", "label": "SubRip subtitles (.srt)"},
                   {"value": "vtt", "label": "WebVTT subtitles (.vtt)"}],
          cli="--transcript-format", rebuild="save", show_if={"save": [True]}),
    Field("word_timestamps", "bool", True, "output",
          "Word timings + confidence",
          "Asks for per-word probability, which is what colours shaky words "
          "in the transcript and what the subtitle formats are built from. "
          "It costs real time on the CPU backend, and nothing on the GPU "
          "server - measured there at 7.05 s against 7.08 s, with the words "
          "returned either way - so turning it off is only worth it while "
          "running on CPU.",
          cli="--no-word-timestamps", cli_negate=True, rebuild="none"),
    Field("confidence_warn", "float", 0.60, "output", "Low confidence below",
          "Words under this are flagged red in the UI, and amber up to the "
          "midpoint between this and 1.",
          minimum=0.0, maximum=1.0, step=0.05, cli="--confidence-warn",
          rebuild="none", advanced=True),

    # -- overlay ----------------------------------------------------------
    Field("overlay", "bool", True, "overlay", "Show overlay",
          "The always-on-top caption strip.",
          cli="--no-overlay", cli_negate=True, rebuild="overlay"),
    Field("overlay_font_family", "str", "Segoe UI", "overlay", "Font",
          cli="--overlay-font", rebuild="overlay",
          show_if={"overlay": [True]}),
    Field("overlay_font_size", "int", 24, "overlay", "Font size", unit="pt",
          minimum=8, maximum=96, cli="--overlay-font-size", rebuild="overlay",
          show_if={"overlay": [True]}),
    Field("overlay_bold", "bool", True, "overlay", "Bold",
          cli="--overlay-no-bold", cli_negate=True, rebuild="overlay",
          show_if={"overlay": [True]}),
    Field("overlay_fg", "color", "#ffffff", "overlay", "Text colour",
          cli="--overlay-fg", rebuild="overlay", show_if={"overlay": [True]}),
    Field("overlay_bg", "color", "#000000", "overlay", "Background",
          help="Also the colour keyed out for transparency, so picking a "
               "background that appears inside the text colour punches holes "
               "in the letters.",
          cli="--overlay-bg", rebuild="overlay", show_if={"overlay": [True]}),
    Field("overlay_opacity", "float", 0.8, "overlay", "Opacity",
          minimum=0.1, maximum=1.0, step=0.05, cli="--overlay-opacity",
          rebuild="overlay", show_if={"overlay": [True]}),
    Field("overlay_lines", "int", 2, "overlay", "Lines kept",
          help="How many recent captions stay on screen.",
          minimum=1, maximum=8, cli="--overlay-lines", rebuild="overlay",
          show_if={"overlay": [True]}),
    Field("overlay_width_pct", "int", 100, "overlay", "Width",
          unit="% of screen",
          minimum=10, maximum=100, cli="--overlay-width", rebuild="overlay",
          show_if={"overlay": [True]}),
    Field("overlay_y_pct", "float", 0.85, "overlay", "Vertical position",
          help="0 is the top of the screen, 1 the bottom. Dragging the "
               "overlay updates this.",
          minimum=0.0, maximum=1.0, step=0.01, cli="--overlay-y",
          rebuild="overlay", show_if={"overlay": [True]}),
    Field("overlay_clear_after", "float", 0.0, "overlay", "Clear after",
          unit="s",
          help="Blank the overlay when nothing new has been said for this "
               "long. 0 leaves the last caption up.",
          minimum=0.0, maximum=60.0, step=1.0, cli="--overlay-clear-after",
          rebuild="overlay", show_if={"overlay": [True]}),
    Field("overlay_locked", "bool", False, "overlay", "Lock position",
          help="Ignore drags, so a stray click cannot move it.",
          cli="--overlay-locked", rebuild="overlay",
          show_if={"overlay": [True]}),

    # -- web --------------------------------------------------------------
    Field("web", "bool", False, "web", "Serve the web UI",
          "Off on the CLI by default, so nothing starts listening unless you "
          "asked for it.",
          cli="--web", rebuild="restart"),
    Field("web_host", "str", "127.0.0.1", "web", "Listen address",
          "127.0.0.1 is this machine only. 0.0.0.0 exposes the control panel "
          "and the live transcript to your whole network - set a token first.",
          cli="--web-host", rebuild="restart"),
    Field("web_port", "int", 8770, "web", "Port",
          minimum=1, maximum=65535, cli="--web-port", rebuild="restart"),
    Field("web_token", "str", "", "web", "Access token",
          "Blank means no check, which is fine while bound to 127.0.0.1. "
          "Once set it is required as ?token=... on the page URL.",
          cli="--web-token", rebuild="restart", placeholder="(none)"),
    Field("web_open", "bool", True, "web", "Open a browser on start",
          cli="--no-web-open", cli_negate=True, rebuild="restart"),
    Field("wpf", "bool", False, "web", "Desktop panel (WPF)",
          "A native window that calls the engine directly - no listener, no "
          "port, no token. Needs the .NET 8 Desktop Runtime and pythonnet; "
          "missing either just means no panel, transcription is unaffected. "
          "Independent of the web panel - both can be open at once.",
          cli="--wpf", rebuild="restart"),
    Field("wpf_theme", "choice", "dark", "web", "Desktop panel theme",
          "Applied live: the window re-themes in place, no restart. A "
          "setting rather than a button in the window so that it persists "
          "with the rest and shows in both panels - read-only in the "
          "browser, which keeps its own switch in its top bar and cannot "
          "move this one.",
          choices=[{"value": "dark", "label": "Dark (charcoal)"},
                   {"value": "light", "label": "Light"}],
          cli="--wpf-theme", show_if={"wpf": [True]}),
]

BY_KEY = dict((f.key, f) for f in SCHEMA)
DEFAULTS = dict((f.key, f.default) for f in SCHEMA)

# Options a browser is not allowed to change, whatever it sends. A page served
# to the network must not be able to move the listener out from under itself;
# those are decisions for whoever started the process. "wpf" is here for the
# same reason: whether a window opens on the host machine belongs to whoever is
# sitting at it - and so does what that window looks like, which is
# "wpf_theme".
#
# The three path keys are here because each one is the ONLY route from an
# untrusted patch to a sink of its kind, and Pipeline.apply pops these keys
# BEFORE it calls validate(), so the value never reaches any of them:
#
#   model      is not a path when it fails to be one. faster-whisper falls
#              through to download_model() for any string that is not an
#              existing directory (transcribe.py: `elif os.path.isdir(...)`,
#              else `download_model(...)`), and local_files_only defaults
#              False, so "someone/backdoor" is an outbound fetch followed by a
#              CTranslate2 weight load - not the failed load it looks like.
#   vad_model  is the only thing that aims the tree's only InferenceSession
#              (speech_gate.py), i.e. attacker-named bytes into a native
#              graph parser.
#   output     is the only WRITE. transcript.py does makedirs(exist_ok=True)
#              then open(path, "a"/"w"), and because `save` is a plain bool
#              and apply() drains synchronously when nothing is running, both
#              fire inside apply() with no start command. app.py already
#              sanitises a preset NAME for exactly this reason; a transcript
#              PATH had no such guard.
#
# file_path is deliberately NOT here, and that is not an oversight. Locking it
# would remove a working browser feature (capture="file") while removing no
# capability at all: the benchmark COMMAND takes a wav path straight off the
# socket into wave.open (app.py _run_benchmark -> pipeline.benchmark ->
# benchmark.load_wav) and never passes through apply(), so REMOTE_LOCKED
# cannot reach it. Read paths are fixed as a class instead, at the sink and by
# shape rather than by name, which reaches both doors - see safe_paths.py.
REMOTE_LOCKED = ("web", "web_host", "web_port", "web_token", "web_open",
                 "wpf", "wpf_theme",
                 "model", "vad_model", "output")


class SettingsError(ValueError):
    """A field was given a value it cannot take."""


def _coerce(field, value):
    """Value for `field`, in `field`'s own type. Raises SettingsError."""
    kind = field.kind
    try:
        if kind == "bool":
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ("1", "true", "yes", "on"):
                    return True
                if low in ("0", "false", "no", "off"):
                    return False
                raise ValueError(value)
            return bool(value)
        if kind == "int":
            value = int(round(float(value)))
        elif kind == "float":
            value = float(value)
        else:
            value = "" if value is None else str(value)
    except (TypeError, ValueError):
        raise SettingsError("{0}: '{1}' is not a valid {2}".format(
            field.label, value, kind))

    if kind in ("int", "float"):
        if field.minimum is not None and value < field.minimum:
            raise SettingsError("{0}: {1} is below the minimum {2}".format(
                field.label, value, field.minimum))
        if field.maximum is not None and value > field.maximum:
            raise SettingsError("{0}: {1} is above the maximum {2}".format(
                field.label, value, field.maximum))
    if kind == "color":
        text = value.strip()
        if not (len(text) == 7 and text[0] == "#"
                and all(c in "0123456789abcdefABCDEF" for c in text[1:])):
            raise SettingsError("{0}: '{1}' is not a #rrggbb colour".format(
                field.label, value))
        return text.lower()
    if kind == "choice" and isinstance(field.choices, list):
        allowed = [c["value"] for c in field.choices]
        if value not in allowed:
            raise SettingsError("{0}: '{1}' is not one of {2}".format(
                field.label, value, ", ".join(allowed)))
    return value


def validate(patch, base=None):
    """
    (clean, errors) for a partial settings dict.

    Unknown keys and failed fields land in `errors` and are left out of
    `clean` rather than taking the whole patch down with them: a browser
    sending one bad number should not lose the other nineteen good ones.
    `base` is the currently-applied settings, used for the cross-field checks.
    """
    clean, errors = {}, []
    for key, value in patch.items():
        field = BY_KEY.get(key)
        if field is None:
            errors.append("unknown setting '{0}'".format(key))
            continue
        try:
            clean[key] = _coerce(field, value)
        except SettingsError as e:
            errors.append(str(e))

    merged = dict(DEFAULTS)
    if base:
        merged.update(base)
    merged.update(clean)

    # Cross-field rules. Each one drops only the field that has to give way, so
    # the rest of the patch still applies.
    if merged["strategy"] == "sliding_window" \
            and merged["slide"] > merged["buffer"]:
        errors.append(
            "Slide ({0}s) cannot exceed buffer ({1}s): consecutive windows "
            "would leave gaps of unheard audio between them.".format(
                merged["slide"], merged["buffer"]))
        clean.pop("slide", None)
        clean.pop("buffer", None)
    if merged["chunk_offset"] >= merged["chunk_length"]:
        errors.append(
            "Trailing silence ({0}s) must be shorter than the chunk ({1}s), "
            "or no chunk can ever qualify.".format(
                merged["chunk_offset"], merged["chunk_length"]))
        clean.pop("chunk_offset", None)
    if merged["chunk_max_length"] < merged["chunk_length"]:
        errors.append(
            "Force-cut ({0}s) cannot be shorter than the chunk it is cutting "
            "({1}s).".format(merged["chunk_max_length"],
                             merged["chunk_length"]))
        clean.pop("chunk_max_length", None)
    if merged["capture"] == "file" and not merged["file_path"]:
        errors.append("Capture is set to 'file' but no WAV file was given.")
        clean.pop("capture", None)
    elif merged["capture"] == "file" \
            and not os.path.exists(merged["file_path"]):
        errors.append("WAV file not found: {0}".format(merged["file_path"]))
        clean.pop("file_path", None)
        clean.pop("capture", None)
    if merged["transcript_format"] in ("srt", "vtt") \
            and not merged["word_timestamps"]:
        # Fixed rather than rejected: asking for subtitles is a clear enough
        # statement of intent to turn on the thing that makes them possible.
        clean["word_timestamps"] = True
    if merged["language"] != "auto" \
            and merged["language"] not in WHISPER_LANGUAGES:
        errors.append(
            "Unknown language '{0}'. Use a Whisper code (en, es, ms, ja, ...) "
            "or 'auto'.".format(merged["language"]))
        clean.pop("language", None)
    return clean, errors


def rebuilds_for(patch, base=None):
    """Which components a patch actually forces to be rebuilt."""
    base = base or {}
    needed = set()
    for key, value in patch.items():
        field = BY_KEY.get(key)
        if field is None or field.rebuild == "none":
            continue
        if key in base and base[key] == value:
            continue        # unchanged; do not tear anything down for it
        needed.add(field.rebuild)
    return needed


# -------------------------------
# CLI
# -------------------------------
def build_parser(description, extra=None):
    """
    An argparse parser carrying every Field that declares a `cli` flag.

    The spellings here are the ones that existed before this file, so commands
    and shortcuts written against the old script keep working unchanged.
    """
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    for field in SCHEMA:
        if not field.cli:
            continue
        # argparse runs help text through %-formatting, so a literal per-cent
        # sign in any Field's help ("about 15% faster", "% of screen") makes
        # --help itself raise TypeError. Escaped here rather than in the schema,
        # because the web UI renders the same strings as plain text and would
        # show the doubled signs.
        summary = (field.help or field.label).replace("%", "%%")
        if field.unit:
            summary = "{0} [{1}]".format(summary, field.unit.replace("%", "%%"))
        kwargs = {"dest": field.key, "help": summary}
        if field.kind == "bool":
            if field.cli_negate:
                kwargs["action"] = "store_false"
                kwargs["default"] = True
                kwargs["help"] = "Turn off: " + summary
            else:
                kwargs["action"] = "store_true"
                kwargs["default"] = False
        else:
            kwargs["default"] = field.default
            kwargs["type"] = {"int": int, "float": float}.get(field.kind, str)
            if field.kind == "choice" and field.cli_choices \
                    and isinstance(field.choices, list):
                kwargs["choices"] = [c["value"] for c in field.choices]
        parser.add_argument(field.cli, **kwargs)

    parser.add_argument(
        "--preset", dest="preset", default=None,
        help="Load a JSON preset first; flags you type still win")
    parser.add_argument(
        "--save-preset", dest="save_preset", default=None,
        help="Write the resolved settings to a JSON preset and carry on")
    parser.add_argument(
        "--list-devices", dest="list_devices", action="store_true",
        help="Print the capture devices this machine has, then exit")
    parser.add_argument(
        "--start-server", dest="start_server", action="store_true",
        help="Start whisper-server from here instead of expecting one to be "
             "running, with no console of its own - its log goes to the "
             "panel and to logs/. run_pipeline.cmd passes this for the two "
             "panel front ends, where it is what removes the second window")
    # Deliberately NOT a Field. Every Field generates a control in both
    # panels (invariant 13), and "start the server I am already running
    # inside of" is a thing you can only say before the process exists -
    # a tickbox for it would do nothing whichever way it was set.
    for pos_args, pos_kwargs in (extra or []):
        parser.add_argument(*pos_args, **pos_kwargs)
    return parser


def _explicit_args(parser, argv):
    """
    Only the options that actually appeared on the command line.

    Needed because argparse cannot tell "--buffer 4 was typed" from "4 is the
    default", and without that distinction a preset could never be overridden
    by a flag - or worse, would itself be overwritten by defaults.
    """
    sink = argparse.ArgumentParser(add_help=False)
    for action in parser._actions:
        if not action.option_strings:
            continue
        kwargs = {"dest": action.dest, "default": argparse.SUPPRESS,
                  "help": argparse.SUPPRESS}
        if isinstance(action, argparse._StoreTrueAction):
            kwargs["action"] = "store_true"
        elif isinstance(action, argparse._StoreFalseAction):
            kwargs["action"] = "store_false"
        elif isinstance(action, argparse._HelpAction):
            continue
        else:
            kwargs["type"] = action.type
            kwargs["choices"] = action.choices
        try:
            sink.add_argument(*action.option_strings, **kwargs)
        except argparse.ArgumentError:
            pass
    known, _ = sink.parse_known_args(argv)
    return vars(known)


def from_args(parser, argv=None):
    """
    (settings, args) for a command line.

    Precedence is defaults < remembered < preset < flags actually typed.

    Remembered state sits directly above the defaults and below everything
    anyone actually asked for: naming a preset or typing a flag is a statement
    about THIS run, and neither should be quietly overruled by what the panel
    happened to be doing last time.
    """
    args = parser.parse_args(argv)
    settings = dict(DEFAULTS)
    settings.update(load_state())

    if args.preset:
        settings.update(load_preset(args.preset))

    explicit = _explicit_args(parser, argv)
    for field in SCHEMA:
        if field.cli and field.key in explicit:
            settings[field.key] = explicit[field.key]

    clean, errors = validate(settings)
    if errors:
        raise SystemExit("Settings error:\n  " + "\n  ".join(errors))
    settings.update(clean)

    if args.save_preset:
        save_preset(args.save_preset, settings)
        print("Preset written to {0}".format(args.save_preset))
    return settings, args


# -------------------------------
# Presets
# -------------------------------
def load_preset(path):
    """A preset file as a settings dict. Unknown keys are reported, not fatal."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise SystemExit("Could not read preset '{0}': {1}".format(path, e))
    if not isinstance(data, dict):
        raise SystemExit("Preset '{0}' must be a JSON object".format(path))
    data.pop("_comment", None)
    clean, errors = validate(data)
    for err in errors:
        print("[preset] ignored: {0}".format(err))
    return clean


def save_preset(path, settings):
    """
    Write only what differs from the defaults, so presets stay readable.

    REMOTE_LOCKED keys are left out, and that is a bug fix rather than a
    security measure - they are refused on the way back in either way. A
    preset is a TRANSCRIPTION PROFILE; "is the desktop panel open" is not part
    of one. Saving from the panel captured `wpf: true` simply because the
    panel was running, and loading that preset then reported
    "One setting in 'x' was refused" every single time - a scary toast, on
    your own saved profile, about a key you never chose to put in it.
    """
    trimmed = dict((k, v) for k, v in settings.items()
                   if k in DEFAULTS and v != DEFAULTS[k]
                   and k not in REMOTE_LOCKED)
    trimmed["_comment"] = ("live-audio-transcription preset. Only non-default "
                           "values are stored; anything missing falls back to "
                           "the default.")
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(trimmed, fh, indent=2, sort_keys=True)
        fh.write("\n")


# -------------------------------
# Remembered state
# -------------------------------
# Deliberately NOT a settings file. Everything else in this schema is a
# default, a flag or a named preset, and that is the whole design: nothing
# about a run is hidden state you cannot see in a file you chose to write.
#
# server_model is the one key that breaks the rule, because it is the only
# setting whose value is spent OUTSIDE this process. whisper-server reads the
# model once, at startup, and the panel starts that server - so picking a model
# is not "change a number the running engine will pick up", it is "launch a
# different program". Forgetting it meant every restart silently reverted to
# the .cmd's own default, which is the smallest model in _models and, on a CUDA
# build, measurably the slowest of the useful ones.
#
# Kept to an explicit allow-list rather than "remember whatever changed", so
# this never quietly grows into the settings file the design says no to.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "_state.json")

REMEMBERED = ("server_model",)


def load_state(path=None):
    """
    The remembered keys, or {} - never fatal.

    A missing, empty, unreadable or nonsense file is the normal first-run case
    and means "nothing remembered", so unlike load_preset (which was named on a
    command line by someone who meant it) this stays silent and returns {}.
    """
    try:
        with open(path or STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    wanted = dict((k, v) for k, v in data.items() if k in REMEMBERED)
    clean, _ = validate(wanted)
    return clean


def save_state(values, path=None):
    """
    Remember the allow-listed keys. Returns True if the file was written.

    Best-effort by design: a read-only checkout or a locked file is not a
    reason to fail a server start that has otherwise worked.
    """
    # str(k) rather than k: testing `k in REMEMBERED` narrows the key type to
    # those exact names, and a dict typed that narrowly rejects the "_comment"
    # line below as a type error. Widening it here keeps the annotation-free
    # style the rest of this module is written in.
    keep = dict((str(k), v) for k, v in values.items() if k in REMEMBERED)
    if not keep:
        return False
    keep["_comment"] = ("Written by the panel: the last engine choice, so the "
                        "next launch starts on it. Safe to delete - the "
                        "defaults come back.")
    try:
        with open(path or STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(keep, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError:
        return False
    return True


# -------------------------------
# For the browser
# -------------------------------
def schema_json():
    """Everything the web UI needs in order to draw itself."""
    return {
        "groups": [{"key": k, "label": lbl, "help": h} for k, lbl, h in GROUPS],
        "fields": [f.as_json() for f in SCHEMA],
        "defaults": DEFAULTS,
        "languages": _language_choices(),
        "remoteLocked": list(REMOTE_LOCKED),
    }
