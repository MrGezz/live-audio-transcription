# Desktop panel (WPF) — work record

Status of the `ui/` front end. Branch `feature/ui-wpf`.

**Everything the plan listed is shipped** — the ten steps below, the last
three from the first day on real hardware (2026-08-23). What is still to do
sits here at the top under its own heading so it cannot be mistaken for the
record. The record stays because half of this file was
never a to-do list: the decisions and the measurements that settled the
arguments are written down so none of it has to be re-derived.

---

## Remaining work

**Nothing open.** The five items this section has carried — four on 2026-08-23
morning, one opened that evening while fixing `REMOTE_LOCKED` — are all closed:
four done, one decided. Step 10 records the first four. The last was the
untrusted-read-path gap, closed the same evening by `safe_paths.py` and pinned
by `tests/test_safe_paths.py`. Anything new goes here first, with what "done"
looks like, before it goes anywhere else in this file.

---

## What this is

A desktop control panel that calls the transcription engine **directly**,
alongside the existing browser panel rather than replacing it.

```
<ui:Button Command="{Binding RestartCommand}" />   compiled XAML
  -> RelayCommand                                  ui/Bridge/RelayCommand.cs
  -> IEngineBridge.Restart()                       a CLR symbol
  -> EngineBridge.Restart                          wpf_panel.py, a Python method
  -> App._lifecycle("restart", ...)                the same code the socket calls
```

No command-name string, no socket, no port, no token, no dispatch table.
Rename `Restart` and the build breaks on both sides rather than at runtime.

`--web` and `--wpf` are independent. Both can be open at once — `_lifecycle`'s
single busy slot already makes that safe. The browser panel keeps *remote*
audio streaming (a phone's microphone into this machine's engine); the
Engine tab's capture card is the local equivalent, pushing this machine's
microphone or system audio through `IEngineBridge.PushAudioChunk` into the
same `Pipeline.feed` the socket uses.

---

## Why the View layer is C# and not Python

This was the decision the whole architecture turned on, and it was settled by
measurement rather than preference.

**WPF cannot data-bind to a Python object.** pythonnet projects a Python class
as a CLR type with zero properties:

```
.NET sees the Python object as: BindProbe.PyVm
CLR properties on it:          []
TypeDescriptor properties:     []
{Binding Status}  ->  ''        silently — no error, no exception
```

A control bound to a known-good CLR source in the same harness rendered
correctly, so that is pythonnet, not the test. Since the entire settings pane
is generated from `settings.schema_json()` by binding, the ViewModels have to
be C#. Python implements one narrow interface — `IEngineBridge` — and the
XAML binds to C# only.

Runtime-loaded XAML (`XamlReader.Load` with Python handlers) *does* work, and
was proven end to end: Python functions as `Click` handlers, `x:Name` lookup,
WPF-UI controls fully templated. It was rejected because it forbids `x:Class`,
forbids `Command=`/`Click=` in markup, has no designer preview, and cannot
bind — which is worse markup/logic separation than `webui/app.js` has today.

---

## What shipped, step by step

The estimates this file used to carry (17–23 days for the remainder) are kept
here only as the record of what each step was scoped as.

### 1. Settings pane — done

`ui/Views/FieldTemplates.xaml` holds the seven control templates plus the
shared row chrome; `FieldTemplateSelector` maps `kind` -> template and throws
on an unmapped one; `SettingsVm.KnownKinds` refuses it at startup too, so an
eighth kind fails loudly twice instead of rendering as a TextBox that writes
the wrong JSON type. The pane is 8 groups over 61 fields (60 plus the new
`wpf` toggle itself) and nothing in the XAML names a setting.

- Every numeric field in the live schema declares both bounds (verified),
  so int and float render as slider + NumberBox with no fallback shape.
- `path` gets a Browse button — `OpenFileDialog`, except `output`, which gets
  a save dialog because the file it names usually does not exist yet.
  `color` gets a swatch-popup picker over a hex box; free text still goes
  through `settings._coerce`, which is where "#zz" actually gets refused.
- The 26 `showIf` conditions evaluate by `JsonValueKind` in
  `ConditionEvaluator` (the naive `ToString()` comparison hides 12 of them
  forever), and `SettingsVm.LoadSchema` now runs the startup self-test its
  remarks promised.
- The advanced filter hides the 19 advanced fields; the search box filters
  by key, label, help and CLI flag and auto-expands matching groups.
- `REMOTE_LOCKED` fields stay **editable** here and carry a "local only"
  chip explaining why the same row is dead in the browser.
- Devices and languages fill dynamically, with the `(not available)` entry
  keeping a vanished device selected instead of lying about the config.
