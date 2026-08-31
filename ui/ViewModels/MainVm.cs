using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.Json;
using System.Windows.Threading;
using LiveTranscription.Ui.Bridge;
using LiveTranscription.Ui.Settings;

namespace LiveTranscription.Ui.ViewModels;

/// <summary>
/// The window's data context: the busy gate, the five tabs, and everything
/// the engine pushes in.
/// </summary>
/// <remarks>
/// The single-slot rule that <c>App._lifecycle</c> enforces is mirrored once
/// here rather than re-derived per button. Python refuses a second lifecycle
/// command while one is running and says so in the log; the panel greys the
/// buttons instead, so the refusal is visible before the click rather than
/// after it.
/// </remarks>
public sealed class MainVm : ViewModelBase
{
    private readonly IEngineBridge _bridge;
    private readonly Dispatcher _dispatcher;
    private readonly List<RelayCommand> _gated = new();
    private readonly Dictionary<string, int> _languagesSeen = new(StringComparer.Ordinal);

    private string _busy = "";
    private bool _running;
    private bool _paused;
    private string _status = "starting";
    private string _bannerTitle = "";
    private string _bannerMessage = "";
    private string _bannerSeverity = "Informational";
    private bool _bannerOpen;
    private int _selectedTab;
    private bool _modelHintShown;
    private bool _languageHintShown;
    private bool _serverOfferShown;
    private bool _hydrated;
    private bool _sourceAlive;
    private bool _hasBackend;
    private string _stateError = "";
    private string _sessionState = "stopped";
    private string _sessionDetail = "";
    private string _version = "";

    public MainVm(IEngineBridge bridge, Dispatcher dispatcher)
    {
        _bridge = bridge;
        _dispatcher = dispatcher;

        // A method group with a return value does not convert to an Action,
        // so the toast entry point the sub-VMs get is this thin wrapper; only
        // MainVm's own toasts ever need the ToastVm back.
        void toast(string severity, string title, string message)
            => Toast(severity, title, message);

        StatusBar = new StatusVm();
        Transcript = new TranscriptVm();
        Log = new LogVm(n => StatusBar.UnreadLogs = n);
        SettingsPane = new SettingsVm(bridge, dispatcher, toast);
        Engine = new EngineVm(bridge, () => IsIdle, toast);
        Benchmark = new BenchmarkVm(bridge, SettingsPane, toast);
        Audio = new AudioVm(bridge, SettingsPane, toast);

        StartCommand = Gate(() => Accepted(_bridge.Start(), "start"));
        StopCommand = Gate(() => Accepted(_bridge.Stop(), "stop"));
        RestartCommand = Gate(() => Accepted(_bridge.Restart(), "restart"));
        ReconnectBackendCommand = Gate(
            () => Accepted(_bridge.Rebuild(new[] { "backend" }), "reconnect"));
        RescanDevicesCommand = Gate(() => _bridge.RescanDevices());

        // Pause is not gated on the lifecycle slot: it does not rebuild
        // anything, and being unable to pause because a Restart is loading a
        // model would be a worse answer than pausing.
        PauseCommand = new RelayCommand(
            () => { if (_paused) { _bridge.Resume(); } else { _bridge.Pause(); } },
            null, ShowError);

        ClearCommand = new RelayCommand(() =>
        {
            _bridge.ClearTranscript();
            Transcript.Clear();
        }, null, ShowError);

        ExitCommand = new RelayCommand(() =>
        {
            SettingsPane.FlushNow();
            Audio.Stop();
            _bridge.RequestExit();
        }, null, ShowError);

        RunCommand = new RelayCommand(
            () => { if (_running) { StopCommand.Execute(null); } else { StartCommand.Execute(null); } },
            () => _busy.Length == 0, ShowError);
        _gated.Add(RunCommand);

        CloseBannerCommand = new RelayCommand(HideBanner);
    }

    // ---- the tabs ---------------------------------------------------------

    public StatusVm StatusBar { get; }

    public TranscriptVm Transcript { get; }

    public SettingsVm SettingsPane { get; }

    public EngineVm Engine { get; }

