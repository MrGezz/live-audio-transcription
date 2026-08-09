# Live System Audio Transcription with Overlay

This project transcribes **system audio in real-time** and displays it in a **modern, draggable overlay**, with optional translation to English and transcript saving.

This also adds **GPU support via whisper.cpp** — Vulkan for AMD/Intel, CUDA for NVIDIA — behind a pluggable backend system:

- **GPU backend** — [whisper.cpp](https://github.com/ggml-org/whisper.cpp) `whisper-server` (Vulkan or CUDA build), reached over local HTTP. Vulkan works on AMD, Intel, and NVIDIA; a CUDA build is the stronger choice on NVIDIA.
- **CPU backend** — [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) (`int8`), used as an automatic fallback when the server is not running — including when it stops running *mid-session*.

> **Why?** Faster-Whisper is built on CTranslate2, which supports NVIDIA CUDA or CPU only. On AMD cards the old `device="cuda"` call silently fell back to CPU. The Vulkan path gives AMD cards real GPU acceleration. NVIDIA users can instead drop a CUDA build into `_whisper.cpp\` — the app does not care which build the server was compiled with.

![Sample Image](sample.png)

## Architecture

```
Stereo Mix ──> live_transcription.py ── HTTP (localhost:8080) ──> whisper-server.exe (Vulkan/CUDA GPU)
                      │                                                   │
                      └────── CPU faster-whisper fallback if the server is down, or goes down
```

## Features

- Live transcription of system audio via **WASAPI loopback of any output device** (headset, speakers - no Stereo Mix needed), or classic Stereo Mix / mic input (`--capture input`).
- **GPU acceleration via whisper.cpp** — Vulkan (AMD/Intel) or CUDA (NVIDIA) — (`--backend server`).
- Automatic backend selection with CPU fallback (`--backend auto`, default) — if the GPU server dies mid-session it drops to CPU rather than going silent, and returns to GPU on its own once the server is back.
- Optional translation to English (`--translate`) — passed per-request, no server restart needed.
- Optional **spoken-language pinning** (`--language ms`) — skips per-buffer auto-detection, which can otherwise disagree with itself on short or noisy windows.
- **Silero voice-activity detection** — buffers with no speech in them are skipped before inference, so fans, music and room tone stop producing hallucinated captions (and stop costing GPU time). No extra dependency; disable with `--no-vad`.
- Live overlay at the bottom center of the screen; transparent and draggable.
- Optional transcript saving (`--save` and `--output`).
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

4. **Set up the GPU backend (recommended, AMD-friendly)** — see [SETUP_AMD.md](SETUP_AMD.md) for full details:
   - Place a whisper.cpp build — Vulkan (AMD/Intel) or CUDA (NVIDIA) — with `whisper-server.exe` and its DLLs under `_whisper.cpp\`
   - Download [`ggml-base-q5_1.bin`](https://huggingface.co/ggerganov/whisper.cpp/tree/main) into `_models\` — this is the filename `start_whisper_server.bat` looks for by default. Any other GGML model works too; pass its filename as an argument: `start_whisper_server.bat ggml-medium-q5_0.bin`

5. **Set up the CPU fallback (optional)**
   - Place the Faster-Whisper medium model under `_models/faster-whisper-medium`

6. **Start the GPU server, then run the script**
```bash
start_whisper_server.bat
python live_transcription.py
```
The server startup log should list your GPU as a Vulkan device (e.g. `ggml_vulkan: 0 = ...`) or, on a CUDA build, `ggml_cuda_init: found N CUDA devices`. Leave that window running.

> **Prefer a guided setup?** Double-click `Start Transcription.vbs` (or run `run_pipeline.cmd`). It asks which script, capture mode and flags you want, then creates the venv, installs requirements, and starts the server for you.

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
| `--translate` | off | Translate speech to English as it transcribes |
| `--language` | `auto` | Spoken language as a Whisper code (`en`, `ms`, `ja`, `zh`, ...). `auto` detects it on every buffer; pin it when you know it — see [Notes](#notes) |
| `--save` | off | Write the transcript to a text file |
| `--output` | auto-named | Transcript path; with `--save` and no name, defaults to `transcript_YYYYmmdd_HHMMSS.txt` |
| `--no-overlay` | off | Console-only; skip the on-screen overlay |
| `--backend` | `auto` | `server` (GPU) / `local` (CPU) / `auto` (server, with automatic CPU fallback and recovery — see [Notes](#notes)) |
| `--server-url` | `http://127.0.0.1:8080` | whisper-server address |
| `--model` | `_models\faster-whisper-medium` | Faster-Whisper model path (CPU backend only) |
| `--buffer` | `4` | Rolling buffer length in seconds — how much audio each transcription pass sees |
| `--slide` | `2` | How far the buffer advances per pass. The overlap (`--buffer` minus `--slide`) is what the duplicate filter removes |
| `--silence-threshold` | `0.01` | Buffers quieter than this are skipped (a throttled `[audio] level ...` hint prints when skipping) |
| `--no-vad` | off | Disable Silero speech detection and gate on loudness alone |
| `--vad-threshold` | `0.5` | Speech probability above which a 32 ms frame counts as speech; lower catches quieter speech and more noise. Not the knob for singing — see `--vad-min-speech-ms` |
| `--vad-min-speech-ms` | `250` | How much speech a buffer needs before it is transcribed. Lower it (try `60`) to caption sung vocals |
| `--vad-model` | bundled | Path to a `silero_vad*.onnx`; defaults to the one shipped with Faster-Whisper |

## Notes

- **`--backend auto` recovers on its own.** If the GPU server stops answering, the script keeps trying for 5 consecutive passes (~10 s) — a single dropped request is a hiccup, not a dead server — then loads the CPU model and carries on, printing one line. While on CPU it re-checks the server once a minute with a 3-second probe and switches back on the first answer, again one line. The CPU model is only ever loaded the moment it is actually needed, so a session that never loses its server pays nothing for this. `--backend server` and `--backend local` are left alone; they mean what they say.
- **`--language` pins the spoken language**, and is worth setting when you know it. Detection is not a one-off: it reruns on every buffer, so a short, quiet or music-backed window can be decoded as a different language than the one before it — and the transcript follows. Pinning also removes the detection pass itself. Codes are Whisper's (`en`, `ms`, `ja`, `zh`, `haw`, ...); an unknown code is rejected at startup rather than mid-session, because the two backends disagree about what they accept — whisper.cpp tolerates `english`, Faster-Whisper does not. Captions are labelled with the short code either way — `[en→EN]`, never `[english→ENGLISH]`. whisper-server reports the full name and Faster-Whisper reports the code, so both are mapped to the code; without that, the same audio would change label mid-session the moment `auto` fell back to CPU.
- Default capture is **WASAPI loopback**: pick the output device you are *listening* on (Enter = Windows default). Whatever plays through it gets transcribed - no Stereo Mix required. Stereo Mix only matters for `--capture input`, and it only hears the Realtek output.
- **Why silence used to get transcribed.** Whisper does not stay quiet on audio with no speech in it — fed room tone it invents plausible text, which lands in the transcript looking like something that was said. The capture path made this worse by normalizing every ~128 ms chunk to full scale, which erased the level `--silence-threshold` is measured against: quiet room tone measured `0.0064` raw (below the threshold, so it should have been skipped) and `0.2214` after normalizing — indistinguishable from speech at `0.2279`. Loudness is now measured on the raw signal, and the window is normalized once before inference instead. On top of that, Silero VAD answers the real question: it scores the same room tone and fan hum at **0% speech** and real speech at **57%**, for about 25 ms per 8-second buffer. Use `--no-vad` if you would rather transcribe everything.
- **Captioning music needs `--vad-min-speech-ms`, not `--vad-threshold`.** Silero is trained on speech and sung vocals score nowhere near it. Frames over the default threshold, per 4-second window: speech `66 70 66 67 74 78 36 83 74 35 97`, music with vocals `2 3 2 11 2 6 7 15 0 5`, room tone and fan hum `0` in every window. So the default 250 ms (8 frames) rejects most music — correct for a speech tool, wrong if you want the lyrics. Lowering the *threshold* does not help: it moves every source toward passing at once, and at `0.05` music reaches 3/6 windows while room tone reaches 2/6. Lowering the *frame requirement* does, precisely because steady noise scores exactly zero: `--vad-min-speech-ms 60` admitted **9 of 10** music windows with room tone and hum still at **0 of 6**. The throttled skip line prints the actual count (`only 6 of the 8 speech frames needed`) and the value to try, so you can tune it instead of guessing. A window reported as **no speech at all** is a different case — there is no frame to count, so no setting of `--vad-min-speech-ms` reaches it; that is an instrumental passage, and `--no-vad` is the only way through. Note also that this is an absolute frame count, not a share of the window, so `--buffer` changes how strict it is in practice: 250 ms is 8 of the 125 frames in a 4-second buffer but 8 of 500 in a 16-second one. Sung vocals that get rejected at `--buffer 4` often pass untouched at the buffer `benchmark.py` recommends. The guided launcher exposes all of this as **Speech detection → [1] Speech / [2] Music / [3] Off**, so you do not need the command line for it.
- **Loud music drowns out speech for the VAD too**, not just for you. Mixing real speech with real music at known ratios, frames over the threshold per 4-second window: speech alone `69`, speech 6 dB *above* the music `75`, equal `59`, but music 6 dB *louder* than the speech drops it to `8` — right at the default cutoff — and 10 dB louder gives `7`. So a podcast over a loud backing track can be rejected as if nobody were talking. Pick **Music** in the launcher (or `--vad-min-speech-ms 60`) when that is your source.
- **Do not swap in a model from the silero-vad repo.** The one bundled with Faster-Whisper is the `h`/`c` export and it is *better* on exactly the hard case: at 6 dB of music over speech it scores a median 8 frames per window against 2 for the repo's `silero_vad.onnx`, and at 10 dB it scores 7 against 0. The repo's exports are marginally better on clean speech (median 80 vs 69) and clearly worse once music is involved. `--vad-model` exists for genuinely newer models, not for this swap.
- **Bigger buffers are not slower.** Whisper always encodes a padded 30-second window, so a 4-second buffer costs about what a 16-second one costs — measured on one machine: 3.89 s versus 3.70 s. That inverts the usual intuition: a small buffer is not the low-latency choice, it is just the one most likely to fall behind and start dropping audio. Run `benchmark.py` rather than guessing, and keep `--buffer` at or under 30.
- Overlay defaults to **center-bottom of the screen**.
- Use `Ctrl+C` in the console to stop transcription.
- The W5500 (8 GB VRAM) handles `medium-q5_0` comfortably; if Vulkan fails to initialize, update your GPU driver (Vulkan 1.2+ required by recent whisper.cpp builds).

## Folder Structure

```
project/
│
├─ live_transcription.py         # Main script with overlay and optional saving
├─ live_transcription_lite.py    # Console-only version
├─ whisper_backends.py           # Backend abstraction: whisper-server (GPU) / faster-whisper (CPU)
├─ benchmark.py                  # Times inference, recommends --buffer / --slide
├─ speech_gate.py                # Silero VAD: skip buffers with no speech in them
├─ start_whisper_server.bat      # Launches whisper.cpp GPU server (Vulkan/CUDA)
├─ run_pipeline.cmd              # Guided launcher: venv + server + script, with prompts
├─ Start Transcription.vbs       # Double-click shortcut for run_pipeline.cmd
├─ SETUP_AMD.md                  # AMD GPU setup walkthrough (CUDA note for NVIDIA inside)
├─ requirements.txt
├─ README.md
├─ _whisper.cpp/                 # whisper.cpp binaries (Vulkan or CUDA build)
└─ _models/
   ├─ ggml-base-q5_1.bin        # GGML model for whisper-server (GPU), default
   └─ faster-whisper-medium/     # Faster-Whisper model (CPU fallback)
```

## Models

- **GPU (whisper-server):** `ggml-base-q5_1.bin` from [ggerganov/whisper.cpp on Hugging Face](https://huggingface.co/ggerganov/whisper.cpp/tree/main) is the default. Other sizes work too (`medium`, `large-v3-turbo`, ...) — drop the file in `_models\` and pass its filename: `start_whisper_server.bat ggml-medium-q5_0.bin`. Smaller/more-quantized models transcribe faster, which matters: if inference takes longer than `--slide`, the script starts skipping audio to stay live.
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