- Edits debounce 250 ms into ONE patch (cross-field validation needs
  buffer+slide together), and a refused key snaps back via the
  absence-from-`changed` test.
- `model` is the one key special-cased by name (a picker over `_models`),
  matching webui/app.js:381 — the two panels must agree what that field IS.

### 2. Transcript — done

`ui/Views/TranscriptTab.xaml`. The ItemsControl carries an explicit
`VirtualizingStackPanel` with recycling and an item-scrolling ScrollViewer —
its default panel is a plain StackPanel that realises all ~12,000 visual
elements of a 600-line transcript. Per-word confidence renders as `Run`s via
the `WordInlines` attached property (Inlines is not a dependency property, so
it cannot be bound), a times column shows wall clock + session clock,
language tags colour by backend, and autoscroll follows the two-flag contract
in `TranscriptVm`: what the user asked for, and where the viewport actually
is, so a new caption never yanks a reader back to the bottom.

### 3. Meters, pills, toasts, banner — done

`StatusVm` pulls meter frames on a 30 Hz `DispatcherTimer` from a field the
8 Hz `OfferMeter` parks under a lock — no `PropertyChanged` per frame, and
`Panel.event` routes `meter` straight to it, skipping the dispatcher
entirely. Four pills (backend / source / xRT / language), the log unread
badge that only counts warnings you have not looked at, the offline dim, and
the toast stack with the 4.5 s / 9 s / 30 s / never lifetimes — hovering
holds a toast open, because a question that expires has been answered "no".
The five-state run banner (busy / not running / capture dead / no backend /
hidden) lives in `MainVm.RefreshRunBanner` with its fixes as buttons inside
it. These are four DISTINCT "something is wrong" channels, and keeping them
distinct is most of the value.

The three interactive toasts: slower-than-real-time offers the smaller
models from `_models` and restarts the server with the picked one;
auto-detect wandering offers to pin the commonest language; a pinned server
backend with nothing answering offers Start-the-server / use-the-CPU.

### 4. Engine and benchmark tabs — done

`EngineTab` has the session card, the whisper-server card (three port states
— answering / held-but-silent / free — because "not running" sends people to
press Start against a taken port), the model picker with the launcher-default
first entry, and the local capture card. `BenchmarkTab` runs the measurement
through the bridge on the LIVE backend, streams rows in as events, labels
synthetic runs as the encoder-floor lower bound they are, and applies
strategy+buffer+slide as the one patch the validator will accept.

### 5. `app.py` integration — done

The predicted 40–80 line diff, roughly as predicted. The nine
`self.server is not None` guard sites collapsed into `_has_front_end()` and
`_notify(kind, data)` — the two-subscriber fan-out. The two that mattered
most: `_main_loop`'s exit condition and `_finished()` now count the panel,
so Stop with only the WPF window attached no longer tears the process down
under it; and `_broadcast_engine` no longer early-outs without a server,
which is what makes busy transitions observable to a WPF-only front end at
all. Also: panel construction in `run()` behind the new `wpf` Field,
`_request_exit` asks the panel to close **without joining** (the Exit button
runs ON the panel's dispatcher; `shutdown()` on the main thread does the
joining close), `_export_payload` lifted out of `_export` for both front
ends, launcher wiring (`run_pipeline.cmd` option [5], `--wpf`), and the
graceful-degrade path: missing pythonnet or Desktop runtime prints one
actionable line and transcription carries on.

### 6. Soak — run on real hardware 2026-08-23

`ui/tools/soak.py`: synthetic meter at 8 Hz plus transcript, state, log,
perf and engine events through the real bridge, with a Python thread
hammering `ApplySettings` (and its settings echo — `HydrateSettings` plus
`Refilter` over all 61 fields is the heaviest binding path the panel has).
Private bytes, handle count, working set and managed heap sampled every 30 s
from the CLR itself; the verdict compares the last quarter against the
second. If the full run fails, the View layer moves behind a WebSocket
client and everything after step 1 changes. Run it on Windows:
`.venv\Scripts\python.exe ui\tools\soak.py` (90 minutes; `--minutes 240`
for the finer release-gate floor, `--minutes 5` as the smoke version). Exit
codes: 0 judged and passed, 1 judged and failed, 2 not judged — a short run,
an abort, or a harness that could not prove it did what it claims.

**What the first real run taught the harness.** The original version was
weaker than its docstring in two ways that a PASS would have hidden:

- It never left the Transcript tab. A `TabControl` builds only the selected
  tab's visuals, so the Settings pane's 61 rows, the Engine cards and the
  500 log rows were never instantiated and the settings churn reached no
  bindings at all. The harness now cycles `MainVm.SelectedTab` every 30 s.
