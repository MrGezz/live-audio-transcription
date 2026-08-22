using System.Windows;
using System.Windows.Controls;
using LiveTranscription.Ui.Settings;

namespace LiveTranscription.Ui.Views;

/// <summary>
/// Code-behind for the settings tab: the preset buttons and the reset
/// confirmation - the parts that need the combo box's free text or a modal.
/// </summary>
public partial class SettingsTab : UserControl
{
    public SettingsTab()
    {
        InitializeComponent();
    }

    private SettingsVm? Vm => DataContext as SettingsVm;

    private string PresetName => (PresetBox.Text ?? "").Trim();

    private void OnPresetLoad(object sender, RoutedEventArgs e)
        => Vm?.LoadPreset(PresetName);

    private void OnPresetSave(object sender, RoutedEventArgs e)
        => Vm?.SavePreset(PresetName);

    private void OnPresetDelete(object sender, RoutedEventArgs e)
    {
        if (Vm is null || PresetName.Length == 0)
        {
            return;
        }

        MessageBoxResult sure = MessageBox.Show(
            "Delete preset \"" + PresetName + "\"?", "Delete preset",
            MessageBoxButton.OKCancel, MessageBoxImage.Question);
        if (sure == MessageBoxResult.OK)
        {
            Vm.DeletePreset(PresetName);
            PresetBox.Text = "";
        }
    }

    private void OnDefaults(object sender, RoutedEventArgs e)
    {
        if (Vm is null)
        {
            return;
        }

        MessageBoxResult sure = MessageBox.Show(
            "Reset every setting to its default?", "Reset settings",
            MessageBoxButton.OKCancel, MessageBoxImage.Question);
        if (sure == MessageBoxResult.OK)
        {
            Vm.ResetToDefaults();
        }
    }
}
