# Desktop panel (WPF) — work record

Status of the `ui/` front end. Branch `feature/ui-wpf`.

**The seven steps this file used to list as remaining are done.** The panel
has its five content tabs, is wired into `app.py` behind `--wpf`, and
degrades to "no desktop panel" when the runtime is missing. This file stays
because half of it was never a to-do list: the decisions and the measurements
that settled the arguments are recorded below so none of it has to be
re-derived.

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

### 6. Soak — harness shipped, four-hour run still owed on real hardware

`ui/tools/soak.py`: synthetic meter at 8 Hz plus transcript, state, log,
perf and engine events through the real bridge, with a Python thread
hammering `ApplySettings` (and its settings echo — `HydrateSettings` plus
`Refilter` over all 61 fields is the heaviest binding path the panel has).
RSS, handle count and managed heap sampled every 30 s from the CLR itself;
the verdict compares the last quarter against the second. The 32,000-round-
trip boundary soak already showed RSS plateauing after round 3 and flat
handles, so a pass is expected — but if the full run fails, the View layer
moves behind a WebSocket client and everything after step 1 changes. Run it
on Windows: `.venv\Scripts\python.exe ui\tools\soak.py` (or `--minutes 5`
as the smoke version).

### 7. Packaging and docs — done

README gained the Desktop panel section and the folder map; CONTRIBUTING
gained the `build_ui.cmd`-then-commit-`ui/runtime` discipline and the bridge
threading rules as invariants 11–13; `requirements.txt` gained
`pythonnet>=3.1.0`.

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

**`REMOTE_LOCKED` does not apply here.** The web_* settings are stripped from
browser patches because that rule is enforced by transport, not by locality.
This panel is local by construction, so `ApplySettings` passes `remote=False`
and they stay editable — a desktop panel forbidden from configuring the
listener would be obeying a rule written for a different threat. (`wpf`
itself joined REMOTE_LOCKED for the mirror-image reason: whether a window
opens on the host machine belongs to whoever is sitting at it.)

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

---

## Open questions

1. **Committed `ui/runtime` vs a fetch script.** Currently committed. Costs
   ~6.5 MB in history per WPF-UI version, permanently. The alternative — a
   `fetch_wpfui.cmd` following the `_models/` precedent — costs every cloner
   the .NET SDK and a network restore.
2. **Light theme.** `CharcoalLight.xaml` and `PanelLight.xaml` are generated
   and written respectively, and `wpf_panel.Panel(dark=False)` merges them —
   but the light pair has never been rendered or reviewed, and nothing
   exposes the switch yet. The web panel has a light palette; whether the
   desktop panel needs one is undecided.
3. **Standalone executable.** The same assembly could ship a `Main` that
   speaks the WebSocket protocol, giving a panel that runs on a different
   machine from the engine. Deliberately cut from v1 — it is a complete second
   client (17 inbound message types, 16 commands, auth, reconnect), which is
   three days, not the one it looks like.
4. **The four-hour soak on real hardware.** The harness is in
   `ui/tools/soak.py`; the run itself needs a Windows desktop session and has
   not happened yet. Until it has, treat any large new retained-state feature
   in the panel as unproven against leaks.
