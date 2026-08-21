using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Text;
using System.Text.Json;
using LiveTranscription.Ui.Bridge;
using LiveTranscription.Ui.Settings;

namespace LiveTranscription.Ui.ViewModels;

/// <summary>One measured buffer size.</summary>
public sealed class BenchRowVm
{
    public BenchRowVm(Doc row)
    {
        Buffer = row["buffer"].Int();
        Median = row["median"].Num();
        Min = row["min"].Num();
        Max = row["max"].Num();
        Chars = row["chars"].Int();
        Xrt = row["xrt"].Num();
        Slide = row["slide"].Num();
    }

    public int Buffer { get; }

    public double Median { get; }

    public double Min { get; }

    public double Max { get; }

    public int Chars { get; }

    /// <summary>Inference time over audio length. Under 1 means it keeps up.</summary>
    public double Xrt { get; }

    /// <summary>The slide this buffer can sustain, with headroom.</summary>
    public double Slide { get; }

    public string BufferText => Buffer.ToString(CultureInfo.InvariantCulture) + " s";

    public string MedianText => Median.ToString("0.00", CultureInfo.InvariantCulture) + " s";

    public string SpreadText => string.Format(CultureInfo.InvariantCulture,
                                              "{0:0.00} - {1:0.00} s", Min, Max);

    public string XrtText => Xrt.ToString("0.00", CultureInfo.InvariantCulture) + "x";

    public string SlideText => Slide.ToString("0.0", CultureInfo.InvariantCulture) + " s";

    public bool Sustainable => Xrt < 1.0;

    public string Tone => Sustainable ? "good" : "bad";
}

/// <summary>
/// The benchmark tab: time this machine, then offer to act on the result.
/// </summary>
/// <remarks>
/// <para>
/// The measurement runs on the LIVE backend, with the worker paused, which is
/// why it belongs behind the bridge rather than in this assembly: measuring a
/// separately-built backend would report a model that is not the one producing
/// your captions, and under <c>--backend auto</c> would not even know which
/// hardware it is on.
/// </para>
/// <para>
/// <see cref="ApplyRecommendationCommand"/> is the reason the settings pane
/// sends whole patches. It moves strategy, buffer and slide TOGETHER, and
/// settings.validate rejects <c>slide &gt; buffer</c> against the merged
/// result - so sent one at a time, whichever arrives first is checked against
/// the old value of the other and refused, and a legal pair cannot be entered
/// at all.
/// </para>
/// </remarks>
public sealed class BenchmarkVm : ViewModelBase
{
    private readonly IEngineBridge _bridge;
    private readonly SettingsVm _settings;
    private readonly Action<string, string, string> _toast;

    private bool _running;
    private string _status = "Not run yet.";
    private string _audio = "";
    private bool _synthetic;
    private string _backend = "";
    private string _buffers = "4, 8, 16, 24";
    private string _wavPath = "";
    private int _reps = 2;
    private int? _bestBuffer;
    private double? _bestSlide;

    public BenchmarkVm(IEngineBridge bridge, SettingsVm settings,
                       Action<string, string, string> toast)
    {
        _bridge = bridge;
        _settings = settings;
        _toast = toast;

        RunCommand = new RelayCommand(Run, () => !_running, OnError);
        ApplyRecommendationCommand = new RelayCommand(
            ApplyRecommendation, () => _bestBuffer is not null, OnError);
    }

    public RelayCommand RunCommand { get; }

    public RelayCommand ApplyRecommendationCommand { get; }

    public ObservableCollection<BenchRowVm> Rows { get; } = new();

    public bool IsRunning
    {
        get => _running;
        private set
        {
            if (Set(ref _running, value))
            {
                RunCommand.RaiseCanExecuteChanged();
            }
        }
    }

    public string Status { get => _status; private set => Set(ref _status, value); }

    public string Audio { get => _audio; private set => Set(ref _audio, value); }

    /// <summary>
    /// Were these numbers measured on tones rather than speech?
    /// </summary>
    /// <remarks>
    /// It matters enough to say on screen: synthetic audio decodes almost no
    /// tokens, and decoding is the part that varies. A synthetic run measures
    /// the encoder floor and nothing else, so it is a lower bound rather than
    /// a prediction.
    /// </remarks>
    public bool Synthetic
    {
        get => _synthetic;
        private set => Set(ref _synthetic, value);
    }

    public string Backend { get => _backend; private set => Set(ref _backend, value); }

    /// <summary>Comma-separated buffer sizes to measure.</summary>
    public string Buffers { get => _buffers; set => Set(ref _buffers, value); }

    /// <summary>A WAV of real speech, or "" for synthetic tones.</summary>
    public string WavPath { get => _wavPath; set => Set(ref _wavPath, value); }

