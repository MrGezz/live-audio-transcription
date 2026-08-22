# Contributing to Live Audio Transcription

Thank you for considering contributing! This guide covers how this project actually works — the setup, the architecture invariants that must never regress, and the review etiquette this repo has learned the hard way.

## Project at a Glance

| File | What it is |
|---|---|
| `live_transcription.py` | Main app: audio capture → buffer window → backend → overlay / console |
| `whisper_backends.py` | Backend abstraction: whisper.cpp server (GPU), faster-whisper (CPU), `auto` (state machine) |
| `live_transcription_lite.py` | Console-only version, zero arguments |
| `benchmark.py` | Times inference through `create_backend()`; recommends `--buffer` / `--slide` |
| `speech_gate.py` | Silero VAD wrapper; skips buffers with no speech before they reach a backend |
| `start_whisper_server.cmd` | Starts the whisper.cpp GPU server (`_whisper.cpp\whisper-server.exe`) |
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

Working on the desktop panel (`ui/`) additionally needs the **.NET 8 SDK**,
once, to run `.\build_ui.cmd`; merely *running* the panel needs only the
Desktop Runtime, because the build output is committed under `ui/runtime`.
`ui\tools\shot.py` renders every tab to a PNG for review (`--light` for the
light pair, `--runtime DIR` to review a publish that is not `ui/runtime`
yet), and `ui\tools\soak.py --minutes 5` is the smoke version of the
four-hour leak soak (run the full one before building more UI on top of a
binding-layer change; exit 0 passed, 1 failed, 2 not judged). Both were
first run on real hardware on 2026-08-23 — PENDING_WORK.md steps 6 and 8
record what that found. `global.json` pins the build to an 8.0 SDK (see
invariant 11): the newest installed SDK (10.x) would otherwise rewrite
`ui/runtime`'s `deps.json` and `runtimeconfig.json` against the committed
output. While a soak runs it holds
`ui/runtime/LiveTranscription.Ui.dll`, so publish to a scratch directory
and point `shot.py --runtime` at it until the soak ends.

To run against the GPU backend:

1. `start_whisper_server.cmd ggml-medium-q5_0.bin` (no argument = default `ggml-base-q5_1.bin`)
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
7. **Language is normalized in one place.** `normalize_language()` in `whisper_backends.py` is the only thing that decides what "detect it" means, because the backends disagree: the HTTP API wants the string `"auto"`, faster-whisper wants `None` and raises `ValueError` on `"auto"`. Codes are validated up front for the same reason — whisper.cpp accepts `english`, faster-whisper does not, so an unvalidated code runs fine on GPU and kills transcription the moment `auto` falls back to CPU. Every backend stores the normalized value and re-spells it at the call site. The same applies in the other direction: whisper-server *reports* a full name (`english`) where faster-whisper reports a code (`en`), so `language_code()` maps both to the code before it reaches a caption — otherwise the label changes mid-session on an auto fallback.
8. **Never normalize per chunk.** Capture hands the worker raw audio; the window is scaled once, immediately before inference. Peak-normalizing each ~128 ms block destroys the only evidence the silence threshold and the VAD have — measured, quiet room tone goes from `0.0064` (correctly below the threshold) to `0.2214`, against speech at `0.2279`. It is also a per-chunk AGC that pumps gain inside a single word.
9. **Silero needs its 64-sample context.** Both ONNX export styles declare a dynamic input shape and will happily accept a bare 512-sample hop — then return near-zero probability for everything, which is indistinguishable from "no speech" and silently stops the transcript. Frames must be `64 + 512`. `speech_gate.py` builds them in one place for exactly this reason. For the same family of reasons `create()` must run the model once before returning it, and `speech_frames()` must never raise into the caller: the worker calls it *outside* the `try/except` that guards `transcribe()`, so a model that loads and then throws kills the worker thread and leaves the app running with no captions and nothing in the log. Input names identify an export but do not prove it runs — upstream ships streaming models both with and without an `sr` input, and feeding an undeclared input is a hard `InvalidArgument`.
10. **The threshold and the frame count are not one knob.** Do not collapse `--vad-threshold` and `--vad-min-speech-ms` into a single "sensitivity" setting — they separate different things, and only one of them separates music from noise. Measured, frames over `0.5` per 4-second window: speech `66 70 66 67 74 78 36 83 74 35 97`, music with vocals `2 3 2 11 2 6 7 15 0 5`, room tone and fan hum `0` in every window. Lowering the *threshold* moves every source toward passing together (at `0.05`, music reaches 3/6 windows and room tone 2/6 — no useful gap). Lowering the *frame requirement* works precisely because steady noise scores zero: `60 ms` admitted 9/10 music windows with noise still at 0/6.
11. **After any `ui/` change: `.\build_ui.cmd`, then commit `ui/runtime`.** The desktop panel's publish output is committed so a fresh clone runs it with only the .NET Desktop Runtime — no SDK, no restore, no network. Nothing enforces the rebuild but this line: a PR that edits `ui/*.cs` or `ui/*.xaml` without a matching `ui/runtime` diff ships a panel that silently does not contain the change. (`.\build_ui.cmd` with the leading `.\` — cmd does not resolve a bare batch name.) `global.json` at the repo root pins the build to the 8.0 SDK band (`rollForward: latestFeature`, so any installed 8.0.x is accepted) — keep it. Without it `dotnet` takes the newest installed SDK, and a 10.x SDK rewrites `ui/runtime`'s `deps.json` and `runtimeconfig.json` (it prunes framework-provided packages and swaps a `configProperties` key) — a diff with no source change behind it. With the pin, a rebuild after a source edit changes `LiveTranscription.Ui.dll` and nothing else; if `git diff --stat ui/runtime` shows more than that, the wrong SDK built it.
12. **Bridge methods return promptly and never touch the window.** Everything in `wpf_panel.py`'s `EngineBridge` runs ON the WPF dispatcher thread holding the GIL; slow work goes through `App._lifecycle`, and state goes back in through `PanelHost.Post*` / `Panel.event`, which marshal asynchronously. A blocking `Dispatcher.Invoke` from a bridge method — or joining the panel thread from a bridge method, which is what `Panel.close()` does — is the one deadlock this architecture can produce; `_request_exit` uses `Panel.request_close()` for exactly that reason.
13. **The settings pane stays generated.** Adding a `Field(...)` to `settings.py` must make a control appear in *both* panels with no UI edit. Anything that special-cases a key by name in `ui/Settings` or `webui/app.js` is a hole in that; there is exactly one sanctioned hole (`model`, rendered as a picker over `_models` — both panels make the same exception, see `SettingsVm.SetModels`). A new field *kind* is different: it needs a template in `ui/Views/FieldTemplates.xaml`, a case in `FieldTemplateSelector`, and the kind added to `SettingsVm.KnownKinds` — the startup assert fails loudly listing exactly these three places until all of them exist.

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
