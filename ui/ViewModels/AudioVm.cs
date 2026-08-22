using System;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.Json;
using System.Threading;
using System.Windows.Threading;
using LiveTranscription.Ui.Audio;
using LiveTranscription.Ui.Bridge;
using LiveTranscription.Ui.Settings;

namespace LiveTranscription.Ui.ViewModels;

/// <summary>
/// The "capture from this panel" card: push this machine's microphone or
/// system audio into the engine over the bridge, as the desktop half of what
/// <c>capture: browser</c> does.
/// </summary>
/// <remarks>
/// <para>
/// The engine can already open devices itself, so this exists for the cases
/// where its libraries cannot: soundcard's loopback and PortAudio's inputs
/// each have devices they refuse that WASAPI serves happily, and a push
/// source needs no engine-side rebuild to switch. The bytes are identical to
/// the browser's - 16 kHz mono PCM16 in 128 ms blocks into
/// <c>Pipeline.feed</c> - so the engine cannot tell the three pushers apart.
/// </para>
/// <para>
/// Starting flips the <c>capture</c> setting to <c>browser</c> first when it
/// is anything else, exactly as webui/app.js:990 does: pushed audio lands in
/// a source that only exists in that mode, and feeding a mode that cannot
/// hear it would look like the button doing nothing.
/// </para>
/// </remarks>
public sealed class AudioVm : ViewModelBase
{
    private readonly IEngineBridge _bridge;
    private readonly SettingsVm _settings;
    private readonly Action<string, string, string> _toast;
    private readonly MicStreamer _streamer;
    private readonly DispatcherTimer _statsTimer;

    private long _sentBytes;
    private int _rejected;
    private bool _streaming;
    private bool _loopback;
    private string _captureMode = "";
    private string _stateText = "not streaming";
    private string _statsText = "";
    private DateTime _startedAt;
    private ChoiceVm? _selectedDevice;

    public AudioVm(IEngineBridge bridge, SettingsVm settings,
                   Action<string, string, string> toast)
    {
        _bridge = bridge;
        _settings = settings;
        _toast = toast;
        _streamer = new MicStreamer(OnBlock, OnStreamerStopped);

        MicCommand = new RelayCommand(
            () => Toggle(loopback: false),
            () => !_streaming || !_loopback,
            OnError);
        LoopbackCommand = new RelayCommand(
            () => Toggle(loopback: true),
            () => !_streaming || _loopback,
            OnError);

        _statsTimer = new DispatcherTimer
        {
            Interval = TimeSpan.FromSeconds(1),
        };
        _statsTimer.Tick += (_, _) => PublishStats();

        RefreshDevices();
    }

    public RelayCommand MicCommand { get; }

    public RelayCommand LoopbackCommand { get; }

    public ObservableCollection<ChoiceVm> Devices { get; } = new();

    public ChoiceVm? SelectedDevice
    {
        get => _selectedDevice;
        set => Set(ref _selectedDevice, value);
    }

    public bool IsStreaming
    {
        get => _streaming;
        private set
        {
            if (Set(ref _streaming, value))
            {
                Raise(nameof(MicButtonText));
                Raise(nameof(LoopButtonText));
                MicCommand.RaiseCanExecuteChanged();
                LoopbackCommand.RaiseCanExecuteChanged();
            }
        }
    }

    public string MicButtonText
        => _streaming && !_loopback ? "Stop" : "Start microphone";

    public string LoopButtonText
        => _streaming && _loopback ? "Stop" : "System audio";

    public string StateText
    {
        get => _stateText;
        private set => Set(ref _stateText, value);
    }

    public string StatsText
    {
        get => _statsText;
        private set => Set(ref _statsText, value);
    }

    // ---- lifecycle --------------------------------------------------------

