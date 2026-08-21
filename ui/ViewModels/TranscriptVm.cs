using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using LiveTranscription.Ui.Bridge;

namespace LiveTranscription.Ui.ViewModels;

/// <summary>One word, with the confidence band that colours it.</summary>
public sealed class WordVm
{
    public WordVm(string text, double? probability, double warnBelow)
    {
        Text = text;
        Probability = probability;

        // The same three bands the browser panel uses (webui/app.js:788), so
        // a word that reads as doubtful in one front end reads as doubtful in
        // the other. "mid" is the midpoint between the warn threshold and 1.
        Band = probability is not double p
            ? "none"
            : p < warnBelow ? "low"
            : p < (warnBelow + 1.0) / 2.0 ? "mid"
            : "hi";
    }

    public string Text { get; }

    public double? Probability { get; }

    /// <summary>none | low | mid | hi</summary>
    public string Band { get; }

    public string Tip => Probability is double p
        ? string.Format(CultureInfo.InvariantCulture, "confidence {0:0}%", p * 100)
        : "";
}

/// <summary>One caption: a times column, a language tag and the words.</summary>
public sealed class TranscriptLineVm
{
    public TranscriptLineVm(Doc entry, double warnBelow)
    {
        Id = entry["id"].Int();
        Text = entry["text"].Str();
        Language = entry["language"].Str("??");
        Translated = entry["translated"].Bool();
        Backend = entry["backend"].Str();

        double tStart = entry["t_start"].Num();
        double duration = entry["duration"].Num();
        double processing = entry["processing_time"].Num();
        double wall = entry["wall"].Num();

        Clock = FormatClock(tStart);
        WallTime = wall > 0
            ? DateTimeOffset.FromUnixTimeMilliseconds((long)(wall * 1000))
                            .ToLocalTime().ToString("HH:mm:ss", CultureInfo.InvariantCulture)
            : "";

        Tip = string.Format(CultureInfo.InvariantCulture,
                            "at {0} into the session  ·  {1:0.##} s window  ·  {2:0.##} s to transcribe",
                            Clock, duration, processing);

        var words = new List<WordVm>();
        foreach (Doc w in entry["words"].Items())
        {
            string text = w["word"].Str();
            if (text.Length > 0)
            {
                words.Add(new WordVm(text, w["probability"].NumOrNull(), warnBelow));
            }
        }

        // A backend that returns no word timings at all - and the server path
        // does unless word_timestamps is on - still has to render, so the flat
        // text is the fallback rather than an empty line.
        if (words.Count == 0 && Text.Length > 0)
        {
            words.Add(new WordVm(Text, null, warnBelow));
        }

        Words = words;
    }

    public int Id { get; }

    public string Text { get; }

    public string Language { get; }

    public bool Translated { get; }

    public string Backend { get; }

    /// <summary>mm:ss into the session.</summary>
    public string Clock { get; }

    /// <summary>Wall-clock time of day, which is what a meeting is indexed by.</summary>
    public string WallTime { get; }

    public string Tip { get; }

    /// <summary>
    /// Codes on both sides - "ms-EN" for a translation, "ms" otherwise.
    /// </summary>
    /// <remarks>
    /// Deliberately the code and not the language name: under auto-detect the
    /// backend can fall back from the server to the CPU mid-session and the
    /// label must not change when it does.
    /// </remarks>
    public string Tag => Translated ? Language + " -> EN" : Language;

    public IReadOnlyList<WordVm> Words { get; }

    public override string ToString() => Clock + "  " + Text;

    private static string FormatClock(double seconds)
    {
        int s = (int)Math.Max(0, Math.Round(seconds));
        return string.Format(CultureInfo.InvariantCulture, "{0:00}:{1:00}", s / 60, s % 60);
    }
}

/// <summary>
/// The transcript view's data: a bounded list of captions.
/// </summary>
/// <remarks>
/// <para>
/// The list MUST be rendered by a virtualizing panel. A 600-line transcript
/// with per-word confidence is on the order of twelve thousand visual
/// elements, and an ItemsControl does not virtualize by default - its default
/// ItemsPanel is a plain StackPanel, which realises every item whether or not
/// it is on screen. Views/TranscriptTab.xaml sets an explicit
/// VirtualizingStackPanel for that reason; changing it back is not a styling
/// decision.
/// </para>
/// <para>
/// The cap here is higher than the browser panel's 600 (webui/app.js:863)
/// precisely BECAUSE of virtualisation: in the DOM 600 lines is 600 lines of
/// layout, while here an off-screen line is a small object and no visual at
/// all. The engine keeps the full history either way - this is only the view.
/// </para>
/// </remarks>
public sealed class TranscriptVm : ViewModelBase
{
    public const int MaxLines = 2000;

    private bool _autoScroll = true;
    private bool _stickToBottom = true;
    private double _warnBelow = 0.60;
    private int _count;

    public ObservableCollection<TranscriptLineVm> Lines { get; } = new();

    /// <summary>Is the user's preference to follow the tail?</summary>
    public bool AutoScroll
    {
        get => _autoScroll;
        set
        {
            if (Set(ref _autoScroll, value) && value)
            {
                StickToBottom = true;
                ScrollToEndRequested?.Invoke();
            }
        }
    }

    /// <summary>
    /// Is the view actually AT the bottom right now?
    /// </summary>
    /// <remarks>
    /// Separate from <see cref="AutoScroll"/> on purpose. "Stick to the bottom
    /// unless the user has scrolled up" needs two facts, not one: what the
    /// user asked for, and where the viewport is. Collapsing them into a
    /// single flag gives the behaviour everyone has suffered - you scroll back
    /// to re-read a line, the next caption arrives, and you are yanked to the
    /// bottom again.
    /// </remarks>
    public bool StickToBottom
    {
        get => _stickToBottom;
        set => Set(ref _stickToBottom, value);
    }

    public int Count
    {
        get => _count;
        private set { if (Set(ref _count, value)) { Raise(nameof(IsEmpty)); } }
    }

    public bool IsEmpty => _count == 0;

    /// <summary>Raised when the view should scroll to the newest line.</summary>
    public event Action? ScrollToEndRequested;

    /// <summary>The confidence_warn setting, which decides the word colours.</summary>
    public void SetConfidenceWarn(double warnBelow)
    {
        if (warnBelow > 0 && Math.Abs(warnBelow - _warnBelow) > 0.0001)
        {
            _warnBelow = warnBelow;
        }
    }

    public void Add(string entryJson)
    {
        Doc entry = Doc.Parse(entryJson);
        if (!entry.Exists)
        {
            return;
        }

        Lines.Add(new TranscriptLineVm(entry, _warnBelow));
        Trim();
        Count = Lines.Count;

        if (_autoScroll && _stickToBottom)
        {
            ScrollToEndRequested?.Invoke();
        }
    }

    /// <summary>Replace everything - the history document, or after a Clear.</summary>
    public void Replace(string historyJson)
    {
        Lines.Clear();
        foreach (Doc entry in Doc.Parse(historyJson).Items())
        {
            Lines.Add(new TranscriptLineVm(entry, _warnBelow));
        }

        Trim();
        Count = Lines.Count;
        ScrollToEndRequested?.Invoke();
    }

    public void Clear()
    {
        Lines.Clear();
        Count = 0;
    }

    private void Trim()
    {
        while (Lines.Count > MaxLines)
        {
            Lines.RemoveAt(0);
        }
    }
}