- `panel.bridge.ApplySettings(...)` is Python calling the Python object.
  The path the panel actually uses — `FieldVm.Commit` → the 250 ms debounce
  → `SettingsVm.Flush` → `IEngineBridge.ApplySettings`, which pythonnet
  dispatches INTO Python on the dispatcher thread and marshals the ack back
  — never ran. The harness now drives `FieldVm` setters on the dispatcher at
  2 Hz (wider than the debounce, so every edit flushes), counts arrivals by
  thread, prints the split, and refuses a verdict if the C# side never called
  in. A 60 s sanity run: 384 hammer arrivals, 77 from C#.
- Smaller: `status()` was constant, so every state event short-circuited
  `Set()` and the pill/banner code never ran (it varies per tick now, with
  the capture "dying" for one event a minute); Ctrl+C produced a verdict
  from a partial run; 'too short' exited 0, the same as PASS.

**Results.** Machine: Windows 11 Pro 26200, .NET Desktop 8.0.30, Python
3.12 — pythonnet had to be installed into `.venv` first (requirements.txt
lists it; the venv predated that line). Five-minute smokes of both harness
versions passed (RSS +0.5 %, handles flat, 764 → 759). An old-harness
four-hour run was stopped at 17 minutes once the coverage gaps above were
proven (flat at ~254 MB / 750 handles to that point). The upgraded-harness
four-hour run started 01:18 on 2026-08-23 against the published
`ui/runtime` (all tabs realised: ~286 MB / ~752 handles at 17 minutes).

**That run was thrown away, and finding out why changed the harness twice.**
It ended at 29 minutes without a verdict. Two things came out of the post
mortem, neither of them a leak:

- *It had been invalid since minute 21.* `ui/runtime/LiveTranscription.Ui.dll`
  was republished at 01:39:14 while that very file was mapped into the
  running process. A .NET assembly is memory-mapped and paged in lazily, so
  everything the CLR touched for the first time after 01:39 came from the
  new build: the last third of the run was a mix of two DLLs. Never publish
  into `ui/runtime` while a soak holds it — publish to a scratch directory
  and point `shot.py --runtime` there, which is what that flag is for.
- *The metric was wrong.* The verdict judged `WorkingSet64`. The working set
  is the OS's decision about how much of a process's commit stays resident,
  not how much it owns: measured on this machine, 213 MB resident against
  375 MB private for the same window. The run watched it fall 293 → 170 MB
  across a single 30 s sample with allocation flat. A trim landing in the
  last quarter subtracts from exactly the number being watched — it turns a
  leak into a PASS, the one direction of error a leak soak must not have.
  The verdict now judges `PrivateMemorySize64` and prints both columns.
  (The suspected cause, a minimised window, was tested and **refuted**:
  minimising moves the working set by under 1 MB on Windows 11. The trim
  was machine-wide memory pressure, which is precisely why the soak must
  not depend on it.)

  How it ended is more mundane: empty stderr, no exception, and a clean
  Python epilogue, so the window took an orderly external close
  (`_window.Closed` → `_app.Shutdown()`) rather than crashing. Nothing in
  the harness closes it before the deadline. A window sitting on someone's
  desktop for four hours is a thing that gets closed — which is its own
  argument for the run below.

**Four hours was never justified; 90 minutes is.** The soak's power against
the leak class that actually threatens this design — per-event, a handler
never unhooked or a container never released — comes from event *count*, and
the rates are fixed. The aborted run bounded that class hard on its own:
13,939 `ApplySettings` round trips, each rehydrating all 61 `FieldVm`s,
moved the handle count by 8. That is under 0.001 handles per event, three
orders of magnitude below anything that matters, and four hours only
multiplies the evidence by 2.7. Duration buys sensitivity only against
time-based drift, at a knowable exchange rate: the two compared windows sit
T/2 apart and the threshold is 10 % of a ~275 MB baseline, so the smallest
detectable drift is about **54/T MB per hour** with T in hours — 13.5 MB/h
at four hours, 36 MB/h at 90 minutes. At the 90-minute floor a panel open
for an eight-hour workday grows ~290 MB and gets caught; the extra 2.5 hours
only buys the band below that, drift no one would notice. So the default is
now 90 minutes, with `--minutes 240` kept for a release gate.

