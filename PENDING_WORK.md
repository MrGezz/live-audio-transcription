# Desktop panel (WPF) — pending work

Status of the `ui/` front end as of the commit that adds this file.
Branch `feature/ui-wpf`.

The foundation is built and runs. The panel is a shell: it themes, it binds,
its buttons reach Python. None of the five content tabs exist yet. This file
is what remains, why the shape is what it is, and the measurements that
settled the arguments — so none of it has to be re-derived.

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
single busy slot already makes that safe. The browser panel keeps browser-mic
streaming, which has no WPF equivalent and is not being ported.

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

## What works now

| Piece | State |
| --- | --- |
| `ui/LiveTranscription.Ui.csproj` | .NET 8 WPF library, WPF-UI 4.3.0 from NuGet |
| `ui/Bridge/IEngineBridge.cs` | 34 methods — 12 typed lifecycle calls, the rest JSON documents |
| `ui/Bridge/RelayCommand.cs` | try/catch on Execute, explicit `RaiseCanExecuteChanged` |
| `ui/PanelHost.cs` | STA host, theme merge, async marshalling, unhandled-exception hook |
| `ui/ViewModels/MainVm.cs` | the busy gate every lifecycle button hangs off |
| `ui/Views/MainWindow.xaml` | shell: TitleBar, top actions, banner. **No tabs.** |
| `ui/Themes/Charcoal*.xaml` | 394 brushes × 2 themes, generated |
| `ui/tools/gen_theme.py` | regenerates the themes from the live dictionary |
| `ui/tools/shot.py` | renders the panel to a PNG for review |
| `wpf_panel.py` | the Python half: bridge implementation and host |
| `build_ui.cmd` | `dotnet publish` into `ui/runtime` |

Tests passing: boundary smoke 13/13, environment gate 11/11.

**Nothing is wired into `app.py` yet.** The panel currently runs only against
a stub engine. That is step 5 below.

### Running what exists

```
.\build_ui.cmd                                    once, needs the .NET 8 SDK
.venv\Scripts\python.exe ui\tools\gen_theme.py    only after a WPF-UI upgrade
.venv\Scripts\python.exe ui\tools\shot.py         renders panel.png
```

---

## Remaining work

Ordered. Each step is independently verifiable.

### 1. Settings pane — 4–5 days

The largest single chunk, and the reason the ViewModels are C#.

`settings.schema_json()` is 8 groups, 60 fields, 101 languages. Field kinds:
`bool` 14, `float` 18, `int` 11, `choice` 6, `str` 5, `path` 4, `color` 2.

- Seven `DataTemplate`s in one `FieldTemplates.xaml`, chosen by a
  `DataTemplateSelector` keyed on `kind`.
- `path` (4 fields) needs `OpenFileDialog`; `color` (2 fields) needs a picker.
- 26 `showIf` conditions with type-heterogeneous values — `{"vad":[true]}`,
  `{"capture":["file"]}`. Compare as `JsonElement` by kind. A naive
  `ToString()` comparison makes every boolean-gated field permanently
  invisible, which is 12 of the 26.
- The advanced filter (19 fields), `remoteLocked` (5 fields — but see the
  note below: this panel is local, so they should be *editable* here).
- Dynamic choices for devices and languages, with the
  `"<value>  (not available)"` fallback for a device that has gone away.
- Debounce on change, snap back on `ack.errors`.

Acceptance: adding a `Field(...)` to `settings.py` makes a control appear with
no C# or XAML edit. Add a startup assert that every `kind` in the live schema
maps to a template, so an eighth kind fails loudly instead of silently
rendering as a TextBox.

### 2. Transcript — 2.5 days

**Must be virtualized.** `ItemsControl` does not virtualize by default; a
600-line transcript with per-word confidence `Run`s is roughly 12,000 visual
elements. Needs an explicit `VirtualizingStackPanel` `ItemsPanelTemplate` plus
`VirtualizingPanel.IsVirtualizing`.

Per-word confidence colouring, times column, language tags, autoscroll with a
"stick to bottom unless scrolled up" rule.

### 3. Meters, pills, toasts — 3–4 days

- Level meter at 8 Hz. Do **not** push a `PropertyChanged` per frame — hold
  the value in a field and pull it on a 30 Hz `DispatcherTimer`.
- Four status pills, log unread badge, offline dim.
- Three interactive toasts with their own controls and 4.5 s / 9 s / 30 s /
  never lifetimes.
- The five-state run banner.

These are four *distinct* "something is wrong" channels and they are not
interchangeable — that distinction is most of the value.

### 4. Engine and benchmark tabs — 2.5 days

whisper-server card (state by port, PID, image, model picker, Start / Restart
/ Stop) and the benchmark runner.

### 5. `app.py` integration — 2.5–3 days

Realistic diff **40–80 lines**, not the dozen it looks like.

There are nine `self.server is not None` / `is None` guard sites — lines 148,
175, 186, 245, 280, 316, 564, 768, 786 — and every one is really asking *does
a front end exist / can it receive this*. Collapse into one
`_has_front_end()` and one `_notify(kind, data)` subscriber list.