    private void Toggle(bool loopback)
    {
        if (IsStreaming)
        {
            Stop();
            return;
        }

        if (!string.Equals(_captureMode, "browser", StringComparison.Ordinal))
        {
            _settings.ApplyPatch(new System.Collections.Generic.Dictionary<string, JsonElement>
            {
                ["capture"] = JsonSerializer.SerializeToElement("browser"),
            });
            _toast("Success", "Capture mode switched",
                   "Capture is now \"browser\" - the push mode this panel "
                   + "feeds. Switch it back to give the engine its device back.");
        }

        try
        {
            _streamer.Start(loopback ? "" : SelectedDevice?.Value ?? "",
                            loopback);
        }
        catch (Exception ex)
        {
            OnError(ex);
            return;
        }

        _loopback = loopback;
        Interlocked.Exchange(ref _sentBytes, 0);
        _rejected = 0;
        _startedAt = DateTime.UtcNow;
        IsStreaming = true;
        StateText = loopback
            ? "streaming what the speakers are playing"
            : "streaming the microphone";
        _statsTimer.Start();
    }

    /// <summary>Stop pushing. Safe to call when nothing is running.</summary>
    public void Stop(string reason = "")
    {
        _statsTimer.Stop();
        _streamer.Stop();
        if (!IsStreaming)
        {
            return;
        }

        IsStreaming = false;
        StateText = reason.Length > 0
            ? "stopped (" + reason + ")"
            : "not streaming";
        StatsText = "";
    }

    // ---- state from the engine -------------------------------------------

    /// <summary>The <c>capture</c> setting, from every settings echo.</summary>
    public void SetCaptureMode(string mode)
    {
        _captureMode = mode ?? "";
        if (IsStreaming
            && !string.Equals(_captureMode, "browser", StringComparison.Ordinal))
        {
            // The engine swapped to a device source, so pushed audio now has
            // nowhere to land. Stopping is the honest answer; streaming into
            // a void would look exactly like working.
            Stop("capture mode changed to " + _captureMode);
        }
    }

    /// <summary>
    /// A fresh device enumeration arrived from the engine. The list itself is
    /// PortAudio's and soundcard's - ids WASAPI cannot open - so it is only
    /// the signal to re-enumerate with the API this card captures with.
    /// </summary>
    public void SetDevices(string devicesJson)
    {
        _ = devicesJson;
        RefreshDevices();
    }

    private void RefreshDevices()
    {
        string keep = SelectedDevice?.Value ?? "";
        Devices.Clear();
        Devices.Add(new ChoiceVm("", "(default microphone)"));
        foreach ((string id, string name, bool isDefault)
                 in MicStreamer.CaptureDevices())
        {
            Devices.Add(new ChoiceVm(
                id, isDefault ? name + "   <- Windows default" : name));
        }

        ChoiceVm? restore = null;
        foreach (ChoiceVm c in Devices)
        {
            if (string.Equals(c.Value, keep, StringComparison.Ordinal))
            {
                restore = c;
            }
        }

        SelectedDevice = restore ?? Devices[0];
    }

    // ---- the capture thread ----------------------------------------------

    private void OnBlock(byte[] block)
    {
        // NAudio's thread. Nothing here may touch a binding; the counters are
        // read by the 1 Hz stats timer on the UI thread.
        if (_bridge.PushAudioChunk(block))
        {
            Interlocked.Add(ref _sentBytes, block.Length);
            _rejected = 0;
        }
        else
        {
            // The engine is not in a push mode, or is stopped. Say so once it
            // is clearly persistent rather than a mode change racing a block.
            if (++_rejected == 8)
            {
                StatePostedFromCaptureThread(
                    "the engine is not taking audio - is the session running, "
                    + "with capture set to \"browser\"?");
            }
        }
    }

    private void StatePostedFromCaptureThread(string text)
        => _statsTimer.Dispatcher.BeginInvoke(
            new Action(() => StateText = text), DispatcherPriority.Background);

    private void OnStreamerStopped(string why)
        => _statsTimer.Dispatcher.BeginInvoke(
            new Action(() => Stop(why)), DispatcherPriority.Background);

    private void PublishStats()
    {
        long sent = Interlocked.Read(ref _sentBytes);
        double audioSeconds = sent / 2.0 / MicStreamer.TargetRate;
        double elapsed = (DateTime.UtcNow - _startedAt).TotalSeconds;
        StatsText = string.Format(CultureInfo.InvariantCulture,
                                  "{0:0} KB sent   ·   {1:0.0} s of audio   ·   {2:0} s elapsed",
                                  sent / 1024.0, audioSeconds, elapsed);
    }

    private void OnError(Exception ex)
        => _toast("Error", "Could not capture", ex.Message);
}
