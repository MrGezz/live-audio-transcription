using System;
using System.Collections.ObjectModel;
using System.Windows.Threading;
using LiveTranscription.Ui.Bridge;
using LiveTranscription.Ui.Settings;

namespace LiveTranscription.Ui.ViewModels;

/// <summary>
/// How long a toast stays on screen.
/// </summary>
/// <remarks>
/// Four values, and the differences between them are the whole design. A
/// confirmation is read in passing and 4.5 s is generous. An error has to
/// survive the user looking away, so 9 s. A toast that OFFERS something -
/// "inference is slower than real time, here are the smaller models" - is
/// worthless if it expires while the user is deciding, so 30 s. And one that
/// asks a question with no default answer must not disappear at all, because
/// a question that vanishes has been answered "no" without anyone saying so.
/// The browser panel uses exactly these (webui/app.js:1073, :572, :893).
/// </remarks>
public enum ToastLife
{
    /// <summary>4.5 s - a confirmation.</summary>
    Short,

    /// <summary>9 s - an error worth re-reading.</summary>
    Error,

    /// <summary>30 s - an offer the user has to weigh.</summary>
    Long,

    /// <summary>Until dismissed - a question with no default.</summary>
    Never,
}

/// <summary>One button on a toast.</summary>
public sealed class ToastActionVm
{
    public ToastActionVm(string label, Action run, bool primary = false)
    {
        Label = label;
        Primary = primary;
        Command = new RelayCommand(run);
    }

    public string Label { get; }

    public bool Primary { get; }

    public RelayCommand Command { get; }
}

/// <summary>
/// A transient message, optionally with controls of its own.
/// </summary>
public sealed class ToastVm : ViewModelBase
{
    private readonly DispatcherTimer? _life;
    private ChoiceVm? _selected;

    public ToastVm(string severity, string title, string message, ToastLife life,
                   Action<ToastVm> close)
    {
        Severity = severity;
        Title = title;
        Message = message;
        Life = life;
        DismissCommand = new RelayCommand(() => close(this));

        if (life == ToastLife.Never)
        {
            return;
        }

        _life = new DispatcherTimer { Interval = Duration(life) };
        _life.Tick += (_, _) =>
        {
            _life.Stop();
            close(this);
        };
        _life.Start();
    }

    public string Severity { get; }

    public string Title { get; }

    public string Message { get; }

    public ToastLife Life { get; }

    public RelayCommand DismissCommand { get; }

    public ObservableCollection<ToastActionVm> Actions { get; } = new();

    public bool HasActions => Actions.Count > 0;

    /// <summary>
    /// An inline dropdown, for a toast that offers a choice rather than a
    /// yes/no - the model suggestion picks WHICH smaller model.
    /// </summary>
    public ObservableCollection<ChoiceVm> Options { get; } = new();

    public bool HasOptions => Options.Count > 0;

    public ChoiceVm? SelectedOption
    {
        get => _selected;
        set => Set(ref _selected, value);
    }

    /// <summary>
    /// Stop the countdown - called when the user starts interacting, so a
    /// 30 s offer does not expire mid-decision.
    /// </summary>
    public void HoldOpen() => _life?.Stop();

    /// <summary>Called by the host when the toast is removed.</summary>
    public void Cancel() => _life?.Stop();

    private static TimeSpan Duration(ToastLife life)
        => life switch
        {
            ToastLife.Short => TimeSpan.FromSeconds(4.5),
            ToastLife.Error => TimeSpan.FromSeconds(9),
            ToastLife.Long => TimeSpan.FromSeconds(30),
            _ => TimeSpan.MaxValue,
        };
}