    public BenchmarkVm Benchmark { get; }

    public LogVm Log { get; }

    public AudioVm Audio { get; }

    /// <summary>0 transcript, 1 settings, 2 engine, 3 benchmark, 4 log.</summary>
    public int SelectedTab
    {
        get => _selectedTab;
        set
        {
            if (Set(ref _selectedTab, value))
            {
                // Reaching the log tab is what clears its unread badge, so the
                // count means "warnings you have not looked at" rather than
                // "warnings since the last click".
                Log.IsVisible = value == 4;
            }
        }
    }

    // ---- commands ---------------------------------------------------------

    public RelayCommand StartCommand { get; }
    public RelayCommand StopCommand { get; }
    public RelayCommand RestartCommand { get; }
    public RelayCommand ReconnectBackendCommand { get; }
    public RelayCommand RescanDevicesCommand { get; }
    public RelayCommand PauseCommand { get; }
    public RelayCommand ClearCommand { get; }
    public RelayCommand ExitCommand { get; }

    /// <summary>The one button that is Start or Stop depending on the state.</summary>
    public RelayCommand RunCommand { get; }

    public RelayCommand CloseBannerCommand { get; }

    private RelayCommand Gate(Action run)
    {
        var cmd = new RelayCommand(run, () => _busy.Length == 0, ShowError);
        _gated.Add(cmd);
        return cmd;
    }

    private void Accepted(bool accepted, string what)
    {
        if (!accepted)
        {
            Toast("Warning", "Something else is still running",
                  "'" + what + "' was not started because another lifecycle "
                  + "command has the slot. Nothing is queued - press it again "
                  + "when the buttons come back.");
        }
    }

    // ---- state pushed in from the engine ------------------------------------

    /// <summary>
    /// Name of the lifecycle command holding the slot, or "".
    /// </summary>
    public string Busy
    {
        get => _busy;
        set
        {
            if (Set(ref _busy, value ?? ""))
            {
                Raise(nameof(IsIdle));
                Raise(nameof(BusyLabel));
                Raise(nameof(IsBusy));

                // The whole reason RelayCommand does not use RequerySuggested:
                // this transition is driven by a Python daemon thread, which
                // raises no input event, so nothing would re-ask otherwise.
                foreach (RelayCommand c in _gated)
                {
                    c.RaiseCanExecuteChanged();
                }

                Engine.RaiseGates();
                Raise(nameof(SessionTone));
                RefreshRunBanner();
            }
        }
    }

    public bool IsIdle => _busy.Length == 0;

    public bool IsBusy => _busy.Length > 0;

    public string BusyLabel => _busy.Length == 0 ? "" : _busy + "...";

    public bool Running
    {
        get => _running;
        set { if (Set(ref _running, value)) { Raise(nameof(RunButtonText)); } }
    }

    public bool Paused
    {
        get => _paused;
        set { if (Set(ref _paused, value)) { Raise(nameof(PauseButtonText)); } }
    }

    public string RunButtonText => _running ? "Stop" : "Start";

    public string PauseButtonText => _paused ? "Resume" : "Pause";

    public string Status { get => _status; set => Set(ref _status, value); }

    public string Version { get => _version; set => Set(ref _version, value); }

    // ---- the session card ---------------------------------------------------

    /// <summary>capturing | running, but nothing is being captured | stopped.</summary>
    public string SessionState
    {
        get => _sessionState;
        private set => Set(ref _sessionState, value);
    }

    /// <summary>backend, chunking, gate and save target, in one line.</summary>
    public string SessionDetail
    {
        get => _sessionDetail;
        private set => Set(ref _sessionDetail, value);
    }

    /// <summary>good | bad | busy | "" - the session card's dot.</summary>
    public string SessionTone => _busy.Length > 0 && !_busy.StartsWith("engine", StringComparison.Ordinal)
        ? "busy"
        : _running ? (_sourceAlive ? "good" : "bad") : "";

    // ---- the banner --------------------------------------------------------

    public bool BannerOpen { get => _bannerOpen; set => Set(ref _bannerOpen, value); }

