# GPU Setup for AMD (Radeon Pro W5500) — whisper.cpp Vulkan backend

`faster-whisper` is built on CTranslate2, which supports **NVIDIA CUDA or CPU only**.
The Radeon Pro W5500 (Navi 14 / gfx1012) is also **not** on AMD's ROCm/HIP SDK support
list for Windows, so there is no way for faster-whisper to ever use this GPU — the
old `device="cuda"` call was silently falling back to CPU.

The working GPU path on AMD is **whisper.cpp with the Vulkan backend**, run as a local
HTTP server (`whisper-server`). The Python scripts now talk to it automatically.

## Architecture

```
Stereo Mix ─> live_transcription.py ─ HTTP (localhost:8080) ─> whisper-server.exe (Vulkan / W5500)
                     │                                                │
                     └── CPU faster-whisper fallback if server is down┘
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

Get `ggml-medium-q5_0.bin` (~540 MB) from
https://huggingface.co/ggerganov/whisper.cpp/tree/main
and place it at:
```
live-audio-transcription\_models\ggml-medium-q5_0.bin
```
(Your existing `_models\faster-whisper-medium` folder stays — it is still used by the
CPU fallback.)

## 3. Start the server

```
start_whisper_server.bat
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

New flags:

| Flag | Default | Purpose |
|---|---|---|
| `--backend` | `auto` | `server` (GPU) / `local` (CPU) / `auto` |
| `--server-url` | `http://127.0.0.1:8080` | whisper-server address |
| `--model` | `_models\faster-whisper-medium` | CPU fallback model path |

## Notes / expectations

- The W5500 (8 GB VRAM) handles `medium-q5_0` comfortably; expect roughly
  2–5× faster than CPU on 4-second buffers.
- If Vulkan fails to initialize, update the Radeon Pro driver — Vulkan 1.2+
  is required by recent whisper.cpp builds.
- `pip install -r requirements.txt` (adds `requests`).
- Language auto-detect and per-request `--translate` are passed through to the
  server; no server restart needed to toggle translation.
