using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using LiveTranscription.Ui.ViewModels;

namespace LiveTranscription.Ui.Views;

public partial class LogTab : UserControl
{
    private LogVm? _wired;

    public LogTab()
    {
        InitializeComponent();
    }

    private void OnDataContextChanged(object sender,
                                      DependencyPropertyChangedEventArgs e)
    {
        if (_wired is not null)
        {
            _wired.ScrollToEndRequested -= OnScrollToEndRequested;
        }

        _wired = e.NewValue as LogVm;
        if (_wired is not null)
        {
            _wired.ScrollToEndRequested += OnScrollToEndRequested;
        }
    }

    private void OnScrollToEndRequested()
        => Dispatcher.BeginInvoke(
            new Action(() =>
            {
                if (List.Template?.FindName("Scroller", List) is ScrollViewer sv)
                {
                    sv.ScrollToEnd();
                }
            }),
            DispatcherPriority.Background);

    private void OnCopy(object sender, RoutedEventArgs e)
    {
        if (_wired is null)
        {
            return;
        }

        try
        {
            Clipboard.SetText(_wired.AsText());
        }
        catch (Exception)
        {
            // The clipboard is a shared resource another process can hold
            // open; failing to copy must not take the window down.
        }
    }

    private void OnClear(object sender, RoutedEventArgs e) => _wired?.Clear();
}
