# Live System Audio Transcription with Overlay

This project transcribes **system audio in real-time** and displays it in a **modern, draggable overlay**, with optional translation to English and transcript saving.

This also adds **GPU support via whisper.cpp** — Vulkan for AMD/Intel, CUDA for NVIDIA — behind a pluggable backend system:

- **GPU backend** — [whisper.cpp](https://github.com/ggml-org/whisper.cpp) `whisper-server` (Vulkan or CUDA build), reached over local HTTP. Vulkan works on AMD, Intel, and NVIDIA; a CUDA build is the stronger choice on NVIDIA.
- **Fallback backend** — [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper), used automatically when the server is not running — including when it stops running *mid-session*. CPU `int8` by default, and that default is load-bearing: this is the path taken *because* the GPU server just died. On NVIDIA it can also run on the card (`--local-device cuda`), which is a sensible choice for `--backend local` and a poor one for the fallback.

> **Why?** Faster-Whisper is built on CTranslate2, which supports NVIDIA CUDA or CPU only. On AMD cards the old `device="cuda"` call silently fell back to CPU. The Vulkan path gives AMD cards real GPU acceleration. NVIDIA users can instead drop a CUDA build into `_whisper.cpp\` — the app does not care which build the server was compiled with.

![Sample Image](sample.png)

## Architecture

```
  loopback / Stereo Mix / browser / WAV
                 │
                 ▼
          audio_sources.py ──► speech_gate.py ──► buffering.py ──► whisper_backends.py
           (capture, 16 kHz)     (Silero VAD)     (window policy)         │
                 │                                                       ├─► whisper-server.exe
                 └───────────────── pipeline.py ◄────────────────────────┘   (Vulkan/CUDA GPU)
                                        │                                └─► faster-whisper (CPU
                                        │                                    fallback, automatic)
                    ┌───────────────────┼───────────────────┬───────────────────┐
                    ▼                   ▼                   ▼                   ▼
                 console            overlay.py         wsserver.py         wpf_panel.py
                                    (Tk captions)      (HTTP + WS)         (pythonnet)
                                                            │                   │
                                                            ▼                   ▼
                                                    browser control panel  ui/ (WPF desktop panel)
```

All four outputs subscribe to the same event stream, and two of them can talk
back: the browser over HTTP and a WebSocket, which is why it needs a port and a
token, and the desktop panel directly through `IEngineBridge` — no socket, no
port, no token. `settings.py` declares every option once — the CLI flags, both
panels' forms and the validation are all generated from it.

## Features

- Live transcription of system audio via **WASAPI loopback of any output device** (headset, speakers - no Stereo Mix needed), classic Stereo Mix / mic input (`--capture input`), a **WAV file**, or audio **streamed from a browser** (a phone in the room, a laptop elsewhere, a shared tab).
- **GPU acceleration via whisper.cpp** — Vulkan (AMD/Intel) or CUDA (NVIDIA) — (`--backend server`).
- Automatic backend selection with CPU fallback (`--backend auto`, default) — if the GPU server dies mid-session it drops to CPU rather than going silent, and returns to GPU on its own once the server is back.
- **Browser control panel** (`--web`) — every one of the 65
 options, changeable *while it runs*, with live meters, word-confidence colouring and a benchmark you can apply with one click. See [Web UI](#web-ui).
- **Per-word confidence** — both backends report the probability of every word, so a caption that reads fluently but was a guess does not look like one the model was sure of. It also makes `.srt` / `.vtt` export possible from any session.
- Optional translation to English (`--translate`) — passed per-request, no server restart needed. A caption is only labelled as translated when it actually came back translated: a model that ignores the task (`large-v3-turbo` does) gets one warning in the log and keeps its own language tag.
- Optional **spoken-language pinning** (`--language ms`) — skips per-buffer auto-detection, which can otherwise disagree with itself on short or noisy windows.
- **Silero voice-activity detection** — buffers with no speech in them are skipped before inference, so fans, music and room tone stop producing hallucinated captions (and stop costing GPU time). No extra dependency; disable with `--no-vad`.
- **Two chunking strategies** (`--strategy`) — a sliding window with a duplicate filter, or wait-for-silence, which never cuts a word in half.
- Live overlay at the bottom center of the screen; transparent, draggable, and fully restyleable.
- Optional transcript saving (`--save`) as **text, JSON Lines, SubRip or WebVTT** (`--transcript-format`).
- **Presets** — save a whole configuration as JSON and load it with `--preset`, or from the panel.
- **Lite version**: console-only output with no arguments (`live_transcription_lite.py`).

## Requirements

- Python 3.10+
- For GPU: a Vulkan-capable GPU (AMD/Intel; tested target: Radeon Pro W5500), or an NVIDIA GPU with a CUDA build of whisper.cpp
- For CPU fallback: the Faster-Whisper model folder

## Installation

1. **Clone or download the repository**
```bash
git clone https://github.com/Pazran/live-audio-transcription.git
cd live-audio-transcription
```

2. **Create and activate a virtual environment**
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```
> The venv is still required — the scripts need `numpy`, `sounddevice`, `soundcard`, and `requests` regardless of backend. `faster-whisper` is only imported by the CPU fallback; you may skip it for a server-only setup.

4. **Set up the GPU backend (recommended, AMD-friendly)** — see [Building whisper.cpp](#building-whispercpp) below, and [SETUP_AMD.md](SETUP_AMD.md) for the AMD walkthrough:
   - Place a whisper.cpp build — Vulkan (AMD/Intel) or CUDA (NVIDIA) — with `whisper-server.exe` and its DLLs under `_whisper.cpp\`. You will most likely have to build this yourself; it takes one command, but which command matters.
   - Download [`ggml-base-q5_1.bin`](https://huggingface.co/ggerganov/whisper.cpp/tree/main) into `_models\` — this is the filename `start_whisper_server.cmd` looks for by default. Any other GGML model works too; pass its filename as an argument: `start_whisper_server.cmd ggml-medium-q5_0.bin`

5. **Set up the CPU fallback (optional)**
   - Place the Faster-Whisper medium model under `_models/faster-whisper-medium`

6. **Start the GPU server, then run the script**
```bash
start_whisper_server.cmd
python live_transcription.py
```
The server startup log should list your GPU as a Vulkan device (e.g. `ggml_vulkan: 0 = ...`) or, on a CUDA build, `ggml_cuda_init: found N CUDA devices`. Leave that window running.

> **Prefer a guided setup?** Double-click `Start Transcription.vbs` (or run `run_pipeline.cmd`). It asks which script, capture mode and flags you want, then creates the venv, installs requirements, and starts the server for you. Option **[4] Web UI** skips the questions entirely and opens the control panel instead.
>
> Choosing **[4] Web UI** or **[5] Desktop UI** leaves you with **one** window, not three: the panel starts whisper-server itself with no console of its own, and the launcher's console hides once the panel is up. Everything either would have printed goes to `logs/session_*.log` (the last 10 runs are kept), and the server's startup — including the line naming your GPU — also appears in the panel's **Log** tab. The console modes are unchanged, since there the console *is* the transcript.

## Building whisper.cpp

Most people end up building this themselves, and the reason is worth knowing
before you start: **a whisper.cpp binary is compiled for a GPU architecture
*and* a CPU instruction set, and neither of those travels.** There are no
official Windows Vulkan binaries at all, and a build made on someone else's
machine is the usual cause of the `STATUS_ILLEGAL_INSTRUCTION` crash that
`start_whisper_server.cmd` already knows how to explain.

The good news is that **you should not hardcode anything.** ggml detects your
CPU and your GPU on its own; almost every "pin the architecture" instruction
you will find online makes the result *worse*. The commands below are short on
purpose.

### Which backend

|  | CUDA | Vulkan |
|---|---|---|
| Runs on | NVIDIA, **Turing (sm_75) or newer** | AMD, Intel, **and** NVIDIA |
| You install | CUDA Toolkit (~3 GB) | LunarG Vulkan SDK (~600 MB) |
| Speed on NVIDIA | fastest | well behind on quantised models |
| Prebuilt Windows binaries exist | ✔ (official `whisper-cublas` release zips) | ✘ — community builds only |

On NVIDIA, build CUDA. Vulkan is the only option on AMD and Intel, and it is
also the answer for **NVIDIA cards older than Turing** — CUDA 13 dropped every
architecture below `sm_75`, so GTX 900/10-series owners either install a
CUDA 12.x toolkit or build Vulkan. Check with:

```bash
nvcc --list-gpu-arch
```

CUDA 13.3 answers `compute_75` … `compute_121`. If your card is not in that
list, that toolkit cannot build for it at all.

### Prerequisites

- **Visual Studio 2022 or 2026** with "Desktop development with C++"
- **CMake 3.24+** and **Ninja** (both ship with the VS C++ workload). whisper.cpp
  itself builds with far older CMake; 3.24 is what the GPU auto-detection below
  needs, and under it ggml silently falls back to a generic architecture list
- **CUDA Toolkit 12.8+** for a CUDA build — 12.8 is the floor for Blackwell
  (RTX 50-series), or **LunarG Vulkan SDK** for a Vulkan build
- A **short build path.** This is not a style preference. One real object file
  is `ggml\src\ggml-cuda\CMakeFiles\ggml-cuda.dir\template-instances\fattn-tile-instance-dkq112-dv112.cu.obj.d`
  — 104 characters *inside* the build directory, before your own path is added.
  Build under something like `C:\src\whisper.cpp\build`, not a deeply nested
  folder, or nvcc fails partway through with `Could not open output file`, an
  error that says nothing about the real cause: Windows' 260-character limit.

### Build it

```bash
git clone https://github.com/ggml-org/whisper.cpp
cd whisper.cpp
```

Then, from a **x64 Native Tools Command Prompt** (so `cl.exe` is on PATH):

```bash
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON
cmake --build build --config Release
```

For Vulkan, swap the one flag:

```bash
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_VULKAN=ON
cmake --build build --config Release
```

**Do not pass `-DCMAKE_CUDA_ARCHITECTURES`.** Left alone, ggml picks `native` —
it queries the GPU in the machine and builds for exactly that. Setting it by
hand replaces a correct answer with a guess, and it is how you get a binary
that runs on your machine and nothing else.

Binaries land in `build\bin\`.

### Install it

Copy into `_whisper.cpp\`:

- `whisper-server.exe`
- **every `.dll` next to it** — `whisper.dll`, `ggml.dll`, `ggml-base.dll`,
  `ggml-cpu.dll`, and the backend one (`ggml-cuda.dll` or `ggml-vulkan.dll`)
- **CUDA builds only:** `cudart64_13.dll`, `cublas64_13.dll` and
  `cublasLt64_13.dll`, which are **not** produced by the build. Take them from
  `%CUDA_PATH%\bin\x64\`. Note the `x64` — CUDA 13 moved its redistributables
  down a level, so every instruction written for CUDA 12 points at the wrong
  folder and whisper-server fails to start with no message at all.

Then check it:

```bash
start_whisper_server.cmd
```

The startup log names the device it found — `ggml_cuda_init: found N CUDA
devices` or `ggml_vulkan: 0 = ...`. If it names your GPU, you are done.

### Building one that runs on other machines

The defaults above deliberately produce a binary for **this** computer. To
build one you can hand to someone else, turn the native optimisation off and
let ggml compile every variant instead:

```bash
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON ^
      -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON
```

- `GGML_NATIVE=OFF` stops the compiler targeting the host CPU's exact
  instruction set. It defaults to **ON**, which is why the naive build is not
  portable and why `STATUS_ILLEGAL_INSTRUCTION` is so common.
- `GGML_CPU_ALL_VARIANTS=ON` builds 14 x86 CPU backends — `x64`, `sse42`,
  `sandybridge`, `haswell`, `skylakex`, `zen4`, `alderlake` and the rest — and
  picks one at run time. It **requires** `GGML_BACKEND_DL=ON`; ggml stops the
  configure with a `FATAL_ERROR` otherwise, since the dispatch needs the
  backends to be loadable modules.
- Leaving `CMAKE_CUDA_ARCHITECTURES` unset matters even more here. With
  `GGML_NATIVE=OFF`, ggml switches from `native` to a curated list —
  `75-virtual 80-virtual 86-real 89-real 90-virtual 120a-real 121a-real` on
  CUDA 13 — mixing `-real` (compiled device code for common GPUs) with
  `-virtual` (PTX, which the driver JITs on first run, so future cards still
  work). Pinning a single `-real` architecture throws all of that away.

### What actually goes wrong

| Symptom | Cause |
|---|---|
| `STATUS_ILLEGAL_INSTRUCTION (0xC000001D)` right after "using ... backend" | Binary built with `GGML_NATIVE=ON` on a newer CPU. Not a GPU problem — it happens with `--no-gpu` too. Rebuild here, or build portable as above. |
| whisper-server exits instantly, no message | Missing `cudart64_13.dll` / `cublas*` — they live in CUDA's `bin\x64`, not `bin`. |
| `nvcc fatal: Could not open output file ...cu.obj.d` | Build path too long. Move the checkout nearer the drive root. |
| CUDA build starts on your machine, crashes on another NVIDIA card | `CMAKE_CUDA_ARCHITECTURES` was pinned (or `native` was used) — no PTX was emitted, so there is nothing for the driver to JIT. |
| `nvcc --list-gpu-arch` does not list your card | CUDA 13 dropped Maxwell, Pascal and Volta. Use CUDA 12.x, or build Vulkan. |

## Web UI

```bash
python live_transcription.py --web
```

Or double-click `Start Transcription.vbs` and choose **[4] Web UI**, which is
the same thing with the environment set up for you first. A browser opens on
`http://127.0.0.1:8770`.

The panel is not a subset of the command line — it is the *same* option set. Both
are generated from one schema in [`settings.py`](settings.py), so every flag is a
control in the browser, with its help text, its bounds, and a marker saying what
changing it will rebuild. Adding an option to `settings.py` makes it appear in
both with no further edit.

What that buys over the console:

- **Change your mind without restarting.** Language, translation, buffer, slide,
  VAD thresholds, decode parameters, the overlay's font and colours, even the
  capture device — the pipeline rebuilds only the component a change touches.
  The old guided launcher fixed all of it at startup, and could reach seven flags.
- **Benchmark, then apply.** `benchmark.py` has always printed
  `RECOMMENDED: --buffer 16 --slide 7` — and the launcher had no way to pass
  either one on. The panel runs the same benchmark on the backend that is
  actually serving and applies the result to the running session with a button.
- **See why nothing is happening.** A live level meter and a speech-frame meter
  with the threshold marked, so "no captions" separates into *the capture device
  is wrong*, *it is below the silence threshold*, or *Silero does not hear speech
  in it* — each with the setting that fixes it.
- **Word confidence.** Words the model was unsure about are coloured and
  underlined. Hover for the number.
- **Devices in a dropdown**, not a console prompt you have to answer before the
  program will start.
- **Start, stop and restart, from the page.** The **Engine** tab runs both
  halves of this: `whisper-server` in its own console window, and the session
  itself — capture, speech gate and backend. A settings change can leave a
  component down (a device that was unplugged, a model file that is not there,
  a server that was not up yet) and nothing retried on its own; a banner above
  the meters now says which one, why, and has the button that fixes it.
  Stopping the session no longer ends the program, so there is something left
  to press Start on.
- **A model switcher.** When inference falls behind real time, the panel lists
  the GGML models in `_models\` and offers to restart `whisper-server` with a
  smaller one — including a server this panel did not start. It finds it by the
  port `server_url` points at, and checks the image name before sending
  anything, so it will not kill something else that happens to hold that port.
- **Export any session** as `.txt`, `.jsonl`, `.srt` or `.vtt`, whether or not
  you remembered to turn saving on — the word timings are already there.
- **Presets**, saved and loaded by name.
- **Browser audio.** Set capture to *This browser* and the page streams your
  microphone or a shared tab to the pipeline, so the machine doing the
  transcribing does not have to be the machine hearing the sound.

### Reaching it from another device

```bash
python live_transcription.py --web --web-host 0.0.0.0 --web-token some-secret
```

Then open `http://<this-machine>:8770/?token=some-secret`. The listener binds to
`127.0.0.1` with no token by default; set one before exposing it, because the
panel shows the transcript and changes settings. Options that would let a page
move the listener, open a window on the host, point either model path elsewhere
or choose where the transcript is written are refused from the browser
regardless — see `REMOTE_LOCKED` in `settings.py`. That list is a backstop, not
a substitute for the token: it covers the settings a patch can carry, and the
panel can still start, stop and reconfigure a running session.

A WAV path is handled differently, because `capture="file"` is a feature worth
keeping rather than a setting worth locking. Instead the *shape* is checked
wherever an untrusted caller can name a file — `safe_paths.py`, which both the
file capture and the benchmark go through. From the browser a path must be a
plain local `.wav`: UNC (`\\host\share\...`) is refused because on Windows
merely resolving one is an outbound authentication rather than a file read, and
so are device names (`CON.wav` is the console, not a file) and `x.wav:stream`.
None of that applies to the desktop panel or the command line, where the person
naming the file is the person running the process.

Once the listener is bound anywhere but loopback, one more rule turns on: a
browser may only read WAVs from the install folder. Widen it with

```bash
set LAT_AUDIO_ROOTS=D:\audio;E:\recordings
```

before starting. On `127.0.0.1` this rule stays off, because the browser at the
other end is you.

## Desktop panel (WPF)

```bash
python live_transcription.py --wpf
```

Or double-click `Start Transcription.vbs` and choose **[5] Desktop UI**. The
same control panel as a native window: the transcript with per-word
confidence, the generated settings pane, the meters and pills, the engine and
benchmark tabs, presets, export — rendered by WPF instead of a browser.

The difference is *how it talks to the engine*. The browser panel is a remote
control: it needs a listener, a port and a token, and its buttons are command
names on a wire. The desktop panel lives in the engine's process and calls it
directly — a click reaches `App._lifecycle` as a compiled method call, with no
socket anywhere. That also means the `web_*` settings the browser is refused
(a listener must not be reconfigurable through itself) stay editable here,
where that rule was never about you — and so do the panel's own two, `wpf`
and `wpf_theme`.

The window is dark by default; `--wpf-theme light` (or the *Desktop panel
theme* row under *Control panels*) switches it, live — the open window
re-themes in place. It is a setting rather than a button so that it persists
with the rest and rides along in presets; the browser panel keeps its own
switch in its top bar.

What it needs, and what happens without it:

- **The [.NET 8 Desktop Runtime](https://dotnet.microsoft.com/download/dotnet)**
  (the *Desktop* runtime — the base or ASP.NET ones have no WPF in it) and the
  `pythonnet` package from `requirements.txt`. Missing either prints one
  actionable line and transcription runs without the panel — `--wpf` can never
  cost you the session.
- **Nothing to build.** The compiled panel ships in `ui/runtime`. The .NET SDK
  is only needed if you change the C# in `ui/` — then run `.\build_ui.cmd`
  once and commit the refreshed output.

Closing the window is like closing the browser tab: the panel goes away and
the engine keeps whatever it was doing. The power button in the panel's top
bar is the one that stops transcription and ends the program.

`--web` and `--wpf` are independent and can be open at once — settings edited
in either show up in both, and the engine's single lifecycle slot keeps their
buttons from racing. The browser keeps one exclusive trick (streaming audio
from a *remote* device's microphone); the desktop panel's Engine tab has the
local equivalent, pushing this machine's microphone or system audio through
the same path.

## Usage

### Run with overlay (auto backend: GPU server if running, else CPU)
```bash
python live_transcription.py
```

### Force GPU server backend (fail loudly if server is down)
```bash
python live_transcription.py --backend server
```

### Force CPU faster-whisper backend
```bash
python live_transcription.py --backend local
```

### Translate to English
```bash
python live_transcription.py --translate
```

### Pin the spoken language (skip per-buffer detection)
```bash
python live_transcription.py --language ms
```

### Save transcript to default file
```bash
python live_transcription.py --save
```

### Save transcript to a specific file
```bash
python live_transcription.py --save --output my_transcript.txt
```

### Disable overlay (console-only output)
```bash
python live_transcription.py --no-overlay
```

### Translate + save + disable overlay
```bash
python live_transcription.py --translate --save --no-overlay
```

### Lite console-only version
```bash
python live_transcription_lite.py
```
- No arguments, prints live transcription to console only. Auto backend, translates to EN.
- **Requires a recording device** (Stereo Mix / microphone) — the Lite script has no WASAPI loopback path, so unlike the main script it cannot capture an output device.

### Measure this machine and pick `--buffer` / `--slide`
```bash
python benchmark.py --wav path\to\speech.wav
```
Times real inference through the same backend the app uses and prints the settings that hold real time:

```
   buffer    median       min       max      xRT    chars    slide
  ----------------------------------------------------------------
       4s     3.19s     3.14s     3.31s    0.80x       30        -
       8s     3.94s     3.67s     4.45s    0.49x       68        -
      16s     4.14s     4.06s     4.20s    0.26x      140       7s
      24s     5.30s     4.41s     6.50s    0.22x      216       8s

 RECOMMENDED:  --buffer 16 --slide 7
```

The defaults (`--buffer 4 --slide 2`) suit a GPU that transcribes 4 s in under 2 s. If yours does not, the app silently skips audio to stay live — this tells you what it can actually sustain instead of guessing. Pass a WAV of real speech: with no `--wav` it uses synthetic tones, which decode to almost no text and so measure only the encoder floor. Use `--backend local` to measure the CPU path.

Treat the result as a starting point, not a verdict. The benchmark runs on an otherwise-idle machine, while live capture shares the CPU with whatever is playing the audio — on one setup a 4.7 s benchmark ran 7–12 s in real use. If the app prints a `[perf]` line while you are actually using it, raise `--slide` until it stops.

### All flags

| Flag | Default | Purpose |
|---|---|---|
| `--capture` | `loopback` | `loopback` (capture any output device via WASAPI) / `input` (Stereo Mix, mic) |
| `--translate` | off | Translate speech to English as it transcribes (needs a model that can — **not** `large-v3-turbo`) |
| `--language` | `auto` | Spoken language as a Whisper code (`en`, `ms`, `ja`, `zh`, ...). `auto` detects it on every buffer; pin it when you know it — see [Notes](#notes) |
| `--save` | off | Write the transcript to a text file |
| `--output` | auto-named | Transcript path; with `--save` and no name, defaults to `transcript_YYYYmmdd_HHMMSS.txt` |
| `--no-overlay` | off | Console-only; skip the on-screen overlay |
| `--backend` | `auto` | `server` (GPU) / `local` (CPU or CUDA) / `auto` (server, with automatic fallback and recovery — see [Notes](#notes)) |
| `--server-url` | `http://127.0.0.1:8080` | whisper-server address |
| `--server-model` | `ggml-base-q5_1.bin` | GGML file the GPU server loads (GPU backend only). Picked from `_models\` in either panel; remembered for the next launch |
| `--model` | `_models\faster-whisper-medium` | Faster-Whisper model path (`local` backend, and the `auto` fallback) |
| `--local-device` | `cpu` | Where Faster-Whisper runs: `cpu`, `cuda`, or `auto` (cuda if it loads, else cpu). `cuda` needs the CUDA 12 wheels — see [requirements.txt](requirements.txt); a CUDA 13 Toolkit does **not** supply them |
| `--local-compute` | `auto` | CTranslate2 compute type. `auto` is `int8` on CPU and `float16` on CUDA; drop to `int8_float16` when VRAM is tight |
| `--buffer` | `4` | Rolling buffer length in seconds — how much audio each transcription pass sees |
| `--slide` | `2` | How far the buffer advances per pass. The overlap (`--buffer` minus `--slide`) is what the duplicate filter removes |
| `--silence-threshold` | `0.01` | Buffers quieter than this are skipped (a throttled `[audio] level ...` hint prints when skipping) |
| `--no-vad` | off | Disable Silero speech detection and gate on loudness alone |
| `--vad-threshold` | `0.5` | Speech probability above which a 32 ms frame counts as speech; lower catches quieter speech and more noise. Not the knob for singing — see `--vad-min-speech-ms` |
| `--vad-min-speech-ms` | `250` | How much speech a buffer needs before it is transcribed. Lower it (try `60`) to caption sung vocals |
| `--vad-model` | bundled | Path to a `silero_vad*.onnx`; defaults to the one shipped with Faster-Whisper |

Everything above still means exactly what it did. What follows is new.

| Flag | Default | Purpose |
|---|---|---|
| `--web` | off | Serve the browser control panel — see [Web UI](#web-ui) |
| `--web-host` / `--web-port` | `127.0.0.1` / `8770` | Where the panel listens. Set a token before leaving localhost |
| `--web-token` | none | Required as `?token=...` once set |
| `--no-web-open` | off | Do not open a browser on start |
| `--wpf` | off | Open the desktop control panel — see [Desktop panel](#desktop-panel-wpf). Independent of `--web`; both can be open at once |
| `--device` | system default | Capture device id or index, so nothing has to be answered at a console prompt. `--list-devices` prints them |
| `--file` | — | Transcribe a 16-bit PCM WAV (`--capture file`). `--file-fast` feeds it as fast as the backend takes it |
| `--strategy` | `sliding_window` | `sliding_window` (overlap + duplicate filter) or `silence_at_end_of_chunk` (wait for a pause, never cut a word) |
| `--chunk-length` / `--chunk-offset` / `--chunk-max-length` | `5` / `0.4` / `20` | Wait-for-silence tuning: minimum chunk, trailing quiet required, and the force-cut that stops a continuous talker producing nothing |
| `--dedup-threshold` | `0.80` | How alike two windows must read before the second is dropped as an overlap repeat |
| `--transcript-format` | `txt` | `txt` / `jsonl` / `srt` / `vtt` |
| `--no-word-timestamps` | off | Stop asking for per-word probability (it costs a little time) |
| `--confidence-warn` | `0.60` | Words below this are flagged in the panel |
| `--initial-prompt` | — | Vocabulary hint — names, jargon and spellings the model keeps getting wrong |
| `--beam-size` / `--best-of` | backend default | Higher is more accurate and measurably slower |
| `--audio-ctx` | full | Truncates the encoder context. The cheapest speed lever the GPU server has — `768` measured ~15% faster. Server backend only |
| `--temperature` / `--temperature-inc` | `0.0` / `0.2` | Decode temperature and its fallback step |
| `--no-speech-thold` | `0.6` | Whisper's own "was that speech" guard, after the Silero gate |
| `--suppress-nst` | off | Block music-cue / applause / `[BLANK_AUDIO]` tokens. Server backend only |
| `--max-context` | `-1` | Tokens of the previous window carried in as context. `0` makes windows independent. Server backend only |
| `--no-language-probabilities` | off | Skip the runner-up language list; measurably faster per request |
| `--overlay-font` / `--overlay-font-size` / `--overlay-fg` / `--overlay-bg` / `--overlay-opacity` / `--overlay-lines` / `--overlay-width` / `--overlay-y` / `--overlay-clear-after` / `--overlay-locked` / `--overlay-no-bold` | see `--help` | The overlay's appearance, previously hardcoded |
| `--preset` / `--save-preset` | — | Load or write a JSON preset. Flags you type still win over a preset |
| `--list-devices` | — | Print this machine's capture devices and exit |

`python live_transcription.py --help` is generated from the same schema, so it is
always the complete list.

## Notes

- **Faster-Whisper on an NVIDIA GPU is worth it, but not by default.** Measured here on an RTX 5070 with `faster-whisper-medium` over 26 s of speech: **8.09 s on CPU `int8`, 1.89 s on CUDA `float16`** — 4.3× — and 1.11 s through the app's own decode path. Two things will mislead you if you try this yourself. **Time more than one pass:** cuBLAS/cuDNN start-up and kernel JIT all land on the *first* inference, which measures CUDA at 10.45 s and makes the GPU look 34 % slower than the CPU it is four times faster than. **And "the model loaded" proves nothing:** without the CUDA 12 wheels on the DLL search path a CUDA model constructs fine and then dies on the first inference with `Library cublas64_12.dll is not found`. The app puts them on the path for you; installing them is one line in [requirements.txt](requirements.txt). The default stays `cpu` because under `--backend auto` this is the *fallback*, and the GPU is what just failed.

- **`--backend auto` recovers on its own.** If the GPU server stops answering, the script keeps trying for 5 consecutive passes (~10 s) — a single dropped request is a hiccup, not a dead server — then loads the CPU model and carries on, printing one line. While on CPU it re-checks the server once a minute with a 3-second probe and switches back on the first answer, again one line. The CPU model is only ever loaded the moment it is actually needed, so a session that never loses its server pays nothing for this. `--backend server` and `--backend local` are left alone; they mean what they say.
- **`--language` pins the spoken language**, and is worth setting when you know it. Detection is not a one-off: it reruns on every buffer, so a short, quiet or music-backed window can be decoded as a different language than the one before it — and the transcript follows. Pinning also removes the detection pass itself. Codes are Whisper's (`en`, `ms`, `ja`, `zh`, `haw`, ...); an unknown code is rejected at startup rather than mid-session, because the two backends disagree about what they accept — whisper.cpp tolerates `english`, Faster-Whisper does not. Captions are labelled with the short code either way — `[en→EN]`, never `[english→ENGLISH]`. whisper-server reports the full name and Faster-Whisper reports the code, so both are mapped to the code; without that, the same audio would change label mid-session the moment `auto` fell back to CPU.
- Default capture is **WASAPI loopback**: pick the output device you are *listening* on (Enter = Windows default). Whatever plays through it gets transcribed - no Stereo Mix required. Stereo Mix only matters for `--capture input`, and it only hears the Realtek output.
- **Why silence used to get transcribed.** Whisper does not stay quiet on audio with no speech in it — fed room tone it invents plausible text, which lands in the transcript looking like something that was said. The capture path made this worse by normalizing every ~128 ms chunk to full scale, which erased the level `--silence-threshold` is measured against: quiet room tone measured `0.0064` raw (below the threshold, so it should have been skipped) and `0.2214` after normalizing — indistinguishable from speech at `0.2279`. Loudness is now measured on the raw signal, and the window is normalized once before inference instead. On top of that, Silero VAD answers the real question: it scores the same room tone and fan hum at **0% speech** and real speech at **57%**, for about 25 ms per 8-second buffer. Use `--no-vad` if you would rather transcribe everything.
- **Captioning music needs `--vad-min-speech-ms`, not `--vad-threshold`.** Silero is trained on speech and sung vocals score nowhere near it. Frames over the default threshold, per 4-second window: speech `66 70 66 67 74 78 36 83 74 35 97`, music with vocals `2 3 2 11 2 6 7 15 0 5`, room tone and fan hum `0` in every window. So the default 250 ms (8 frames) rejects most music — correct for a speech tool, wrong if you want the lyrics. Lowering the *threshold* does not help: it moves every source toward passing at once, and at `0.05` music reaches 3/6 windows while room tone reaches 2/6. Lowering the *frame requirement* does, precisely because steady noise scores exactly zero: `--vad-min-speech-ms 60` admitted **9 of 10** music windows with room tone and hum still at **0 of 6**. The throttled skip line prints the actual count (`only 6 of the 8 speech frames needed`) and the value to try, so you can tune it instead of guessing. A window reported as **no speech at all** is a different case — there is no frame to count, so no setting of `--vad-min-speech-ms` reaches it; that is an instrumental passage, and `--no-vad` is the only way through. Note also that this is an absolute frame count, not a share of the window, so `--buffer` changes how strict it is in practice: 250 ms is 8 of the 125 frames in a 4-second buffer but 8 of 500 in a 16-second one. Sung vocals that get rejected at `--buffer 4` often pass untouched at the buffer `benchmark.py` recommends. The guided launcher exposes all of this as **Speech detection → [1] Speech / [2] Music / [3] Off**, so you do not need the command line for it.
- **Loud music drowns out speech for the VAD too**, not just for you. Mixing real speech with real music at known ratios, frames over the threshold per 4-second window: speech alone `69`, speech 6 dB *above* the music `75`, equal `59`, but music 6 dB *louder* than the speech drops it to `8` — right at the default cutoff — and 10 dB louder gives `7`. So a podcast over a loud backing track can be rejected as if nobody were talking. Pick **Music** in the launcher (or `--vad-min-speech-ms 60`) when that is your source.
- **Do not swap in a model from the silero-vad repo.** The one bundled with Faster-Whisper is the `h`/`c` export and it is *better* on exactly the hard case: at 6 dB of music over speech it scores a median 8 frames per window against 2 for the repo's `silero_vad.onnx`, and at 10 dB it scores 7 against 0. The repo's exports are marginally better on clean speech (median 80 vs 69) and clearly worse once music is involved. `--vad-model` exists for genuinely newer models, not for this swap.
- **Bigger buffers are not slower.** Whisper always encodes a padded 30-second window, so a 4-second buffer costs about what a 16-second one costs — measured on one machine: 3.89 s versus 3.70 s. That inverts the usual intuition: a small buffer is not the low-latency choice, it is just the one most likely to fall behind and start dropping audio. Run `benchmark.py` rather than guessing, and keep `--buffer` at or under 30.
- **A real song settles what the "Music" preset can and cannot do.** Captured 45 s of music through this project's own loopback path — a song with sung vocals, which Whisper transcribed correctly at 92% language confidence. Silero scores those vocals at **0 frames in 9 of 11 windows**. Even `--vad-min-speech-ms 32`, the lowest there is, admits only 2 of 11. So for heavily-produced vocals no frame count works, and the note below applies: with no speech frames at all there is nothing to count, and `--no-vad` is the only way through. The ASR is not the limitation — the gate is, on purpose. Real music also confirms the mixing numbers: windows passing the default gate are 8/8 with music 6 dB quieter than the speech, 8/8 at equal, **2/8** with music 6 dB louder, and 0/8 at 10 dB louder.
- **The bundled Silero export could caption a backing track; v6.2 does not.** `_models\silero_vad_v6.2.onnx` ships with the project and is preferred automatically over the one packaged inside faster-whisper. On that same song the bundled export scored one window at **11** frames — over the default 8-frame gate — where v6.2 never exceeds **2**. It also drops fan hum from 2 false-positive frames to 0, and more than doubles its score on speech buried under hum 6 dB louder (61 → 141), while leaving clean speech and room tone unchanged. Full tables in [UPSTREAM_MINING.md](UPSTREAM_MINING.md).
- **`--vad-min-silence-ms` below 250 ms does nothing.** It decides when a gap counts as the talker stopping rather than drawing breath, and it is what `silence_at_end_of_chunk` cuts on - together with `--chunk-offset`: the chunk is cut once the gate has closed the last run *and* the trailing silence has passed since it ended, whichever is later, so at the defaults (400 ms and 0.4 s) the two coincide. Segments found in a 49 s sample whose sentences number six: 100 ms → 13, 160 ms → 13, 250 ms → 13, **400 ms → 6**, 500 ms → 6, 800 ms → 5, 1200 ms → 1. This speaker leaves about 0.3 s between words and 0.85 s between sentences, so the useful setting sits between those; 400 ms is the default for that reason, and a faster talker needs less.
- **Your whisper-server build crashes on very short audio.** A 16-sample buffer segfaults it (exit 139); 100 samples and up survive. That is whisper.cpp #3956, fixed upstream on 2026-08-06 — the prebuilt binary in `_whisper.cpp\` predates it. Nothing in the app sends buffers that short, and `ServerBackend` now refuses anything under a quarter second regardless, so no code path can take the server down. Rebuilding whisper.cpp from a current checkout fixes it properly.
- **An English-only model reports a random language.** `ggml-*.en` models have no language tokens, so there is nothing for `--language auto` to detect with — but whisper-server answers the question anyway. Measured on `ggml-small.en-q5_1` with 11 seconds of clear English: all 100 entries in `language_probabilities` come back as `0.01002`, and the winner is reported as **`serbian`**. Without a guard that label rides along on every caption and changes from window to window, which looks exactly like the real "auto-detect wandered" problem — except no setting can fix it. This is now detected from the flatness of the distribution (not the file name, which is not in the response), captions are labelled `en`, and one line says so. Load a multilingual model if you actually need detection.
- **`--audio-ctx` is the cheapest speed lever the GPU server has.** It truncates the encoder's context; `768` measured about 15% faster than the full 1500 on the reference machine, for a model that was otherwise holding pace only just. Too low starts costing accuracy, so move it in steps and watch the confidence colouring in the panel.
- Overlay defaults to **center-bottom of the screen**, and everything about its appearance is now a setting rather than a constant.
- Use `Ctrl+C` in the console to stop transcription.
- The W5500 (8 GB VRAM) handles `medium-q5_0` comfortably; if Vulkan fails to initialize, update your GPU driver (Vulkan 1.2+ required by recent whisper.cpp builds).

## Folder Structure

```
project/
│
├─ live_transcription.py         # Console entry point: device prompt, then App
├─ live_transcription_lite.py    # Minimal console-only version, unchanged
├─ app.py                        # Wires pipeline + overlay + web panel together
│
├─ settings.py                   # EVERY option, declared once. The CLI, the web
│                                #   form and the validation are generated from it
├─ pipeline.py                   # The worker loop, reconfigurable while running
├─ audio_sources.py              # loopback / recording device / browser / WAV
├─ speech_gate.py                # Silero VAD: is there speech, and where did it stop
├─ buffering.py                  # Sliding window, or wait-for-silence
├─ whisper_backends.py           # whisper-server (GPU) / faster-whisper (CPU) / auto
├─ safe_paths.py                 # What an untrusted caller may name as a file to read
├─ transcript.py                 # txt / jsonl / srt / vtt writers
├─ overlay.py                    # The Tk caption strip, fully restyleable
├─ wsserver.py                   # RFC 6455 + static HTTP, standard library only
├─ benchmark.py                  # Times inference, recommends --buffer / --slide
│
├─ webui/                        # The control panel (served, not opened as a file)
│  ├─ index.html  app.js  style.css  audio-worklet.js  favicon.svg
│
├─ wpf_panel.py                  # The desktop panel's Python half: bridge + host
├─ build_ui.cmd                  # dotnet publish ui/ into ui/runtime (SDK, once)
├─ ui/                           # The desktop panel's View layer (C#, WPF)
│  ├─ Bridge/                    #   IEngineBridge - the whole Python boundary
│  ├─ ViewModels/  Settings/     #   the bindable state the XAML hangs off
│  ├─ Views/  Converters/        #   the window, tabs and field templates
│  ├─ Themes/                    #   charcoal brushes, generated by tools/gen_theme.py
│  ├─ tools/                     #   gen_theme.py, shot.py, soak.py
│  └─ runtime/                   #   committed publish output - cloners need no SDK
│
├─ Start Transcription.vbs       # ← START HERE. Double-click shortcut for the launcher
├─ run_pipeline.cmd              # THE launcher: venv + requirements + server + script
├─ start_whisper_server.cmd      # One component: the whisper.cpp GPU server
├─ console.py                    # Session log in logs/; hides the console once a panel is up
├─ presets/                      # Saved configurations (JSON)
├─ SETUP_AMD.md                  # AMD GPU setup walkthrough (CUDA note for NVIDIA inside)
├─ UPSTREAM_MINING.md            # What was mined from faster-whisper and whisper.cpp
├─ requirements.txt
├─ README.md
├─ _whisper.cpp/                 # whisper.cpp binaries (Vulkan or CUDA build)
└─ _models/
   ├─ ggml-base-q5_1.bin         # GGML model for whisper-server (GPU), default
   └─ faster-whisper-medium/     # Faster-Whisper model (CPU fallback)
```

The web panel adds no dependency. The desktop panel adds exactly one Python
package (`pythonnet`) plus the .NET 8 Desktop Runtime — and degrades to "no
panel" rather than "no transcription" when either is missing.

## Models

- **GPU (whisper-server):** `ggml-base-q5_1.bin` from [ggerganov/whisper.cpp on Hugging Face](https://huggingface.co/ggerganov/whisper.cpp/tree/main) is the default. Other sizes work too (`medium`, `large-v3`, `large-v3-turbo`, ...) — drop the file in `_models\` and either pick it on the panel's Engine tab, set `--server-model`, or pass it to the component directly: `start_whisper_server.cmd ggml-medium-q5_0.bin`. Smaller/more-quantized models transcribe faster, which matters: if inference takes longer than `--slide`, the script starts skipping audio to stay live.
- **The model you pick is remembered.** Starting the engine on a model writes it to `_state.json` (gitignored), so the next launch starts on the same one instead of falling back to the launcher's default. A preset or a typed `--server-model` still wins over it, and deleting the file restores the default. If the remembered `.bin` is gone, the server starts on the launcher's default and the log says why rather than refusing to start.
- **Bigger is not always slower.** On an RTX 5070 with a CUDA build, `ggml-large-v3-turbo-q8_0.bin` benchmarked *faster* than `ggml-small-q8_0.bin` end to end (553 ms vs 1243 ms) despite being three times the size, because turbo's decoder is four layers against small's twelve and decode steps dominate. Measure on your own card before assuming the small file is the fast one.
- **`large-v3-turbo` cannot translate.** It is a distilled *transcription* model — four decoder layers instead of thirty-two — and it accepts the translate task and then decodes the audio in its own language anyway. Measured against a live `whisper-server`: with `-l ja -tr`, `ggml-large-v3-turbo-q8_0.bin` returns Japanese while `ggml-large-v3.bin` and `ggml-base-q5_1.bin` return English for the identical request. Use `large-v3`, `medium` or `base` with `--translate`; turbo is fine for same-language captions. The app detects this and says so in the log rather than labelling the caption as English — see the `--translate` flag.
- **CPU (Faster-Whisper):** medium model recommended (`int8`). Path configurable via `--model`.

## Useful Links

- [whisper.cpp GitHub](https://github.com/ggml-org/whisper.cpp)
- [whisper.cpp Windows Vulkan prebuilt binaries](https://github.com/jerryshell/whisper.cpp-windows-vulkan-bin)
- [whisper.cpp official releases (CUDA/cuBLAS prebuilt zips)](https://github.com/ggml-org/whisper.cpp/releases)
- [Faster-Whisper GitHub](https://github.com/SYSTRAN/faster-whisper)
- [Python Documentation](https://docs.python.org/3/)

## License

MIT — see [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