Two matter more than the rest: if `_main_loop`'s exit condition and
`_finished()` are not amended, pressing Stop with no browser attached tears
the process down while the WPF window is still on screen — the same class of
bug commit `9ac67be` fixed for the overlay.

Also needed: panel construction in `run()`, a `_request_exit` arm calling
`Dispatcher.InvokeShutdown()`, a `wpf` bool `Field` in `settings.py`, launcher
wiring, and a graceful-degrade path when pythonnet or the Desktop runtime is
missing (`--wpf` failing must mean "no desktop panel", never "no
transcription").

`_broadcast_engine` currently returns early when `self.server is None`, so
with the WPF panel as the only front end **no busy transition is observable**.
That is why the subscriber list is required and not cosmetic.

One method the bridge already calls does not exist yet: `App._export_payload`.
The export logic is inline in `App._export` (app.py:476–506) and needs
lifting out so both front ends can share it.

### 6. Soak — 1 day

Four hours of synthetic meter (8 Hz) plus transcript, state and log through
the real bridge, with a Python thread hammering `ApplySettings`. Watch RSS,
handle count, managed heap.

Run this **before** the content tabs, not after. A 32,000-round-trip soak
already showed RSS plateauing after round 3 and flat handles, so it is
expected to pass — but if it fails, the View layer moves behind a WebSocket
client instead and everything after step 1 changes.

### 7. Packaging and docs — 1 day

README, CONTRIBUTING, SETUP notes. The `build_ui.cmd`-then-commit-`ui/runtime`
discipline. `requirements.txt` gains `pythonnet>=3.1.0`.

**Estimate for the remainder: 17–23 days.** Original full-parity estimate was
24–31; the foundation accounts for the difference.

---

## Decisions already made

**WPF-UI comes from NuGet.** `<PackageReference Include="WPF-UI" Version="4.3.0" />`.
Never a `ProjectReference` to a local wpfui checkout: every path in this
project stays relative to the repo root, and a local clone is liable to sit
some commits past its tag — silent version drift.

**`ui/runtime` is committed.** 6.4 MB of publish output, so a fresh clone runs
the panel with only the .NET Desktop runtime — no SDK, no restore, no network.
This cuts against the repo's habit of gitignoring binaries (`_models/*`,
`_whisper.cpp/`); the alternative costs every cloner the SDK. Re-run
`build_ui.cmd` and commit the output after any `ui/` change. Nothing enforces
that but discipline.

**The theme is generated, not written.** See below.

**Glassmorphism is not being ported.** `webui/style.css` uses per-surface
`backdrop-filter: blur(24px)`, which blurs *the app's own content*. WPF has no
primitive for that. `WindowBackdropType.Acrylic` is a DWM *system* backdrop —
it blurs the desktop behind the window — and setting an opaque
`ApplicationBackgroundBrush` (which is mandatory, or the window renders white
on Windows 10 and over RDP) defeats it entirely. The agreed target is
charcoal + cyan + Fluent depth. Ambient glows, if wanted, are `Ellipse` +
`RadialGradientBrush` — never `BlurEffect` at a large radius.

**`REMOTE_LOCKED` does not apply here.** The five `web_*` settings are
stripped from browser patches because that rule is enforced by transport, not
by locality. This panel is local by construction, so `ApplySettings` passes
`remote=False` and they stay editable — a desktop panel forbidden from
configuring the listener would be obeying a rule written for a different
threat.

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
can produce.

**The CLR is apartment-neutral; `sounddevice` is not.** Importing
`sounddevice` takes the main thread to `MAIN_STA` before any CLR exists.
Hosting the CLR afterwards changes nothing — verified in both import orders,
with loopback enumeration (the `soundcard` COM canary, `audio_sources.py:63`)
returning the same 2 devices and inputs the same 9 either way. The panel gets
its own `.NET`-created STA thread; the main thread is never touched, because
`overlay.py`'s Tk mainloop owns it.

**`cmd` will not resolve a bare batch name.** `build_ui.cmd` must be invoked as
`.\build_ui.cmd`. Same trap as commit `9ac67be`.

---

## Open questions

1. **Committed `ui/runtime` vs a fetch script.** Currently committed. Says
   6.4 MB in history per WPF-UI version, permanently. The alternative — a
   `fetch_wpfui.cmd` following the `_models/` precedent — costs every cloner
   the .NET SDK and a network restore.
2. **Light theme.** `CharcoalLight.xaml` is generated but has never been
   rendered or reviewed. The web panel has a light palette; whether the
   desktop panel needs one is undecided.
3. **Standalone executable.** The same assembly could ship a `Main` that
   speaks the WebSocket protocol, giving a panel that runs on a different
   machine from the engine. Deliberately cut from v1 — it is a complete second
   client (17 inbound message types, 16 commands, auth, reconnect), which is
   three days, not the one it looks like.
