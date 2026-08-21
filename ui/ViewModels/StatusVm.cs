using System;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Windows.Threading;
using LiveTranscription.Ui.Bridge;

namespace LiveTranscription.Ui.ViewModels;

/// <summary>
/// The strip above the transcript: level meters, four pills, and the toasts.
/// </summary>
/// <remarks>
/// <para>
/// These are four DISTINCT "something is wrong" channels and they are not
/// interchangeable. The banner says the session is in a bad state. A pill says
/// which component is. The meter says whether any audio is arriving at all. A
/// toast says something just happened. Collapsing them into one status line
/// is the design this panel exists to replace: "the meters are flat" and
/// "nothing is running" look identical until something says which.
/// </para>
/// <para>
/// <b>The meter does not raise PropertyChanged per frame.</b> Frames arrive at
/// 8 Hz from the pipeline's worker thread; <see cref="OfferMeter"/> parks them
/// in a field under a lock and returns, touching no dispatcher and no binding.
/// A 30 Hz timer on the UI thread publishes whatever is parked. That decouples
/// a Python worker thread from WPF entirely - a push costs a lock and a parse,
/// not a BeginInvoke and a re-render - and it is what lets the bar decay
/// smoothly between frames instead of stepping eight times a second.
/// </para>
/// </remarks>
public sealed class StatusVm : ViewModelBase
{
    /// <summary>
    /// The meter scale. Speech RMS lives near the bottom of 0..1, so a linear
    /// bar is a flat line with occasional twitches; sqrt(level / 0.25) spreads
    /// the range people actually speak in across the whole bar. Same curve as
    /// webui/app.js:496, so the two panels show the same picture.
    /// </summary>
    private const double MeterFullScale = 0.25;

    /// <summary>How fast the bar falls when the level drops, per second.</summary>
    private const double DecayPerSecond = 2.2;

    private readonly DispatcherTimer _pump;
    private readonly object _gate = new();

    // Parked by OfferMeter from any thread; read by the pump on the UI thread.
    private double _rawLevel;
    private double _rawThreshold;
    private double _rawPending;
    private int _rawQueue;
    private int? _rawSpeechFrames;
    private int _rawMinFrames;
    private bool _rawVad;
    private string _rawGated = "";
    private bool _meterDirty;

    private double _level;
    private double _levelPeak;
    private double _threshold;
    private double _vadFill;
    private double _vadMark;
    private string _vadText = "off";
    private string _gateState = "";
    private string _gateText = "";
    private string _pendingText = "";
    private string _levelText = "0.0000";
    private bool _offline;
    private int _unreadLogs;

    public StatusVm()
    {
        BackendPill = new PillVm("engine");
        SourcePill = new PillVm("audio");
        RealtimePill = new PillVm("speed");
        LanguagePill = new PillVm("language");

        _pump = new DispatcherTimer(DispatcherPriority.Render)
        {
            Interval = TimeSpan.FromMilliseconds(1000.0 / 30.0),
        };
        _pump.Tick += (_, _) => Pump();
    }

    public PillVm BackendPill { get; }

    public PillVm SourcePill { get; }

    public PillVm RealtimePill { get; }

    public PillVm LanguagePill { get; }

    public ObservableCollection<ToastVm> Toasts { get; } = new();

    /// <summary>Start the 30 Hz pull. Called once the window is up.</summary>
    public void Start() => _pump.Start();

    public void Stop() => _pump.Stop();

    // ---- the meter --------------------------------------------------------

    /// <summary>
    /// Take a meter frame. Safe from ANY thread and deliberately cheap: it
    /// parses, takes a lock, assigns and returns. Nothing here touches a
    /// dependency object, so nothing here can throw a thread-affinity
    /// exception or block on the dispatcher.
    /// </summary>
    public void OfferMeter(string json)
    {
        Doc m = Doc.Parse(json);
        if (!m.Exists)
        {
            return;
        }

        lock (_gate)
        {
            _rawLevel = m["level"].Num();
            _rawThreshold = m["threshold"].Num();
            _rawPending = m["pending_s"].Num();
            _rawQueue = m["queue"].Int();
            _rawSpeechFrames = m["speech_frames"].IntOrNull();
            _rawMinFrames = m["min_frames"].Int();
            _rawVad = m["vad"].Bool();
            _rawGated = m["gated"].Str();
            _meterDirty = true;
        }
    }

