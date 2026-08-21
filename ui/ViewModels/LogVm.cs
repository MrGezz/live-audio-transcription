using System;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Text;
using LiveTranscription.Ui.Bridge;

namespace LiveTranscription.Ui.ViewModels;

/// <summary>One log line: a clock, a level and the message.</summary>
public sealed class LogLineVm
{
    public LogLineVm(string level, string message)
    {
        Level = string.IsNullOrEmpty(level) ? "info" : level;
        Message = message;
        Time = DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture);
    }

    public string Time { get; }

    /// <summary>info | warn | error</summary>
    public string Level { get; }

    public string Message { get; }

    public override string ToString() => Time + "  " + Level + "  " + Message;
}

/// <summary>
/// The log tab, and the unread badge that points at it.
/// </summary>
/// <remarks>
/// The badge counts warnings and errors only, and only while the tab is not
/// the one on screen. Counting info lines would leave a permanent number on
/// the tab - the pipeline logs every accepted settings change - and a badge
/// that is always lit is a badge nobody reads.
/// </remarks>
public sealed class LogVm : ViewModelBase
{
    /// <summary>Matches the browser panel's cap (webui/app.js:907).</summary>
    public const int MaxLines = 500;

    private readonly Action<int> _reportUnread;
    private bool _isVisible;
    private int _unread;
    private bool _autoScroll = true;

    public LogVm(Action<int> reportUnread)
    {
        _reportUnread = reportUnread;
    }

    public ObservableCollection<LogLineVm> Lines { get; } = new();

    public bool AutoScroll
    {
        get => _autoScroll;
        set => Set(ref _autoScroll, value);
    }

    /// <summary>Raised when the view should scroll to the newest line.</summary>
    public event Action? ScrollToEndRequested;

    /// <summary>Set by the shell when the log tab becomes the selected one.</summary>
    public bool IsVisible
    {
        get => _isVisible;
        set
        {
            if (Set(ref _isVisible, value) && value)
            {
                _unread = 0;
                _reportUnread(0);
                ScrollToEndRequested?.Invoke();
            }
        }
    }

    public void Add(string level, string message)
    {
        Lines.Add(new LogLineVm(level, message));
        while (Lines.Count > MaxLines)
        {
            Lines.RemoveAt(0);
        }

        if (_isVisible)
        {
            if (_autoScroll)
            {
                ScrollToEndRequested?.Invoke();
            }

            return;
        }

        if (level is "warn" or "error")
        {
            _unread++;
            _reportUnread(_unread);
        }
    }

    public void Clear()
    {
        Lines.Clear();
        _unread = 0;
        _reportUnread(0);
    }

    /// <summary>Everything on screen, for the copy button.</summary>
    public string AsText()
    {
        var sb = new StringBuilder();
        foreach (LogLineVm line in Lines)
        {
            sb.Append(line.Time).Append("  [").Append(line.Level).Append("] ")
              .AppendLine(line.Message);
        }

        return sb.ToString();
    }
}