**The run that counts.** 90 minutes against the final `ui/runtime` (theme
build included), harness judging private bytes, nothing else touching the
DLL. It vindicated the metric change while it ran: at 4,511 s the working
set fell 260 → 163 MB, a 37 % drop, while private bytes did not move
(471.2 → 471.4 MB). Under the old verdict that trim lands squarely in the
judged last quarter and subtracts ~97 MB from the number being watched.
Verdict, 2026-08-23 09:33: **PASS** — private bytes 467.2 → 473.7 MB
(+1.4 %, against a 10 % threshold) and handles 757 → 675, i.e. *down* 82.
Over the run the bridge carried 42,120 `ApplySettings` round trips (34,715
from the Python hammer, 7,405 the CLR → Python direction), each rehydrating
all 61 `FieldVm`s, with every tab realised and cycled. The binding layer
does not leak, and the View layer stays in C#: the WebSocket-client fallback
in step 1 is not needed.

### 7. Packaging and docs — done

README gained the Desktop panel section and the folder map; CONTRIBUTING
gained the `build_ui.cmd`-then-commit-`ui/runtime` discipline and the bridge
threading rules as invariants 11–13; `requirements.txt` gained
`pythonnet>=3.1.0`.

### 8. First render on real hardware (2026-08-23) — what `shot.py` found

`shot.py` passes (2.7 s, a 2× PNG). Rendering every tab with a populated
hello — `shot.py` captures only the Transcript tab with an empty stub, which
is how the first two items survived until now — found four things:

