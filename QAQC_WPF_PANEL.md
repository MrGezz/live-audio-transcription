# QA/QC of the desktop panel (`ui/`, the C# View layer) — 2026-09-25

A read-through of every file under `ui/` (about 6,600 lines of C# and XAML)
and of `wpf_panel.py`, checked against the Python side of each bridge
contract: what `app.py` and `pipeline.py` actually put in the documents and
events the C# reads. Done on a Linux box with the .NET 8 SDK
(`8.0.425`, inside `global.json`'s band), where the panel compiles with zero
warnings but cannot be run — so everything here is by reading and by build,
and the panel tests (`tests/test_panel.py`) still have to be run on Windows
to confirm. That is written down at the end rather than assumed.

## What was checked and holds

- **Every bridge document key the C# reads exists on the Python side.**
  `state` (`running`, `paused`, `source_alive`, `backend`, `backend_name`,
  `error`, `strategy`, `gate`, `saving`, `source`, `uptime_s`, `stats.xrt`,
  `stats.windows`), `meter`, `perf`, `benchmark` (`running`/`row`/`done`/
  `error` and their fields), `engine` (including `busy`, which
  `_broadcast_engine` always attaches — `MainVm.ApplyEngine` overwriting
  `Busy` from it is therefore correct), `models`, `modelCatalog`, the preset
  acks (`saved`/`loaded`/`deleted`/`error`/`errors`/`presets`), `export`
  (`filename`, `count`, `content`), transcript entries and their `words`.
- **Word runs need no spacing added.** Both backends keep whisper's leading
  space in `word`, so `WordInlines` concatenating `Run`s reads as prose, the
  same as the browser's spans.
- **Light/dark key parity.** `Panel.xaml`/`PanelLight.xaml` (27 keys) and
  `Charcoal.xaml`/`CharcoalLight.xaml` (337 keys) define identical key sets —
  the silent-black failure the WPF-UI retheming note warns about cannot
  happen here today.
- **The stamp was current** (`sources.sha256` matched the tree) and the
  committed `deps.json` / `runtimeconfig.json` reproduce byte-for-byte from
  the pinned SDK band, as `build_ui.cmd` claims.
- **Threading rules (invariant 12) hold in the C#.** Nothing on the Python
  side blocks on the dispatcher; every push is a `BeginInvoke`; meter frames
  bypass it; `RelayCommand.Execute` catches so a Python exception cannot end
  `Application.Run`.
- `ConditionEvaluator` compares by `JsonValueKind`, `Doc` never throws, the
  transcript list is explicitly virtualised, `SetPresets` never `Clear()`s a
  bound collection — the traps PENDING_WORK records are all still closed.

## Findings

### Fixed in this pass (needs `.\build_ui.cmd` + `tests.test_panel` on Windows)

1. **A machine with only the .NET 9 or 10 Desktop Runtime could not host the
   panel.** `RollForward` was `LatestMinor`, which never leaves the requested
   major (Microsoft's roll-forward table: `LatestMinor` on a request for
   `8.0.0` with only `9.x` installed fails). The csproj comment, the README
   and `wpf_panel._explain`'s "8.0 or newer" all promised otherwise. Now
   `Major`: runs on 8.x when present, else the next major that is. This is
   the one change that alters `ui/runtime/*.runtimeconfig.json`.
2. **A refused edit left the pane gating on the refused value.**
   `OnFieldEdited` writes the new value into `_live` so `showIf` can follow
   the edit before the ack; on refusal the field snapped back but `_live` did
   not, so visibility (and the device list, which reads `_live["capture"]`)
   followed a value the engine never held until the next echo. `ApplyAck`
   now restores `_live` from the snapped-back field.
3. **Two fields refused in one patch both showed the first message.**
   `settings._coerce` writes every refusal as `<label>: <why>`, so
   `ApplyAck` now picks the message whose prefix is the field's own label,
   falling back to the first.
4. **A question toast could be evicted by a burst of errors.** `StatusVm`
   holds four toasts and evicted oldest-first, so the "backend has nowhere to
   go" question (`ToastLife.Never`, which by its own remarks must not vanish)
   was pushed off by four `[error]` log lines — exactly when it was being
   asked. Eviction now drops the oldest *expiring* toast first.
5. **The capture card complained after one second about a switch still under
   way, then never took it back.** Starting a stream flips `capture` to
   `browser`; a running pipeline applies that at the top of its next worker
   iteration, up to one inference later, and every block pushed meanwhile is
   refused. At 8 refused blocks (1.0 s) the card said "the engine is not
   taking audio", and once blocks were accepted again the text stayed. Now
   40 blocks (~5 s), and the state text is restored when audio is accepted.
6. **32-bit integer PCM was decoded as float.** `MicStreamer` inferred
   "float" from `BitsPerSample == 32`; a driver handing out 24-in-32 integer
   samples would have produced noise. Float is now read from the format's
   encoding (`IeeeFloat`, or the `WAVEFORMATEXTENSIBLE` sub-format), 32-bit
   integer PCM is decoded, and the refusal message names all three formats.
7. **The engine card's Stop button lit for a process Python refuses to kill.**
   `_engine_stop` declines by design to kill an image that is not a whisper
   server; the button was gated on a PID only, so pressing it on an
   "occupied" port could only produce an error line. Gated on `Ours` too.
8. Stale prose: `app.py`'s `_sync_panel_theme` docstring still called `model`
   "the one sanctioned exception" to invariant 13 (there are none since
   2026-08-31); README and CONTRIBUTING said ".NET 8 Desktop Runtime" where
   the true requirement is 8.0 or newer.

### Open — a decision for the owner, not a patch

- **`Pipeline.apply` drains inline when the session is stopped, on the
  dispatcher thread.** `SettingsVm.Flush` calls `IEngineBridge.ApplySettings`
  synchronously (it needs the ack), and `Pipeline.apply` calls
  `_drain_pending()` itself when nothing is running (invariant 19 relies on
  that for `output`). So with the session stopped, changing `backend`,
  `model` or `local_device` from the pane runs `_build_backend` — a CPU
  model load, seconds — on the WPF thread, and changing `capture`/`device`
  opens a device there. That is the stall invariant 12 exists to prevent; the
  browser never sees it because its `apply` runs on the socket thread. The
  same holds for `LoadPreset` (`_preset_load` → `apply`). Options: drain on a
  worker when `not alive()` and the rebuild set is non-empty (changes the
  synchronous guarantee invariant 19 leans on), or have the bridge answer the
  validation half synchronously and hand the rebuild to `_lifecycle`. Either
  is a Python-side design change; recorded in PENDING_WORK.
- **No unit tests exercise `StatusVm`, `AudioVm`, `TranscriptVm`,
  `MicStreamer` or `ConditionEvaluator`** beyond the startup self-test.
  `tests/test_panel.py` (19 tests) covers presets, the settings echo, the
  theme and the engine model picker. Items 2–5 above would each have been a
  one-assert test against the real `SettingsVm`/`StatusVm` through the
  existing `on_ui` harness; none is added here because they cannot be run on
  the machine this was done on.
- **The informational version embeds the git hash at build time**
  (`0.1.0+<commit>`), so the committed DLL always names the commit *before*
  the one that committed it. Harmless, but it means a rebuild is never
  byte-identical across commits — worth knowing before anyone chases it.

## What "done" looks like for this pass

- `.\build_ui.cmd` on Windows, then `tests.test_panel` and
  `tests.test_ui_stamp` green. The `ui/runtime` in this pass was published on
  Linux with `EnableWindowsTargeting`; its `deps.json` is byte-identical to
  the previous commit's and the DLL builds clean, but it has not been loaded
  by pythonnet on Windows. The Windows toolchain of 2026-08 additionally
  emitted WPF's `GeneratedInternalTypeHelper` into the assembly; the newer
  8.0.4xx toolchain does not, and nothing in this XAML needs it (every type
  is public, handlers attach through the generated `Connect` methods) — but
  a Windows rebuild settles it either way.
- `ui\tools\shot.py` for the Engine tab: the Stop button greyed on an
  occupied port, and the capture card's state text returning to "streaming
  …" after a Start.
