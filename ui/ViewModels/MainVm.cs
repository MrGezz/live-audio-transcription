using System;
using System.Collections.Generic;
using System.Windows.Threading;
using LiveTranscription.Ui.Bridge;

namespace LiveTranscription.Ui.ViewModels;

/// <summary>
/// The window's data context. Owns the busy gate that every lifecycle button
/// hangs off, so the single-slot rule the Python side enforces is mirrored
/// once here rather than re-derived per button.
/// </summary>
public sealed class MainVm : ViewModelBase
{
    private readonly IEngineBridge _bridge;
    private readonly Dispatcher _dispatcher;
    private readonly List<RelayCommand> _gated = new();

    private string _busy = "";
    private bool _running;
    private bool _paused;
    private string _status = "starting";
    private string _bannerTitle = "";
    private string _bannerMessage = "";
    private string _bannerSeverity = "Informational";
    private bool _bannerOpen;

    public MainVm(IEngineBridge bridge, Dispatcher dispatcher)
    {
        _bridge = bridge;
        _dispatcher = dispatcher;

        StartCommand = Gate(() => _bridge.Start());
        StopCommand = Gate(() => _bridge.Stop());
        RestartCommand = Gate(() => _bridge.Restart());
        ReconnectBackendCommand = Gate(() => _bridge.Rebuild(new[] { "backend" }));
        RescanDevicesCommand = Gate(() => _bridge.RescanDevices());

        // Pause is not gated on the lifecycle slot: it does not rebuild
        // anything, and being unable to pause because a Restart is loading a
        // model would be a worse answer than pausing.
        PauseCommand = new RelayCommand(
            () => { if (_paused) { _bridge.Resume(); } else { _bridge.Pause(); } },
            null, ShowError);

        ClearCommand = new RelayCommand(() => _bridge.ClearTranscript(), null, ShowError);
        ExitCommand = new RelayCommand(() => _bridge.RequestExit(), null, ShowError);
    }

    /// <summary>Build a command that is disabled while any lifecycle command runs.</summary>
    private RelayCommand Gate(Action run)
    {
        var cmd = new RelayCommand(run, () => _busy.Length == 0, ShowError);
        _gated.Add(cmd);
        return cmd;
    }

    public RelayCommand StartCommand { get; }
    public RelayCommand StopCommand { get; }
    public RelayCommand RestartCommand { get; }
    public RelayCommand ReconnectBackendCommand { get; }
    public RelayCommand RescanDevicesCommand { get; }
    public RelayCommand PauseCommand { get; }
    public RelayCommand ClearCommand { get; }
    public RelayCommand ExitCommand { get; }

    /// <summary>
    /// Name of the lifecycle command currently holding the slot, or "".
    /// </summary>
    public string Busy
    {
        get => _busy;
        set
        {
            if (Set(ref _busy, value ?? ""))
            {
                Raise(nameof(IsIdle));
                // The whole reason RelayCommand does not use RequerySuggested:
                // this transition is driven by a Python daemon thread, which
                // raises no input event, so nothing would re-ask otherwise.
                foreach (RelayCommand c in _gated)
                {
                    c.RaiseCanExecuteChanged();
                }
            }
        }
    }

    public bool IsIdle => _busy.Length == 0;

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

    // ---- the banner ------------------------------------------------------
    // One place that says what is wrong, above the meters, because "the meters
    // are flat" and "nothing is running" look identical until something says
    // which.

    public bool BannerOpen { get => _bannerOpen; set => Set(ref _bannerOpen, value); }
    public string BannerTitle { get => _bannerTitle; set => Set(ref _bannerTitle, value); }
    public string BannerMessage { get => _bannerMessage; set => Set(ref _bannerMessage, value); }
    public string BannerSeverity { get => _bannerSeverity; set => Set(ref _bannerSeverity, value); }

    public void ShowBanner(string severity, string title, string message)
    {
        BannerSeverity = severity;
        BannerTitle = title;
        BannerMessage = message;
        BannerOpen = true;
    }

    public void HideBanner() => BannerOpen = false;

    private void ShowError(Exception ex)
        => ShowBanner("Error", "That did not work", ex.Message);
}