- **The Engine tab was blank.** `ui/Views/EngineTab.xaml.cs` did not exist.
  A XAML file with an `x:Class` still gets its `InitializeComponent()`
  generated and its BAML compiled, but nothing CALLS it, so the control
  built, instantiated and rendered as an empty `UserControl` with no error
  anywhere (two visual descendants against Benchmark's 217). Fixed with the
  one-line code-behind every other view has.
- **Every accent-derived brush was frozen transparent.** `gen_theme.py`
  walked the WPF-UI dictionaries standalone, where a `Color` set with
  `{DynamicResource SystemAccentColor…}` is an unresolved expression that
  reads as `#00FFFFFF`, and the name filter (`"Accent" in key`) only ever
  saw the three keys with Accent in the name. 36 others — `ToggleSwitchFillOn`,
  `SliderThumbBackground`, `TextControlFocusedBorderBrush`,
  `CheckBoxCheckBackgroundFillChecked`, `ProgressBarForeground`… — shipped
  as transparent, so an ON toggle was a bare knob in both themes. The
  generator now skips any brush whose `ReadLocalValue(ColorProperty)` is an
  expression (39 keys, detected by what the value IS) and leaves them to the
  runtime; 22 brushes that WPF-UI itself defines as transparent stay.
- **WPF-UI brightens the accent it is given.** The `(colour, theme)`
  overload derived Primary `#4DEBFF` / Secondary `#73EFFF` from `#00BCD4`,
  so primary buttons were a cyan the browser panel never shows, and the
  light theme was handed the dark accent. `PanelHost.ApplyAccent` now reads
  `PanelAccentBrush` / `PanelAccent2Brush` from the merged theme dictionary
  and hands them to the four-colour `Apply` verbatim: Primary (toggle ON,
  thumb, focus) and Secondary (accent button at rest) are `--accent`,
  Tertiary (its hover) is `--accent-2`, per theme. Pixel-checked:
  `#00BCD4` dark, `#0097A7` light.
- **The Benchmark table header was a white band** in both themes: the stock
  `GridView` header template is classic-white and no theme dictionary
  touches it. `ui:GridView` / `ui:GridViewColumn` (WPF-UI's own) fix it.

Also: the light pair (`CharcoalLight.xaml` + `PanelLight.xaml`) rendered for
the first time and holds up, once its pin table was made the same 45 keys as
the dark one token-for-token (`--ok`/`--warn` hues, `--bg-2`, eight keys that
had fallen through to interpolation); the generator now fails if the two
files' key sets differ. The unread-log badge ink became `PanelDangerInkBrush`
(dark `#0B1013`, light `#FFFFFF` — the light danger fill is darker, so the
winning ink flips). `build_ui.cmd` had no SDK pin: the machine's newest SDK
(10.x) rewrites `deps.json` and `runtimeconfig.json` against the committed
output; an 8.0 SDK reproduces it byte-for-byte. The publishes above were
done with a temporary `global.json` pinning 8.0; the permanent one landed on
2026-08-23 as `9a05aa0` (repo root, `8.0.100` with `rollForward:
latestFeature`, so any installed 8.0.x is accepted) and is verified: with
8.0.424 resolved, `deps.json` and `runtimeconfig.json` came back
byte-identical to HEAD and `LiveTranscription.Ui.dll` was the only diff.

### 9. VAD profiles reach the panels (2026-08-23)

`run_pipeline.cmd` has always offered three named speech-detection choices —
`[1] Speech`, `[2] Music` (`--vad-min-speech-ms 60`), `[3] Off` (`--no-vad`).
Neither panel did. Both expose the Speech gate as seven raw knobs, so
"Music" meant knowing that the magic number is 60, which is knowledge that
lived only in `run_pipeline.cmd` and invariant 10. The panels now ship the
same three choices as preset files in `presets/builtin/`.

No new UI, and deliberately no `vad_profile` field: invariant 13 forbids
special-casing a settings key by name in either panel, and a profile field
would also have become a lie the moment someone hand-edited the underlying
knob. A preset is honestly a *starting point you then tweak*, which is what
the launcher's CLI args already are.

Four things had to be true, and three of them were not:

- **Presets apply as a patch.** `_preset_load` hands the file to
  `pipeline.apply`, which writes only the keys present, so a delta-only file
  leaks the previous profile's state: a Music of `{vad_min_speech_ms: 60}`
  cannot switch the gate back on after Off, and an Off that leaves `60`
  behind arms Music invisibly the next time Silero is re-ticked by hand,
  because `show_if` hides that field while the gate is off. Every file now
  states the full union `{vad, vad_min_speech_ms}` — and *only* that union,
  because pinning `vad_threshold` or the silence knobs would stomp a user's
  own tuning on every switch. All 24 orderings (6 permutations × repeat ×
  clean and hand-tuned starts) were run through `validate` plus the real
  `changed` filter: no errors, final state a pure function of the last
  profile loaded, user tuning untouched. All six transitions raise a `gate`
  rebuild.
- **`save_preset` cannot produce these files.** It stores only *non-default*
  values, so a "Speech" saved from the UI trims to `{}` — and an empty
  preset is a silent no-op that still returns `{"loaded": ...}` and toasts
  success in both panels. They are hand-authored, and invariant 14 now says
  so, because deleting the "redundant" defaults is the obvious tidy-up and
  it is the bug.
- **The desktop panel emptied its own preset dropdown on load.**
  `SettingsVm.SetPresets` refills the list from the ack and its own comment
  names `"presets"` as the contract, but `_preset_load` was the one path
  that never returned it — so the first profile switch cleared the list it
  was picked from. Fixed at all three layers, because each had let it
  through: `app.py` sends the list on every return of save/load/delete,
  error shapes included; the bridge's `_preset_ack` adds it to any ack that
  arrives without it; and `SetPresets` returns before `Clear()` unless it
  was actually handed an array. The C# half waited for the soak to release
  the DLL (step 6), and went out with the pinned rebuild.
- **The desktop panel toasted "Preset loaded" whatever came back.** A
  refused name, a missing file, or a field `Pipeline.apply` rejected all
  read as success. `PresetAck` now reads both of `app.py`'s refusal shapes
  (`error` for the whole call; `errors` per field, with the rest applied),
  joins them into one toast, tests success positively on the key naming
  what was done, and shows the name `_safe_preset_name` actually used.
  Neither shipped tool could see this bug; `tests/test_panel.py` (step 10)
  does.
- **The browser's preset placeholder had no value.** `renderPresets` rebuilt
  it with a helper that sets `textContent` only, so its value became its own
  label and picking it would have asked the engine for a preset called
  `Presets…`. Harmless while the list was empty; a daily path once three
  profiles are in it.

Shipped profiles live in `presets/builtin/` rather than `presets/` for two
reasons. `.gitignore` ignores `presets/*.json` as personal setups, and a
gitignore `*` never crosses a `/` — so one level down is tracked with no
negation rule to maintain. And it makes "shipped" structural instead of a
blessed-name list in code: Save always writes to `presets/`, so saving over
a built-in's name *shadows* it instead of destroying a tracked file, and
deleting that shadow restores the original. Verified end to end, including
that deleting a pure built-in is refused.

Switching profiles is also how two dead knobs surfaced. The settings pane
echoed `vad_neg_threshold` and `vad_min_silence_ms` faithfully — validated,
logged, rebuilt the gate — and neither reached `SpeechGate.create()` or
`configure()`; the gate ran `min_silence_ms` 160 while both panels showed
400. That is Python-side work and is recorded where it belongs: CONTRIBUTING
invariants 10 and 15, the `speech_gate.py` docstring, and `tests/`.

### 10. The remaining-work list, closed (2026-08-23)

**A test that asserts what the panel did.** `tests/test_panel.py`, seven
cases. It opens the real window from the published `ui/runtime`, binds
app.py's own handlers — `_preset_save/_load/_delete`, `_on_event`,
`_sync_panel_theme` — onto soak.py's engine stand-in so production Python
sits under the real `SettingsVm`, and reads the toasts and the theme back
off the dispatcher. Loading a built-in toasts Success with its name and
leaves the dropdown populated; a missing name is one Error toast; a file
the validator refuses is one Error toast and no Success; deleting a
built-in is refused while deleting a saved copy over it restores the
original; an ack without a `presets` array leaves the list alone. Skips
without pythonnet or the Desktop Runtime, as `test_real_model.py` skips
without the model: `.venv\Scripts\python.exe -m unittest tests.test_panel
-v`, the window on screen for about a second. The suite is 54 tests with
everything present.

**The theme is a setting, applied live.** `wpf_theme` (`dark` | `light`,
`--wpf-theme`) in the *Control panels* group, REMOTE_LOCKED like `wpf` and
for the same reason — what the host's window looks like belongs to whoever
is sitting at it. A `Field` rather than a button in the window, so it
persists with the rest and appears in both panels with no UI edit
(invariant 13); and the same invariant decides WHERE it is applied:
`app.py`'s `_sync_panel_theme`, beside `_sync_overlay`, tells the panel
"dark" or "light" through `Panel.theme()` — C# never sees the key.
`PanelHost.ApplyTheme` then swaps WPF-UI's theme, the Charcoal pair and the
Panel pair in place and re-runs the accent tiers; the views reach every
brush by DynamicResource, so the open window restyles without being
rebuilt. One trap, below: the backdrop manager writes a local Background
over MainWindow's resource reference on a live switch.

**Theme fidelity beyond colour.** Each of the five audited deviations is
now matched to `webui/style.css`, or recorded as deliberate.
`ControlCornerRadius` is 8 (`--radius-sm`; one key in Panel.xaml, which
every WPF-UI control style reads dynamically) and cards, the banner and the
toasts are 12 (`--radius`). Pills carry `.pill`'s fill (the panel colour at
60 %) and a border in the tone's hue (45 %, danger 50 %) instead of ink
alone. The level meter is the `--ok` → `--warn` gradient and the speech
meter `--accent` → `--violet`, across the fill as the CSS paints it. The
scrollbar thumb is `--sb-thumb` — the accent at 40 % over `--panel-2` dark,
45 % over `--line` light — pinned in `gen_theme.py`. The transcript tag is
radius 6 with the 1 px edge and the backend's tint. Deliberate: a tag's edge
uses the pill alpha (45 %) where the CSS says 35 %, one key per hue; and
the thumb's hover keeps WPF-UI's opacity step rather than going to full
accent. Rendered in both themes with `shot.py`, every tab.

**Light `--bg-2`: settled, not fixed here.** The panel follows the website
and `webui/style.css` (`#FFFFFF`); the guideline's table (`#E7EDF0`) is a
document outside this repository and is its own to correct. `gen_theme.py`
says at the pin which one it mirrors and why.

---

## Decisions already made

**WPF-UI comes from NuGet.** `<PackageReference Include="WPF-UI" Version="4.3.0" />`.
Never a `ProjectReference` to a local wpfui checkout: every path in this
project stays relative to the repo root, and a local clone is liable to sit
some commits past its tag — silent version drift.

**`ui/runtime` is committed.** ~6.5 MB of publish output, so a fresh clone runs
the panel with only the .NET Desktop runtime — no SDK, no restore, no network.
This cuts against the repo's habit of gitignoring binaries (`_models/*`,
`_whisper.cpp/`); the alternative costs every cloner the SDK. Re-run
`build_ui.cmd` and commit the output after any `ui/` change. Nothing enforces
that but discipline (now written down as CONTRIBUTING invariant 11).