    public int Reps
    {
        get => _reps;
        set => Set(ref _reps, Math.Max(1, Math.Min(10, value)));
    }

    public string Recommendation => _bestBuffer is int b && _bestSlide is double s
        ? string.Format(CultureInfo.InvariantCulture,
                        "A {0} s buffer with a {1:0.0} s slide is the fastest setting "
                        + "this machine sustains.", b, s)
        : "";

    public bool HasRecommendation => _bestBuffer is not null;

    private void Run()
    {
        var sizes = new List<int>();
        foreach (string part in _buffers.Split(',', ';', ' '))
        {
            if (int.TryParse(part.Trim(), NumberStyles.Integer,
                             CultureInfo.InvariantCulture, out int n) && n >= 1)
            {
                sizes.Add(n);
            }
        }

        if (sizes.Count == 0)
        {
            _toast("Warning", "Nothing to measure",
                   "Give at least one buffer size, e.g. \"4, 8, 16\".");
            return;
        }

        var args = new StringBuilder("{\"buffers\":[");
        for (int i = 0; i < sizes.Count; i++)
        {
            if (i > 0)
            {
                args.Append(',');
            }

            args.Append(sizes[i].ToString(CultureInfo.InvariantCulture));
        }

        args.Append("],\"reps\":").Append(_reps.ToString(CultureInfo.InvariantCulture));
        if (_wavPath.Trim().Length > 0)
        {
            args.Append(",\"wav\":").Append(JsonSerializer.Serialize(_wavPath.Trim()));
        }

        args.Append('}');

        Rows.Clear();
        _bestBuffer = null;
        _bestSlide = null;
        Raise(nameof(Recommendation));
        Raise(nameof(HasRecommendation));
        ApplyRecommendationCommand.RaiseCanExecuteChanged();

        IsRunning = true;
        Status = "Starting...";

        // Returns as soon as the thread is spawned. Everything after this
        // arrives as benchmark events, which is why IsRunning is cleared in
        // Apply and not here.
        if (!_bridge.RunBenchmark(args.ToString()))
        {
            IsRunning = false;
            Status = "The engine refused to start a benchmark.";
        }
    }

    /// <summary>Apply a <c>benchmark</c> event.</summary>
    public void Apply(string json)
    {
        Doc b = Doc.Parse(json);
        string status = b["status"].Str();

        switch (status)
        {
            case "running":
                IsRunning = true;
                Audio = b["audio"].Str();
                Synthetic = b["synthetic"].Bool();
                Status = "Measuring " + b["sizes"].Count + " buffer sizes...";
                break;

            case "row":
                IsRunning = true;
                Rows.Add(new BenchRowVm(b["row"]));
                Status = string.Format(CultureInfo.InvariantCulture,
                                       "Measured {0} s...", b["row"]["buffer"].Int());
                break;

            case "done":
                IsRunning = false;
                Audio = b["audio"].Str(Audio);
                Synthetic = b["synthetic"].Bool();
                Backend = b["backend"].Str();

                // Rebuilt from the final document rather than trusted from the
                // row events: a benchmark started from the other front end
                // produces a "done" with no "row" events having reached here.
                Rows.Clear();
                foreach (Doc r in b["rows"].Items())
                {
                    Rows.Add(new BenchRowVm(r));
                }

                Doc rec = b["recommend"];
                _bestBuffer = rec["buffer"].IntOrNull();
                _bestSlide = rec["slide"].NumOrNull();
                Status = Rows.Count > 0 ? "Done." : "Nothing was measured.";
                Raise(nameof(Recommendation));
                Raise(nameof(HasRecommendation));
                ApplyRecommendationCommand.RaiseCanExecuteChanged();
                break;

            case "error":
                IsRunning = false;
                Status = b["msg"].Str("The benchmark failed.");
                _toast("Error", "Benchmark failed", Status);
                break;
        }
    }

    private void ApplyRecommendation()
    {
        if (_bestBuffer is not int buffer || _bestSlide is not double slide)
        {
            return;
        }

        // All three in one patch. See the class remarks - this is the case the
        // whole one-patch-per-flush design exists for.
        _settings.ApplyPatch(new Dictionary<string, JsonElement>
        {
            ["strategy"] = JsonSerializer.SerializeToElement("sliding_window"),
            ["buffer"] = JsonSerializer.SerializeToElement((double)buffer),
            ["slide"] = JsonSerializer.SerializeToElement(slide),
        });

        _toast("Success", "Applied",
               string.Format(CultureInfo.InvariantCulture,
                             "Sliding window, {0} s buffer, {1:0.0} s slide.", buffer, slide));
    }

    private void OnError(Exception ex)
        => _toast("Error", "The benchmark command failed", ex.Message);
}
