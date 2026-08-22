using System.Windows;
using System.Windows.Input;
using LiveTranscription.Ui.ViewModels;
using Wpf.Ui.Controls;

namespace LiveTranscription.Ui.Views;

public partial class MainWindow : FluentWindow
{
    public MainWindow()
    {
        InitializeComponent();
    }

    private void Toast_MouseEnter(object sender, MouseEventArgs e)
        // Interacting with a toast stops its countdown, so a 30 s offer does
        // not expire while the user is reading the model list it carries.
        => ((sender as FrameworkElement)?.DataContext as ToastVm)?.HoldOpen();
}