    private void Pump()
    {
        double level, threshold, pending;
        int queue, minFrames;
        int? frames;
        bool vad, dirty;
        string gated;

        lock (_gate)
        {
            level = _rawLevel;
            threshold = _rawThreshold;
            pending = _rawPending;
            queue = _rawQueue;
            frames = _rawSpeechFrames;
            minFrames = _rawMinFrames;
            vad = _rawVad;
            gated = _rawGated;
            dirty = _meterDirty;
            _meterDirty = false;
        }

        double target = Scale(level);

        // Rise instantly, fall gradually. A meter that tracks the raw value in
        // both directions flickers to zero in every gap between words, which
        // reads as dropouts in the capture rather than as normal speech.
        if (target >= _levelPeak)
        {
            _levelPeak = target;
        }
        else
        {
            _levelPeak = Math.Max(target, _levelPeak - (DecayPerSecond / 30.0));
        }

        Level = _levelPeak;
        Threshold = Scale(threshold);

        if (!dirty)
        {
            // Nothing new arrived; the decay above still had to run, but none
            // of the text below can have changed.
            return;
        }

        LevelText = level.ToString("0.0000", CultureInfo.InvariantCulture);

        if (vad && minFrames > 0)
        {
            // Scaled against three times the requirement so the threshold mark
            // sits a third of the way along and there is somewhere for a good
            // reading to go. Mirrors webui/app.js:504.
            double scale = Math.Max(minFrames * 3.0, 24.0);
            VadFill = frames is int f ? Math.Min(1.0, f / scale) : 0.0;
            VadMark = Math.Min(1.0, minFrames / scale);
            VadText = frames is int n
                ? string.Format(CultureInfo.InvariantCulture, "{0}/{1}", n, minFrames)
                : "-";
        }
        else
        {
            VadFill = 0.0;
            VadMark = 0.0;
            VadText = vad ? "-" : "off";
        }

        switch (gated)
        {
            case "silence":
                GateState = "silence";
                GateText = "below the silence threshold - nothing is being transcribed";
                break;
            case "no-speech":
                GateState = "nospeech";
                GateText = "audible, but the speech gate does not hear speech in it";
                break;
            default:
                if (level > threshold)
                {
                    GateState = "live";
                    GateText = "listening";
                }
                else
                {
                    GateState = "";
                    GateText = "";
                }

                break;
        }

        PendingText = pending > 0
            ? string.Format(CultureInfo.InvariantCulture, "buffered {0:0.0} s{1}",
                            pending, queue > 0 ? "   ·   queue " + queue : "")
            : "";
    }

    private static double Scale(double raw)
        => Math.Min(1.0, Math.Sqrt(Math.Max(0.0, raw) / MeterFullScale));

    /// <summary>0..1, already curved and decayed. Bind a bar's width to it.</summary>
    public double Level
    {
        get => _level;
        private set => Set(ref _level, value);
    }

    /// <summary>0..1 - where the silence threshold sits on the same scale.</summary>
    public double Threshold
    {
        get => _threshold;
        private set => Set(ref _threshold, value);
    }

    public string LevelText
    {
        get => _levelText;
        private set => Set(ref _levelText, value);
    }

    public double VadFill
    {
        get => _vadFill;
        private set => Set(ref _vadFill, value);
    }

    public double VadMark
    {
        get => _vadMark;
        private set => Set(ref _vadMark, value);
    }

    public string VadText
    {
        get => _vadText;
        private set => Set(ref _vadText, value);
    }

    /// <summary>"" | live | silence | nospeech</summary>
    public string GateState
    {
        get => _gateState;
        private set => Set(ref _gateState, value);
    }

    public string GateText
    {
        get => _gateText;
        private set { if (Set(ref _gateText, value)) { Raise(nameof(HasGateText)); } }
    }