    public string BannerTitle { get => _bannerTitle; set => Set(ref _bannerTitle, value); }

    public string BannerMessage { get => _bannerMessage; set => Set(ref _bannerMessage, value); }

    public string BannerSeverity { get => _bannerSeverity; set => Set(ref _bannerSeverity, value); }

    /// <summary>The banner's own buttons - the fix next to the complaint.</summary>
    public ObservableCollection<ToastActionVm> BannerActions { get; } = new();

    public void ShowBanner(string severity, string title, string message)
    {
        BannerActions.Clear();
        BannerSeverity = severity;
        BannerTitle = title;
        BannerMessage = message;
        BannerOpen = true;
    }

    public void HideBanner() => BannerOpen = false;

    /// <summary>
    /// The run banner: one line above everything, because that is where the
    /// eye already is when nothing is happening.
    /// </summary>
    /// <remarks>
    /// Five states, highest priority first: a lifecycle command in flight,
    /// nothing running, a dead capture device, no backend, and hidden. The
    /// browser panel's renderBanner (webui/app.js:637) has the same five -
    /// its sixth, "lost the connection", cannot happen to a panel that lives
    /// in the engine's process. Recomputed on every state event and every
    /// busy transition, so a one-off error banner from a failed command is
    /// overwritten by the truth as soon as the engine speaks again.
    /// </remarks>
    private void RefreshRunBanner()
    {
        if (!_hydrated)
        {
            // Nothing is known yet; a red banner during startup is noise.
            return;
        }

        if (_busy.Length > 0)
        {
            ShowBanner("Informational",
                       char.ToUpperInvariant(_busy[0]) + _busy[1..] + " in progress",
                       "This takes a few seconds - the buttons come back when "
                       + "it finishes.");
            return;
        }

        if (!_running)
        {
            ShowBanner("Warning", "Nothing is capturing or transcribing.", "");
            BannerActions.Add(new ToastActionVm("Start",
                () => StartCommand.Execute(null), primary: true));
            BannerActions.Add(new ToastActionVm("Open Engine",
                () => SelectedTab = 2));
            return;
        }

        if (!_sourceAlive)
        {
            ShowBanner("Error", "The capture device is not running",
                       _stateError);
            BannerActions.Add(new ToastActionVm("Restart",
                () => RestartCommand.Execute(null), primary: true));
            BannerActions.Add(new ToastActionVm("Rescan devices",
                () => RescanDevicesCommand.Execute(null)));
            return;
        }

        if (!_hasBackend)
        {
            ShowBanner("Error", "No transcription backend", _stateError);
            BannerActions.Add(new ToastActionVm("Reconnect",
                () => ReconnectBackendCommand.Execute(null), primary: true));
            BannerActions.Add(new ToastActionVm("Open Engine",
                () => SelectedTab = 2));
            return;
        }

        HideBanner();
    }

    private void ShowError(Exception ex)
        => ShowBanner("Error", "That did not work", ex.Message);

    public ToastVm Toast(string severity, string title, string message)
        => StatusBar.Toast(severity, title, message,
                           severity == "Error" ? ToastLife.Error : ToastLife.Short);

    // ---- hydration -----------------------------------------------------------

    /// <summary>
    /// Take the whole opening document at once - the same payload the browser
    /// gets on connect: schema, settings, status, devices, models, presets,
    /// history, engine and the busy slot.
    /// </summary>
    public void ApplyHello(string json)
    {
        Doc hello = Doc.Parse(json);
        if (!hello.Exists)
        {
            ShowBanner("Error", "The panel could not read the engine",
                       "The opening document was not valid JSON.");
            return;
        }

        Version = hello["version"].Str();

        try
        {
            SettingsPane.LoadSchema(hello["schema"].Raw());
        }
        catch (InvalidOperationException ex)
        {
            // A field kind with no template. Fatal for the settings pane and
            // nothing else, so the rest of the panel still comes up and the
            // banner says exactly what is missing.
            ShowBanner("Error", "The settings pane could not be built", ex.Message);
        }

        SettingsPane.HydrateSettings(hello["settings"].Raw());
        SettingsPane.SetDevices(hello["devices"].Raw());
        SettingsPane.SetModels(hello["models"].Raw());
        SettingsPane.SetPresets(hello["presets"].Raw());

        Engine.ApplyModels(hello["models"].Raw());
        Engine.ApplyCatalog(hello["modelCatalog"].Raw());
        Engine.Apply(hello["engine"].Raw());

        _hydrated = true;
        ApplyState(hello["status"].Raw());
        ApplySettingsEcho(hello["settings"].Raw());
        Transcript.Replace(hello["history"].Raw());
        Audio.SetDevices(hello["devices"].Raw());

        Busy = hello["busy"].Str();
        RefreshRunBanner();
        StatusBar.Offline = false;
        StatusBar.Start();
    }

