# Mined from faster-whisper and whisper.cpp

Both engines this project sits on kept moving after it was written. This is what
came across, what was measured on the way, and what was deliberately left.

---

## faster-whisper

Checked at `1.2.1` plus three unreleased commits (November 2025). Its newest
commit is nine months old, so the whole delta is two VAD changes — but both are
directly relevant, because this project's speech gate is a hand-rolled
approximation of the one they improved.

### Silero VAD v6.2 weights

`_models\silero_vad_v6.2.onnx`. The gate now prefers the newest
`silero_vad*.onnx` in `_models\` and falls back to the export packaged inside
faster-whisper, so a fresh clone (where `_models\` is gitignored) still works.

Frames over the 0.5 threshold, this machine:

| source | bundled | v6.2 |
|---|---|---|
| clean speech, per 4 s window | 93–125 | 94–124 |
| fan hum | 2 | **0** |
| speech under hum 6 dB louder than it | 61 | **141** |
| a real song, per 4 s window | 0–**11** | 0–**2** |
| room tone | 0 | 0 |

The song row is the one that matters. The bundled export scored one window at
11 frames — over the default 8-frame gate — so it would have transcribed the
backing track as if somebody had spoken. v6.2 never exceeds 2.

### The real VAD state machine

`speech_gate.py` used to threshold the probabilities, take runs of `True`, and
merge gaps under 100 ms. faster-whisper's `get_speech_timestamps` is a state
machine, and three of its parts change **where speech is reported to end** —
which is the number `silence_at_end_of_chunk` cuts on:

- **Hysteresis** (`--vad-neg-threshold`). Speech starts above `threshold` and
  only ends below `neg_threshold`, derived as `threshold - 0.15`. With one
  threshold for both, a single frame dipping to 0.49 mid-word reads as the end
  of a sentence.
- **Minimum silence** (`--vad-min-silence-ms`). A gap only ends speech once it
  has lasted this long — see the measured ladder below.
- **Minimum speech.** Runs shorter than `--vad-min-speech-ms` are discarded, so
  a click or a door stops registering.

`speech_pad_ms` was deliberately **not** ported: upstream pads segments because
it then cuts the audio at them, and we only use the timestamps to decide about a
buffer we send whole.

**The default moved from 160 ms to 400 ms, and the first version of this was
wrong.** Segments found in a 49 s sample whose sentences number six:

| `--vad-min-silence-ms` | 100 | 160 | 250 | 400 | 500 | 800 | 1200 |
|---|---|---|---|---|---|---|---|
| segments | 13 | 13 | 13 | **6** | 6 | 5 | 1 |

Nothing below 250 ms changes anything, because this speaker leaves about 0.3 s
between words and about 0.85 s between sentences — the useful setting is between
those, and 400 ms is the first value that finds the sentences. A faster talker
needs less. faster-whisper's own default is 2000 ms, right for splitting a
recording and far too slow for live captions.

### Cutting at the longest pause

`SilenceAtEndOfChunk` force-cuts at `chunk_max_length` so a continuous talker
still gets captions. That cut used to land wherever the clock said. Upstream's
"Adds new VAD parameters" collects every silence over `min_silence_at_max_speech`
and splits at the **longest** one instead of the last; `GateResult.best_cut()`
does the same here.

Mean absolute level *at the cut point*, 49 s sample:

| | level at cut |
|---|---|
| blunt cut at the limit | 0.0656 – 0.0756 (mid-word) |
| longest-pause cut | 0.0002 – 0.0018 (actual silence) |

Roughly 40× to 350× quieter. If no pause qualifies, it falls back to the blunt
cut, so the behaviour never gets worse than it was.

---

## whisper.cpp

Active — HEAD was four days old when this was written. Its server parses far
more per-request parameters than this project was sending. Each one below was
tested against the running binary before being exposed, because *parsed* is not
the same as *effective*.

| now exposed | measured effect |
|---|---|
| `--max-len` / `--no-split-on-word` | Caption width. A 49 s sample gave 17 captions of 44–64 characters uncapped, and 31 of 22–29 at `--max-len 30`. No measurable time cost. |
| `--entropy-thold` | The retry trigger that breaks a repetition loop. Strictly monotonic in strictness and cost. |
| `--logprob-thold` | The other half of the same test. `-0.1` took 6.0 s against 2.7 s at the default; `-5.0` gave up in 1.6 s. Maps to `log_prob_threshold` on the CPU path too. |
| `--carry-initial-prompt` | Re-sends the vocabulary hint with every window instead of only the first. |

**Deliberately not exposed:** `word_thold`. Identical per-word probabilities at
0.01, 0.5 and 0.9 on the same 36-word window — a slider that moves nothing.

**A correction to an earlier claim in this repo:** `no_speech_thold` *is* read.
An earlier probe concluded it was ignored, but that probe used clean speech,
where `no_speech_prob` is 0.0004 — below every possible setting, so nothing
could change. On speech buried in noise, 0.05 gives 0 segments and 0.50 gives 4.

**Server-side VAD was not adopted.** whisper.cpp gained VAD timestamp mapping and
exposed VAD segments in July. Both are irrelevant here: this project gates
*before* the HTTP request, so a rejected buffer costs no round trip and no
inference at all. Gating locally is strictly cheaper for a live pipeline.

**Diarization is available and unusable.** `diarize=true` returns a `speaker`
field per segment, but whisper.cpp derives it from stereo, and every capture path
here downmixes to mono before the gate. It would always report one speaker.

### Your `whisper-server.exe` crashes on very short audio

Reproduced: a 16-sample buffer segfaults the server process (exit 139). 100
samples and up survive. That is whisper.cpp #3956 — `log_mel_spectrogram`
reflect-pads the start of the buffer by reading 200 samples from `samples[1]`
*before* the length check runs, so anything under 201 samples reads off the end
of the allocation. Fixed upstream on 2026-08-06.

`ServerBackend` refuses to send anything under a quarter second, which matches
`buffering.MIN_FLUSH_SAMPLES`, so no code path can take the server down
regardless of which build is installed. That was the guard while the shipped
binary predated the fix.

**Closed at the source, 2026-08-31.** `_whisper.cpp\` is now built from
whisper.cpp `eacbd823` (v1.9.3-77), which is well past the 2026-08-06 fix. It
is a **CUDA** build for THIS machine and deliberately not a portable one:
`-DCMAKE_CUDA_ARCHITECTURES=120a-real` targets the RTX 5070's sm_120 alone and
`-DGGML_NATIVE=ON` bakes in this CPU's AVX-512, so it would fail with
`STATUS_ILLEGAL_INSTRUCTION` on an older machine — the failure
`start_whisper_server.cmd` already has a handler for. That is safe because
`_whisper.cpp/` is gitignored and cannot be committed; the build to *ship* is
`build-cuda-portable` (`GGML_NATIVE=OFF`, `GGML_BACKEND_DL=ON`,
`GGML_CPU_ALL_VARIANTS=ON`), and the two must never be confused. The previous
Vulkan build is kept at `_whisper.cpp.bck\` (also gitignored). Verified:
`CUDA : ARCHS = 1200`, `AVX512 = 1`, 26 s of audio in 528 ms.

One trap if this is rebuilt again: a stale CMake cache pins
`CMAKE_CXX_COMPILER`, and `vcvars64.bat` moved from MSVC 14.44 to 14.51.
14.51's `<yvals_core.h>` static_asserts the compiler is ≥ 19.50 and 14.44 is
19.44, so every C++ translation unit fails with STL1001. `CMAKE_CXX_COMPILER`
is sticky, so reconfiguring in place cannot move it — delete `build-cuda` and
configure fresh.

### Non-speech markers no longer become captions

Whisper narrates what it hears when it hears no words: `[BLANK_AUDIO]`,
`(indistinct)`, `[MUSIC PLAYING]`. A quarter second of digital silence came back
as the caption `[BLANK_AUDIO]`, which the overlay displayed and the transcript
file kept. Text that is *entirely* one bracketed group is now dropped.
Deliberately narrow — a caption that merely contains a bracket, or ends
`(laughs)` after real words, is left alone, because dropping half a sentence to
remove an annotation is the worse error.

---

## What a real song settled

Captured 45 s of whatever was playing through the speakers, via this project's
own WASAPI loopback path — a Russian song with sung vocals, which whisper
transcribed correctly at 92% language confidence.

Silero scores those vocals at **0 frames in 9 of 11 windows**. Even
`--vad-min-speech-ms 32`, the lowest setting there is, admits 2 of 11 with the
bundled model and 1 of 11 with v6.2.

So for this kind of production the "Music" preset cannot work, and the README's
existing note is the one that applies: a window with **no speech frames at all**
cannot be reached by lowering a frame count, because there is no frame to count.
`--no-vad` is the only way through. The ASR is not the limitation here — it
transcribed the lyrics fine — the gate is, and that is the trade a speech tool
makes on purpose.

Worth knowing: v6.2 rejects music *better*, which is right for captioning
meetings and wrong for captioning lyrics.

Real music also confirms the README's warning about speech under music. Mixed at
known ratios, windows passing the default gate: music 6 dB quieter 8/8, equal
8/8, music 6 dB **louder 2/8**, 10 dB louder 0/8.
