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
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate
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

### All flags

| Flag | Default | Purpose |
|---|---|---|
| `--capture` | `loopback` | `loopback` (capture any output device via WASAPI) / `input` (Stereo Mix, mic) |
| `--translate` | off | Translate speech to English as it transcribes |
| `--save` | off | Write the transcript to a text file |
| `--output` | auto-named | Transcript path; with `--save` and no name, defaults to `transcript_YYYYmmdd_HHMMSS.txt` |
| `--no-overlay` | off | Console-only; skip the on-screen overlay |
| `--backend` | `auto` | `server` (GPU) / `local` (CPU) / `auto` (server, with automatic CPU fallback and recovery — see [Notes](#notes)) |
| `--server-url` | `http://127.0.0.1:8080` | whisper-server address |
| `--model` | `_models\faster-whisper-medium` | Faster-Whisper model path (CPU backend only) |
| `--buffer` | `4` | Rolling buffer length in seconds — how much audio each transcription pass sees |
| `--slide` | `2` | How far the buffer advances per pass. The overlap (`--buffer` minus `--slide`) is what the duplicate filter removes |
| `--silence-threshold` | `0.01` | Buffers quieter than this are skipped (a throttled `[audio] level ...` hint prints when skipping) |

## Notes

- **`--backend auto` recovers on its own.** If the GPU server stops answering, the script keeps trying for 5 consecutive passes (~10 s) — a single dropped request is a hiccup, not a dead server — then loads the CPU model and carries on, printing one line. While on CPU it re-checks the server once a minute with a 3-second probe and switches back on the first answer, again one line. The CPU model is only ever loaded the moment it is actually needed, so a session that never loses its server pays nothing for this. `--backend server` and `--backend local` are left alone; they mean what they say.
- Default capture is **WASAPI loopback**: pick the output device you are *listening* on (Enter = Windows default). Whatever plays through it gets transcribed - no Stereo Mix required. Stereo Mix only matters for `--capture input`, and it only hears the Realtek output.
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