    /// <summary>
    /// One event off the engine's stream - the same (kind, data) pairs the
    /// websocket carries, dispatched to whichever part of the panel owns the
    /// kind. Unknown kinds are dropped on purpose: the engine growing a new
    /// event must not break an older panel.
    /// </summary>
    /// <remarks>
    /// Runs on the dispatcher thread - PanelHost.PostEvent marshals before
    /// calling. The one exception is <c>meter</c>, which PanelHost hands
    /// straight to <see cref="StatusVm.OfferMeter"/> from the caller's thread
    /// and never routes through here; a meter frame arriving here anyway (the
    /// soak harness does it deliberately) still lands in the right place.
    /// </remarks>
    public void ApplyEvent(string kind, string json)
    {
        switch (kind)
        {
            case "state":
                ApplyState(json);
                break;
            case "settings":
                ApplySettingsEcho(json);
                break;
            case "transcript":
                ApplyTranscript(json);
                break;
            case "meter":
                StatusBar.OfferMeter(json);
                break;
            case "perf":
                StatusBar.ApplyPerf(json);
                break;
            case "log":
                Doc line = Doc.Parse(json);
                string level = line["level"].Str("info");
                Log.Add(level, line["msg"].Str());
                if (level == "error")
                {
                    // Mirrors webui/app.js:913 - an error is worth a toast
                    // even when the log tab is not the one on screen.
                    Toast("Error", "Error", line["msg"].Str());
                }

                break;
            case "engine":
                ApplyEngine(json);
                break;
            case "devices":
                SettingsPane.SetDevices(json);
                Audio.SetDevices(json);
                break;
            case "models":
                SettingsPane.SetModels(json);
                Engine.ApplyModels(json);
                break;
            case "model_catalog":
                Engine.ApplyCatalog(json);
                break;
            case "presets":
                SettingsPane.SetPresets(json);
                break;
            case "benchmark":
                Benchmark.Apply(json);
                break;
            case "history":
                Transcript.Replace(json);
                break;
            case "error":
                Toast("Error", "Error", Doc.Parse(json)["msg"].Str());
                break;
            case "dropped":
                // The duplicate filter working as intended.
                break;
        }
    }

    /// <summary>Apply an <c>engine</c> event - the whisper-server document.</summary>
    public void ApplyEngine(string json)
    {
        Engine.Apply(json);
        Busy = Doc.Parse(json)["busy"].Str();

        // The one state in which the panel looks completely broken: capture
        // runs, the meter moves, and no captions ever appear. Offered once.
        if (!_serverOfferShown && _hydrated && _running
            && !Engine.Reachable
            && string.Equals(Engine.Backend, "server", StringComparison.Ordinal))
        {
            _serverOfferShown = true;
            MaybeOfferServer();
        }
    }

