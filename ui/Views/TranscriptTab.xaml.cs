using System;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using LiveTranscription.Ui.Bridge;
using LiveTranscription.Ui.ViewModels;
using Microsoft.Win32;

namespace LiveTranscription.Ui.Views;

/// <summary>
/// Code-behind for the transcript: the autoscroll contract and the export
/// dialog - the two jobs that need the ScrollViewer and the file system.
/// </summary>
/// <remarks>
/// "Stick to the bottom unless the user has scrolled up" takes two facts -
/// what the user asked for (<see cref="TranscriptVm.AutoScroll"/>) and where
/// the viewport actually is (<see cref="TranscriptVm.StickToBottom"/>).
/// The first is a toggle; the second is maintained here, from scroll events,
/// because only the view knows where the viewport is.
/// </remarks>
public partial class TranscriptTab : UserControl
{
    private TranscriptVm? _wired;

    public TranscriptTab()
    {
        InitializeComponent();
    }

    private MainVm? Vm => DataContext as MainVm;

    private ScrollViewer? Scroller
        => List.Template?.FindName("Scroller", List) as ScrollViewer;

    private void OnDataContextChanged(object sender,
                                      DependencyPropertyChangedEventArgs e)
    {
        if (_wired is not null)
        {
            _wired.ScrollToEndRequested -= OnScrollToEndRequested;
        }

        _wired = (e.NewValue as MainVm)?.Transcript;
        if (_wired is not null)
        {
            _wired.ScrollToEndRequested += OnScrollToEndRequested;
        }
    }

    private void OnScrollToEndRequested()
        // After layout, not now: the request fires from Lines.Add, before the
        // new container exists, and scrolling then lands one line short.
        => Dispatcher.BeginInvoke(
            new Action(() => Scroller?.ScrollToEnd()),
            DispatcherPriority.Background);

    private void OnScrollChanged(object sender, ScrollChangedEventArgs e)
    {
        if (_wired is null || sender is not ScrollViewer sv)
        {
            return;
        }

        // Only a scroll the USER made may move the flag. Content growing
        // (ExtentHeightChange != 0) fires this too, and letting those set the
        // flag would re-stick the view the moment a caption arrived - the
        // exact yank-to-bottom this two-flag design exists to stop.
        if (e.ExtentHeightChange != 0)
        {
            return;
        }

        // CanContentScroll means these are item units, not pixels; at the
        // bottom offset + viewport equals extent exactly, so one unit of
        // slack is "close enough to count as the bottom" in both modes.
        _wired.StickToBottom =
            sv.VerticalOffset + sv.ViewportHeight >= sv.ExtentHeight - 1.0;
    }

    private void OnExport(object sender, RoutedEventArgs e)
    {
        if (Vm is null || (sender as FrameworkElement)?.Tag is not string format)
        {
            return;
        }

        Doc payload = Vm.Export(format);
        if (payload["count"].Int() == 0)
        {
            Vm.Toast("Warning", "Nothing to export yet",
                     "The transcript is empty.");
            return;
        }

        var dialog = new SaveFileDialog
        {
            FileName = payload["filename"].Str("transcript." + format),
            Filter = "Transcript (*." + format + ")|*." + format
                     + "|All files (*.*)|*.*",
        };
        if (dialog.ShowDialog() != true)
        {
            return;
        }

        try
        {
            File.WriteAllText(dialog.FileName, payload["content"].Str());
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            Vm.Toast("Error", "Could not write the file", ex.Message);
            return;
        }

        Vm.Toast("Success", "Exported",
                 payload["count"].Int() + " captions as " + format + " -> "
                 + dialog.FileName);
    }
}