**The theme is generated, not written.** See below. The panel's OWN semantic
keys (confidence bands, pill tones, meter tracks) live in the hand-written
`ui/Themes/Panel.xaml` / `PanelLight.xaml`, merged after the generated pair —
they exist in no WPF-UI dictionary, so there is nothing to generate them from.

**Glassmorphism is not being ported.** `webui/style.css` uses per-surface
`backdrop-filter: blur(24px)`, which blurs *the app's own content*. WPF has no
primitive for that. `WindowBackdropType.Acrylic` is a DWM *system* backdrop —
it blurs the desktop behind the window — and setting an opaque
`ApplicationBackgroundBrush` (which is mandatory, or the window renders white
on Windows 10 and over RDP) defeats it entirely. The agreed target is
charcoal + cyan + Fluent depth. Ambient glows, if wanted, are `Ellipse` +
`RadialGradientBrush` — never `BlurEffect` at a large radius.

**`REMOTE_LOCKED` does not apply here.** Those settings are stripped from
browser patches because that rule is enforced by transport, not by locality.
This panel is local by construction, so `ApplySettings` passes `remote=False`
and they stay editable — a desktop panel forbidden from configuring the
listener would be obeying a rule written for a different threat. (`wpf`
itself joined REMOTE_LOCKED for the mirror-image reason: whether a window
opens on the host machine belongs to whoever is sitting at it.)

