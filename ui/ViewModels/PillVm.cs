namespace LiveTranscription.Ui.ViewModels;

/// <summary>
/// One status pill: a short value, a tone that colours it, and the sentence
/// behind it.
/// </summary>
/// <remarks>
/// There are four - backend, source, real-time factor, language - and they are
/// not decoration. Each one answers a different "why is nothing happening?":
/// no backend, a dead capture device, inference falling behind, and
/// auto-detect wandering between languages. A single combined status line
/// cannot say which, which is the state the panel exists to end.
/// </remarks>
public sealed class PillVm : ViewModelBase
{
    private string _text = "-";
    private string _tone = "";
    private string _tip = "";

    public PillVm(string label)
    {
        Label = label;
    }

    public string Label { get; }

    public string Text
    {
        get => _text;
        set => Set(ref _text, value);
    }

    /// <summary>"" | good | bad | warn | gpu | cpu - drives the brush.</summary>
    public string Tone
    {
        get => _tone;
        set => Set(ref _tone, value);
    }

    public string Tip
    {
        get => _tip;
        set => Set(ref _tip, value);
    }

    public void Set(string text, string tone, string tip)
    {
        Text = string.IsNullOrEmpty(text) ? "-" : text;
        Tone = tone;
        Tip = tip;
    }
}
