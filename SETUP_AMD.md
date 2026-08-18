# GPU Setup for AMD (Radeon Pro W5500) — whisper.cpp Vulkan backend

`faster-whisper` is built on CTranslate2, which supports **NVIDIA CUDA or CPU only**.
The Radeon Pro W5500 (Navi 14 / gfx1012) is also **not** on AMD's ROCm/HIP SDK support
list for Windows, so there is no way for faster-whisper to ever use this GPU — the
old `device="cuda"` call was silently falling back to CPU.

The working GPU path on AMD is **whisper.cpp with the Vulkan backend**, run as a local
HTTP server (`whisper-server`). The Python scripts now talk to it automatically.

> **NVIDIA users:** you don't need the Vulkan-specific steps below. Grab a **CUDA build**
> of whisper.cpp instead — the official [releases](https://github.com/ggml-org/whisper.cpp/releases)
> ship `whisper-cublas` zips — drop `whisper-server.exe` + DLLs into `_whisper.cpp\`, and
> use `start_whisper_server.cmd <model>` exactly as described. The app and launcher are
> backend-agnostic; only the build you place in `_whisper.cpp\` differs.

## Architecture

```
Stereo Mix ─> live_transcription.py ─ HTTP (localhost:8080) ─> whisper-server.exe (Vulkan or CUDA)
                     │                                                │
                     └── CPU faster-whisper fallback if server is down┘
                         (also if it goes down later - and back to GPU
                          on its own when it returns)
```

## 1. Get a Vulkan build of whisper.cpp

The official whisper.cpp releases do not currently ship Windows Vulkan binaries, so
either download a community build or compile once yourself:

**Option A — prebuilt (fastest):**
- https://github.com/jerryshell/whisper.cpp-windows-vulkan-bin (Releases → zip)
- https://github.com/DomoticX/whisper.cpp-windows-vulkan

**Option B — build yourself (official source, ~5 min):**
```bash
git clone https://github.com/ggml-org/whisper.cpp
cd whisper.cpp
cmake -B build -DGGML_VULKAN=1
cmake --build build --config Release
# binaries land in build\bin\Release\
```
(Requires Visual Studio C++ tools, CMake, and the LunarG Vulkan SDK.)

Copy `whisper-server.exe` **and all DLLs next to it** into:
```
live-audio-transcription\_whisper.cpp\
```

## 2. Download the GGML model

Get `ggml-base-q5_1.bin` from
https://huggingface.co/ggerganov/whisper.cpp/tree/main
and place it at:
```
live-audio-transcription\_models\ggml-base-q5_1.bin
```
That filename is what `start_whisper_server.cmd` loads by default. To use a
different model, drop it in `_models\` and pass its filename:
```
start_whisper_server.cmd ggml-medium-q5_0.bin
```
Bigger models are more accurate but slower — if inference takes longer than
`--slide` seconds, `live_transcription.py` starts skipping audio to stay live
and prints a `[perf]` warning.

(Your existing `_models\faster-whisper-medium` folder stays — it is still used by the
CPU fallback.)

## 3. Start the server

```
start_whisper_server.cmd
```
The startup log should show your GPU, e.g.
`ggml_vulkan: 0 = AMD Radeon Pro W5500 ...`. Leave this window running.

## 4. Run transcription as before

```bash
python live_transcription.py              # auto: uses GPU server, falls back to CPU
python live_transcription.py --translate  # translate to English
python live_transcription.py --backend server   # GPU only, fail loudly if server down
python live_transcription.py --backend local    # force CPU faster-whisper
python live_transcription_lite.py         # console-only, auto backend
```

Backend flags added by this setup:

| Flag | Default | Purpose |
|---|---|---|
| `--backend` | `auto` | `server` (GPU) / `local` (CPU) / `auto` (switches between them as the server comes and goes) |
| `--server-url` | `http://127.0.0.1:8080` | whisper-server address |
| `--model` | `_models\faster-whisper-medium` | CPU fallback model path |

See the **All flags** table in [README.md](README.md) for the complete list —
`--capture`, `--buffer`, `--slide`, `--silence-threshold` and the rest live there
so there is only one place to keep current.

## Notes / expectations

- The W5500 (8 GB VRAM) handles `medium-q5_0` comfortably; expect roughly
  2–5× faster than CPU on 4-second buffers.
- If Vulkan fails to initialize, update the Radeon Pro driver — Vulkan 1.2+
  is required by recent whisper.cpp builds.
- **If `whisper-server` exits silently** right after `using ... backend`, with
  no error of its own, check the exit code: `0xC000001D` is
  STATUS_ILLEGAL_INSTRUCTION, meaning the prebuilt binaries were compiled for
  CPU instructions your machine does not have (commonly AVX-512 — absent on
  most mobile Intel chips). Confirm it by adding `-ng`: if it still dies with
  the GPU disabled, it is the CPU build target, not Vulkan. The model loads
  first regardless, because loading is plain code and the fault happens on the
  first compute kernel. Fix by building from source on that machine
  (Option B above — CMake targets the host CPU) or finding a build that
  matches it. `start_whisper_server.cmd` detects this case and says so.
- `pip install -r requirements.txt` (adds `requests`).
- Language auto-detect and per-request `--translate` are passed through to the
  server; no server restart needed to toggle translation.
- You can close and restart the server window without restarting transcription:
  on `--backend auto` the script falls back to CPU after ~10 s of failures and
  picks the server back up within a minute of it returning. One log line each
  way. Use `--backend server` if you would rather it fail loudly instead.