    /// <summary>Apply a <c>state</c> event.</summary>
    public void ApplyState(string json)
    {
        Doc st = Doc.Parse(json);
        if (!st.Exists)
        {
            return;
        }

        Running = st["running"].Bool();
        Paused = st["paused"].Bool();
        _sourceAlive = st["source_alive"].Bool();
        _hasBackend = st["backend"].Str().Length > 0;
        _stateError = st["error"].Str();
        StatusBar.ApplyState(json);

        Status = DescribeStatus(st);
        SessionState = !_running ? "stopped"
            : _sourceAlive ? "capturing"
            : "running, but nothing is being captured";

        var parts = new List<string>
        {
            "backend: " + st["backend_name"].Str("none"),
        };
        string strategy = st["strategy"].Str();
        if (strategy.Length > 0)
        {
            parts.Add("chunking: " + strategy);
        }

        parts.Add(st["gate"].Bool() ? "speech gate on" : "speech gate off");
        string saving = st["saving"].Str();
        if (saving.Length > 0)
        {
            parts.Add("writing " + saving);
        }

        SessionDetail = string.Join("   ·   ", parts);
        Raise(nameof(SessionTone));

        RefreshRunBanner();
        MaybeSuggestModel(st);
    }

    /// <summary>Apply a <c>settings</c> event - the full settings dict.</summary>
    public void ApplySettingsEcho(string json)
    {
        SettingsPane.HydrateSettings(json);
        Doc s = Doc.Parse(json);
        Transcript.SetConfidenceWarn(s["confidence_warn"].Num(0.60));
        Audio.SetCaptureMode(s["capture"].Str());
    }

    public void ApplyTranscript(string json)
    {
        Transcript.Add(json);
        NoteLanguage(Doc.Parse(json));
    }

    private static string DescribeStatus(Doc st)
    {
        if (!st["running"].Bool())
        {
            return "stopped";
        }

        string source = st["source"].Str();
        double uptime = st["uptime_s"].Num();
        string head = st["paused"].Bool() ? "paused" : "listening";
        return string.Format(CultureInfo.InvariantCulture, "{0}  ·  {1}  ·  {2}",
                             head,
                             source.Length > 0 ? source : "no capture",
                             FormatUptime(uptime));
    }

    private static string FormatUptime(double seconds)
    {
        var t = TimeSpan.FromSeconds(Math.Max(0, seconds));
        return t.TotalHours >= 1
            ? string.Format(CultureInfo.InvariantCulture, "{0:0}h {1:00}m",
                            Math.Floor(t.TotalHours), t.Minutes)
            : string.Format(CultureInfo.InvariantCulture, "{0:0}m {1:00}s", t.Minutes, t.Seconds);
    }

    // ---- the interactive toasts ------------------------------------------------
    // Three, and each one exists because a warning that does not carry its own
    // fix is a warning the user has to go and act on somewhere else.

    /// <summary>
    /// Inference cannot hold real-time pace, and there are smaller models
    /// sitting in _models. Offer to restart the server with one.
    /// </summary>
    private void MaybeSuggestModel(Doc st)
    {
        if (_modelHintShown)
        {
            return;
        }

        double? xrt = st["stats"]["xrt"].NumOrNull();
        if (xrt is not double x || x < 1.0 || st["stats"]["windows"].Int() < 3)
        {
            return;
        }

        // One past the launcher default, which is Models[0].
        if (Engine.Models.Count < 2)
        {
            return;
        }

        _modelHintShown = true;

        ToastVm toast = StatusBar.Toast(
            "Warning", "Inference is slower than real time",
            string.Format(CultureInfo.InvariantCulture,
                          "{0:0.00}x. A smaller model is the biggest single lever.", x),
            ToastLife.Long);

        for (int i = 1; i < Engine.Models.Count; i++)
        {
            toast.Options.Add(Engine.Models[i]);
        }

        toast.SelectedOption = toast.Options[0];
        toast.Actions.Add(new ToastActionVm("Restart the server with it", () =>
        {
            // 'restart', not 'start': the whole point is that one is already
            // running, and 'start' would refuse because the port is taken.
            _bridge.EngineAction("restart", toast.SelectedOption?.Value ?? "");
            StatusBar.Dismiss(toast);
        }, primary: true));

        toast.Actions.Add(new ToastActionVm("Benchmark instead", () =>
        {
            SelectedTab = 3;
            StatusBar.Dismiss(toast);
        }));

        Raise(nameof(SelectedTab));
    }