> Written when the list was the five `web_*` keys. It has since grown twice —
> `wpf` / `wpf_theme`, then `model` / `vad_model` / `output` on 2026-08-23 —
> which is why nothing here quotes a count any more. The reasoning above is
> unchanged: everything on the list stays editable in this panel.

**Closing the window is not exiting the program.** The window closing is the
front end going away — exactly a closed browser tab — and the engine keeps
whatever it was doing; `PanelHost` hooks `Closed` to end the message loop so
`Panel.alive` tells the truth. The panel's power button is the one that calls
`RequestExit`.

---

## Things that cost time to find

Kept because each one is invisible until it bites.

**Colors cannot be overridden, only brushes.** WPF-UI's `Dark.xaml` defines 88
`Color` keys and 361 `SolidColorBrush` keys, and the brushes bind their colour
with `{StaticResource}` — resolved at *parse* time. Redefining the Colors in a
dictionary merged afterwards changes nothing at all. That is why
`ui/Themes/Charcoal.xaml` restates every brush and why it is generated.

**Accent brushes must be left alone.** The first generated theme froze
`AccentButtonBackground` to `#00FFFFFF` — fully transparent — because it is a
`DynamicResource` onto a key that only exists once
`ApplicationAccentColorManager` has run. The primary button rendered as an
empty outline that looked disabled. Anything matching `Accent` is now excluded
from the generated file entirely.

**A `Library` gets no `runtimeconfig.json`.** Only executables do. pythonnet
needs one, and it must name `Microsoft.WindowsDesktop.App` —
`clr_loader`'s auto-generated config names only `Microsoft.NETCore.App`, which
contains no WPF, and the failure is `FileNotFoundException` on
`PresentationFramework`. Fixed with
`<GenerateRuntimeConfigurationFiles>true</GenerateRuntimeConfigurationFiles>`.

**`Application.ResourceAssembly` must be set before any component loads.** The
generated `InitializeComponent` uses a *relative* pack URI, which resolves
against `ResourceAssembly` — and a library hosted inside `python.exe` has no
entry assembly to default to. Miss it and you get a missing-resource
`IOException`, or an untemplated raw Win32 frame with no exception at all.

**pythonnet mints a real CLR type per class statement.** Defining the bridge
class twice raises *Duplicate type name within an assembly*, and there is no
way to unregister one. `_bridge_class()` caches.

**Every WPF object is thread-affine.** Reading `win.Title` from Python's main
thread throws `InvalidOperationException`. Nothing in `wpf_panel.py` touches
the window directly; state goes in through `PanelHost.Post*`, which marshals
with `BeginInvoke`. Never a blocking `Dispatcher.Invoke` from a bridge method —
that, against a UI thread waiting on the GIL, is the one deadlock this design
can produce. The same rule wearing another hat: `Panel.close()` joins the WPF
thread, so it may never be called FROM a bridge method — `request_close()`
exists for the Exit button's path.

**The CLR is apartment-neutral; `sounddevice` is not.** Importing
`sounddevice` takes the main thread to `MAIN_STA` before any CLR exists.
Hosting the CLR afterwards changes nothing — verified in both import orders,
with loopback enumeration (the `soundcard` COM canary, `audio_sources.py:63`)
returning the same 2 devices and inputs the same 9 either way. The panel gets
its own `.NET`-created STA thread; the main thread is never touched, because
`overlay.py`'s Tk mainloop owns it.

**An ItemsControl does not virtualize.** Its default panel is a plain
StackPanel and its default template has no ScrollViewer; both must be
supplied, plus `ScrollViewer.CanContentScroll="True"`, or a long transcript
realises every row it will ever show. `VirtualizationMode="Recycling"`
containers get handed a *different* line, which is why `WordInlines` rebuilds
on every attached-property change rather than only on first render.

**Popup content is not in the visual tree you think.** Walking visual parents
from inside a Popup ends at an internal `PopupRoot` whose parent is nothing;
the `Popup` itself is only reachable as the *logical* `Parent` of its child.
The colour swatch's close-after-pick walks visual parents while checking
`fe.Parent is Popup` at each step for exactly this reason.

**A method group with a return value is not an `Action`.** C# lambdas may
discard a result; method group conversions may not (CS0407). `MainVm.Toast`
returns the `ToastVm` (the interactive toasts need it back), so the sub-VMs
receive a wrapping local function, not the method group.

**`cmd` will not resolve a bare batch name.** `build_ui.cmd` must be invoked as
`.\build_ui.cmd`. Same trap as commit `9ac67be`.

