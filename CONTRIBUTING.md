# Contributing to Live Audio Transcription

Thank you for considering contributing! This guide covers how this project actually works — the setup, the architecture invariants that must never regress, and the review etiquette this repo has learned the hard way.

## Project at a Glance

| File | What it is |
|---|---|
| `live_transcription.py` | Main app: audio capture → buffer window → backend → overlay / console |
| `whisper_backends.py` | Backend abstraction: whisper.cpp server (GPU), faster-whisper (CPU), `auto` (state machine) |
| `live_transcription_lite.py` | Console-only version, zero arguments |
| `start_whisper_server.bat` | Starts the whisper.cpp GPU server (`_whisper.cpp\whisper-server.exe`) |
| `run_pipeline.cmd` / `Start Transcription.vbs` | Guided launcher / double-click shortcut |
| `SETUP_AMD.md` | GPU setup walkthrough (build or download whisper.cpp, models) |

Runtime layout (gitignored, not part of the repo):

```
_whisper.cpp/   whisper-server.exe + DLLs (Vulkan or CUDA build)
_models/        GGML .bin models (GPU server) + faster-whisper model folder (CPU fallback)
```

## Development Environment

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

To run against the GPU backend:

1. `start_whisper_server.bat ggml-medium-q5_0.bin` (no argument = default `ggml-base-q5_1.bin`)
2. Wait until the server responds: `curl -s -o NUL -w "%%{http_code}" http://127.0.0.1:8080/` → `200`
3. `set PYTHONUTF8=1` then `.venv\Scripts\python live_transcription.py`

**Why `PYTHONUTF8=1`?** Captions print the `→` character (U+2192), which does not exist in the Windows cp1252 console encoding. On a plain console the worker thread raises `UnicodeEncodeError` and dies *silently* — the app keeps running with no captions. Run from Windows Terminal or set the env var.

## Architecture Invariants (do not regress)

Each of these fixes a real bug we found in review. A PR that weakens one needs a strong justification.

1. **Trim *before* transcribe.** The window handed to `transcribe()` must be bounded (max `BUFFER_LENGTH_SEC + 2*BUFFER_SLIDE_SEC`). Trimming after drain-and-append, or leaving the buffer unbounded, makes latency feed on itself — measured 4.1 s → 53.2 s of backlog on a slow GPU. The worker drains the queue, bounds the window, *then* transcribes.
2. **Error backoff.** Log the first 3 consecutive errors, then every 10th. The backend layer counts failures per session; per-line logging floods the console when a server dies.
3. **Dedupe against the previous window's full segment set**, not just the last line. A window can return multiple segments; comparing to the last line lets duplicates through.
4. **AutoBackend state machine** (`--backend auto`): 5 consecutive failures → fall back to CPU; probe the server every 60 s (3 s timeout); failures are counted *per server* so a broken CPU model can't flip back to a dead server; a sticky local error stays on CPU.
5. **Explicit device-index validation.** `speakers[-1]` would silently select the last device via Python's negative indexing — always range-check before indexing.
6. **bat parsing.** Inside `( )` blocks cmd expands `%VAR%` at parse time; paths containing `)` break the block. Keep the existing quoting patterns, and read `%ERRORLEVEL%` outside the block.
7. **`language=auto` is hardcoded** per request in `whisper_backends.py`. Known limitation — a `--language` flag is a welcome, small PR.

## Testing Expectations

- **Empirically verify behavior changes.** PR #1 was won on evidence, not opinions: a fake whisper-server for backend tests (zero model downloads), stub audio drivers, and real runs against a real server + model.
- **Latency claims need numbers** — windows/sec, `infer_s` vs `--slide`, before/after. "Feels faster" doesn't survive review.
- **The AutoBackend test suite** (55-clock-fake tests) was written during development but never committed. Adopting it, or writing your own equivalent, is welcome.
- **GPU-specific behavior needs a real GPU run.** Don't claim "holds real time" without a measured `infer_s`.

## Git Workflow & Branching

- **Never submit a PR from `main`.** Create a dedicated branch: `feature/...` or `fix/...`.
- **One PR = one change.** A single feature or bug fix; no unrelated edits mixed in.
- **Keep `main` green.** Housekeeping (docs, license, CI) can land on `main`; behavior changes go through review.

## Review Etiquette

- **Draft PRs until the code is final.** Convert to "Ready for Review" only when you're done.
- **No mid-review pushes.** Once a reviewer has started, don't push new commits except in direct response to feedback — continuous updates invalidate existing code suggestions and waste the reviewer's time.
- **Test locally before opening.** If it doesn't run on your machine, it doesn't go up.

## Responsible AI Code Generation

If you use AI tools to assist your development:

- **You are responsible for what you push.** If you can't explain the logic, syntax, or architecture of your PR, it will be rejected.
- **No live AI-prompting on open PRs.** Iterate with your AI locally, on your own machine, before opening the PR.
- **Verify before claiming.** AI suggestions that touch the invariants above need the same empirical proof as anything else.

## PR Checklist

- [ ] Branch from `main`, focused scope
- [ ] `requirements.txt` updated if dependencies changed
- [ ] Runs with the server backend AND the CPU fallback (or an explanation of why not)
- [ ] No regression of the architecture invariants above
- [ ] Behavior/latency changes documented with before/after numbers
- [ ] Output is cp1252-safe (no `→` crash without `PYTHONUTF8=1`)
- [ ] README / `SETUP_AMD.md` updated if flags, layout, or models changed