    /// <summary>
    /// Auto-detect has wandered between languages. Offer to pin the commonest.
    /// </summary>
    /// <remarks>
    /// Detection reruns on every buffer and the transcript follows it, so a
    /// bilingual meeting produces a transcript that changes language mid-way
    /// and reads as a transcription failure. Pinning is exactly what stops it.
    /// Never expires: it is a question, and a question that vanishes has been
    /// answered "no" without anyone saying so.
    /// </remarks>
    private void NoteLanguage(Doc entry)
    {
        string code = entry["language"].Str();
        if (code.Length == 0 || code == "??")
        {
            return;
        }

        FieldVm? languageField = SettingsPane.Field("language");
        bool auto = languageField is null
                    || string.Equals(languageField.TextValue, "auto", StringComparison.Ordinal);

        _languagesSeen[code] = _languagesSeen.TryGetValue(code, out int n) ? n + 1 : 1;

        int total = 0;
        string top = code;
        int topCount = 0;
        foreach (KeyValuePair<string, int> kv in _languagesSeen)
        {
            total += kv.Value;
            if (kv.Value > topCount)
            {
                top = kv.Key;
                topCount = kv.Value;
            }
        }

        bool wandering = auto && _languagesSeen.Count > 1 && total >= 6;
        StatusBar.SetLanguage(code, wandering);

        if (!wandering || _languageHintShown)
        {
            return;
        }

        _languageHintShown = true;

        var parts = new List<string>();
        foreach (KeyValuePair<string, int> kv in _languagesSeen)
        {
            parts.Add(kv.Key + " x" + kv.Value.ToString(CultureInfo.InvariantCulture));
        }

        ToastVm toast = StatusBar.Toast(
            "Warning", "Auto-detect is wandering",
            string.Format(CultureInfo.InvariantCulture,
                          "{0} different languages this session ({1}). Detection reruns "
                          + "per buffer, and the transcript follows it.",
                          _languagesSeen.Count, string.Join(", ", parts)),
            ToastLife.Never);

        string pin = top;
        toast.Actions.Add(new ToastActionVm("Pin " + pin, () =>
        {
            SettingsPane.ApplyPatch(new Dictionary<string, JsonElement>
            {
                ["language"] = JsonSerializer.SerializeToElement(pin),
            });
            StatusBar.Dismiss(toast);
        }, primary: true));

        toast.Actions.Add(new ToastActionVm("Leave it", () => StatusBar.Dismiss(toast)));
    }

    /// <summary>
    /// The backend is pinned to the GPU server and the server is not there.
    /// Offer to start it, or to fall back to the CPU.
    /// </summary>
    /// <remarks>
    /// This is the state in which the panel looks completely broken: capture
    /// runs, the meter moves, and no captions ever appear, because
    /// create_backend checked once at build time, failed, and left
    /// Pipeline.backend as None with nothing that would ever look again.
    /// </remarks>
    public void MaybeOfferServer()
    {
        if (Engine.Reachable
            || !string.Equals(Engine.Backend, "server", StringComparison.Ordinal))
        {
            return;
        }

        ToastVm toast = StatusBar.Toast(
            "Error", "The transcription backend has nowhere to go",
            "Transcription is pinned to the GPU server, and nothing is answering on "
            + Engine.Host + ":" + Engine.PortText + ". Captions will not appear until "
            + "one of these is true.",
            ToastLife.Never);

        if (Engine.CanStart)
        {
            toast.Actions.Add(new ToastActionVm("Start the server", () =>
            {
                _bridge.EngineAction("start", "");
                StatusBar.Dismiss(toast);
            }, primary: true));
        }

        toast.Actions.Add(new ToastActionVm("Use the CPU instead", () =>
        {
            SettingsPane.ApplyPatch(new Dictionary<string, JsonElement>
            {
                ["backend"] = JsonSerializer.SerializeToElement("local"),
            });
            StatusBar.Dismiss(toast);
        }));

        toast.Actions.Add(new ToastActionVm("Dismiss", () => StatusBar.Dismiss(toast)));
    }

    /// <summary>Render the session and hand back the payload for saving.</summary>
    public Doc Export(string format) => Doc.Parse(_bridge.ExportJson(format));
}