**A view with `x:Class` and no code-behind renders empty, silently.** The
BAML compiles, `InitializeComponent()` is generated, and nothing calls it.
No exception, no binding error — two visual descendants where there should
be hundreds. Step 8 has the story.

**Accent-derived brushes are `DynamicResource` expressions.** In a dictionary
walked outside a running application, `.Color` on one reads `#00FFFFFF`.
Detect them with `ReadLocalValue(SolidColorBrush.ColorProperty)` — an
`Expression`, not a `Color` — never by the key's name: 36 of the 39 have no
"Accent" in them.

**`ApplicationAccentColorManager.Apply(colour, theme, …)` brightens.** It
derives Primary/Secondary/Tertiary from the colour (`#00BCD4` → `#4DEBFF`,
`#73EFFF`, `#A6F5FF`). The four-colour overload takes the tiers verbatim.
Secondary is the accent button's colour at rest, Tertiary its hover, Primary
everything else (toggle ON, slider thumb, focus border).

**A `TabControl` realises the selected tab only.** The other four
`UserControl`s exist as logical children — their top-level bindings are even
live — but nothing inside their item templates is instantiated. A soak or a
screenshot that never switches tabs is looking at one fifth of the View
layer.

**`panel.bridge` is the Python object.** Calling its methods from Python
never crosses the CLR boundary. Only a C#-side action — a `FieldVm` setter on
the dispatcher, a command — exercises pythonnet's reverse path, the GIL it
takes on the dispatcher thread, and `ApplyAck`.

**`PanelHost.Post*` queues at `DispatcherPriority.Background`.** A tool that
`BeginInvoke`s at the default priority jumps the queue: the first C#-side
edit in the soak ran before the hello had loaded the schema.

**The stock `GridView` header template is classic-white** and no theme
dictionary touches it. `ui:GridView` / `ui:GridViewColumn`.

**`dotnet` picks the newest installed SDK.** SDK 10 prunes framework-provided
packages from `deps.json` and swaps a `runtimeconfig` property; the committed
`ui/runtime` is an 8.0 build. `global.json` at the repo root pins the 8.0 band
(`latestFeature`); a `ui/runtime` diff that touches anything besides
`LiveTranscription.Ui.dll` means the pin was bypassed.

**A live theme switch loses the window's Background binding.**
`ApplicationThemeManager.Apply` re-applies the backdrop to the open window,
and on the way writes a local Background over MainWindow's
`{DynamicResource ApplicationBackgroundBrush}` — the opaque brush that keeps
the window from rendering white wherever DWM is off. `PanelHost.ApplyTheme`
calls `SetResourceReference(BackgroundProperty, ...)` after it; the panel
test asserts the window is opaque `#ECEFF1` after a switch, not transparent.

---

## Settled

Questions this file carried as open, and what closed them. Kept so the
reasoning is not re-derived; nothing here is pending anything.

- **`ui/runtime` is committed, not fetched.** ~6.5 MB in history per WPF-UI
  version, permanently, against costing every cloner the .NET SDK and a
  network restore. Committed wins until the history cost becomes the
  problem; the alternative, if it ever does, is a `fetch_wpfui.cmd` on the
  `_models/` precedent.
- **No standalone executable in v1.** The same assembly could ship a `Main`
  that speaks the WebSocket protocol, giving a panel that runs on a
  different machine from the engine. Cut deliberately — it is a complete
  second client (17 inbound message types, 16 commands, auth, reconnect),
  three days, not the one it looks like.
- **The leak soak: PASSED, at 90 minutes.** 2026-08-23 09:33 on real
  hardware: private bytes +1.4 %, handles down 82 over 42,120 bridge round
  trips, every tab realised and cycled (step 6 has the numbers, and the
  exchange rate — about `54/T` MB per hour of detectable drift, T in hours —
  that settled the duration). `--minutes 240` stays for a release gate.
  Re-run it before trusting any large new retained-state feature in the
  panel. Nothing bounds drift over a *multi-day* stretch, and nobody has
  needed it to yet; that becomes an item the day someone does.
- **`global.json` is in.** `9a05aa0`, `8.0.100` with `rollForward:
  latestFeature`; verified with 8.0.424 resolved (step 8). Invariant 11 says
  what a bypassed pin looks like.
- **The light theme renders, and is exposed.** Every tab, via
  `Panel(dark=False)`, coherent once its pins mirrored the dark ones key for
  key (step 8); `wpf_theme` switches the open window to it (step 10).
- **Light `--bg-2` follows the CSS.** `#FFFFFF`, as the website and
  `webui/style.css` say; the guideline's table (`#E7EDF0`) is a document
  outside this repository and is its own to correct. `gen_theme.py` says at
  the pin which one it mirrors.
