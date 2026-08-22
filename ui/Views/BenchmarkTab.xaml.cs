using System.Windows;
using System.Windows.Controls;
using LiveTranscription.Ui.ViewModels;
using Microsoft.Win32;

namespace LiveTranscription.Ui.Views;

public partial class BenchmarkTab : UserControl
{
    public BenchmarkTab()
    {
        InitializeComponent();
    }

    private void OnBrowseWav(object sender, RoutedEventArgs e)
    {
        if (DataContext is not BenchmarkVm vm)
        {
            return;
        }

        var dialog = new OpenFileDialog
        {
            Title = "A WAV of real speech to measure with",
            Filter = "WAV audio (*.wav)|*.wav|All files (*.*)|*.*",
        };
        if (dialog.ShowDialog() == true)
        {
            vm.WavPath = dialog.FileName;
        }
    }
}