    public bool HasGateText => _gateText.Length > 0;

    public string PendingText
    {
        get => _pendingText;
        private set => Set(ref _pendingText, value);
    }

    // ---- the pills ----------------------------------------------------------

    /// <summary>Apply a <c>state</c> event - Pipeline.status().</summary>
    public void ApplyState(string json)
    {
        Doc st = Doc.Parse(json);
        if (!st.Exists)
        {
            return;
        }

        string backend = st["backend"].Str();
        string error = st["error"].Str();
        BackendPill.Set(
            backend switch { "server" => "GPU", "local" => "CPU", _ => "-" },
            backend switch { "server" => "gpu", "local" => "cpu", _ => error.Length > 0 ? "bad" : "" },
            st["backend_name"].Str(error.Length > 0 ? error : "no backend"));

        string source = st["source"].Str();
        SourcePill.Set(
            source.Length > 0 ? ShortSource(source) : "-",
            st["source_alive"].Bool() ? "good" : source.Length > 0 ? "bad" : "",
            source.Length > 0 ? source : "no capture");

        double? xrt = st["stats"]["xrt"].NumOrNull();
        if (xrt is double x)
        {
            RealtimePill.Set(
                x.ToString("0.00", CultureInfo.InvariantCulture) + "x",
                x < 0.9 ? "good" : "bad",
                "inference time over audio length - under 1.00 means it keeps up");
        }
    }

    /// <summary>Apply a <c>perf</c> event - one window's timing.</summary>
    public void ApplyPerf(string json)
    {
        Doc p = Doc.Parse(json);
        if (!p.Exists)
        {
            return;
        }

        RealtimePill.Set(
            p["xrt"].Num().ToString("0.00", CultureInfo.InvariantCulture) + "x",
            p["sustainable"].Bool() ? "good" : "bad",
            string.Format(CultureInfo.InvariantCulture,
                          "{0:0.00} s for a {1} s window, step {2} s",
                          p["infer_s"].Num(), p["window_s"].Num(), p["slide_s"].Num()));
    }

    public void SetLanguage(string code, bool wandering)
        => LanguagePill.Set(code, wandering ? "bad" : "",
                            wandering
                                ? "auto-detect has landed on several languages this session"
                                : "detected language");

    /// <summary>
    /// The engine is gone - dim everything rather than leave stale numbers on
    /// screen looking live.
    /// </summary>
    public bool Offline
    {
        get => _offline;
        set => Set(ref _offline, value);
    }

    // ---- the log badge -------------------------------------------------------

    /// <summary>Warnings and errors the user has not looked at yet.</summary>
    public int UnreadLogs
    {
        get => _unreadLogs;
        set
        {
            if (Set(ref _unreadLogs, value))
            {
                Raise(nameof(HasUnreadLogs));
                Raise(nameof(UnreadLabel));
            }
        }
    }

    public bool HasUnreadLogs => _unreadLogs > 0;

    public string UnreadLabel => _unreadLogs > 99 ? "99+"
        : _unreadLogs.ToString(CultureInfo.InvariantCulture);

    // ---- toasts ---------------------------------------------------------------

    public ToastVm Toast(string severity, string title, string message,
                         ToastLife life = ToastLife.Short)
    {
        var toast = new ToastVm(severity, title, message, life, Dismiss);

        // Oldest first out. Without a cap a burst of engine errors - and they
        // do burst, the worker logs one per failed window - buries the screen.
        while (Toasts.Count >= 4)
        {
            Dismiss(Toasts[0]);
        }

        Toasts.Add(toast);
        return toast;
    }

    public void Dismiss(ToastVm toast)
    {
        toast.Cancel();
        Toasts.Remove(toast);
    }

    public void ClearToasts()
    {
        foreach (ToastVm t in Toasts)
        {
            t.Cancel();
        }

        Toasts.Clear();
    }

    private static string ShortSource(string name)
    {
        int cut = name.IndexOf(':');
        string tail = cut >= 0 ? name[(cut + 1)..].Trim() : name;
        return tail.Length > 22 ? tail[..21] + "…" : tail;
    }
}
