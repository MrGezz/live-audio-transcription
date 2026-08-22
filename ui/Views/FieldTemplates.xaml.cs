using System.Collections.Generic;
using System.Windows;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using LiveTranscription.Ui.Settings;
using Microsoft.Win32;

namespace LiveTranscription.Ui.Views;

/// <summary>
/// Code-behind for the field templates: the two things a template cannot do
/// by binding alone - open a file dialog, and close a popup after a pick.
/// </summary>
public partial class FieldTemplates : ResourceDictionary
{
    /// <summary>
    /// The swatch grid. Charcoal neutrals first (the overlay defaults live
    /// there), then one usable step of each hue the web palette knows.
    /// </summary>
    public static IReadOnlyList<string> Palette { get; } = new[]
    {
        "#ffffff", "#ecf4f7", "#cfd8dc", "#8fa6b2",
        "#5f7885", "#37474f", "#263238", "#000000",
        "#ff5252", "#ff7043", "#ffb300", "#ffee58",
        "#9ccc65", "#4caf50", "#26a69a", "#00bcd4",
        "#29b6f6", "#3f51b5", "#7c4dff", "#ab47bc",
        "#f06292", "#795548", "#607d8b", "#0d1417",
    };

    private void OnBrowsePath(object sender, RoutedEventArgs e)
    {
        if ((sender as FrameworkElement)?.DataContext is not FieldVm field)
        {
            return;
        }

        // "output" is a file that will be WRITTEN - it usually does not exist
        // yet, and an open dialog refuses a name that is not there.
        FileDialog dialog = field.Key == "output"
            ? new SaveFileDialog { OverwritePrompt = false }
            : new OpenFileDialog();

        dialog.Title = field.Label;
        dialog.Filter = field.Key switch
        {
            "file_path" => "WAV audio (*.wav)|*.wav|All files (*.*)|*.*",
            "vad_model" => "ONNX model (*.onnx)|*.onnx|All files (*.*)|*.*",
            _ => "All files (*.*)|*.*",
        };
        if (field.TextValue.Length > 0)
        {
            dialog.FileName = field.TextValue;
        }

        if (dialog.ShowDialog() == true)
        {
            field.TextValue = dialog.FileName;
        }
    }

    private void OnSwatchPick(object sender, RoutedEventArgs e)
    {
        if (sender is not FrameworkElement swatch
            || swatch.Tag is not string hex)
        {
            return;
        }

        // The button's DataContext is the palette string; the FieldVm is
        // inherited by the swatch grid from the popup's content. One walk up
        // the visual tree finds both - and the popup is found through the
        // logical Parent of its child, because visually the chain ends at an
        // internal PopupRoot that is the parent of nothing.
        FieldVm? field = null;
        Popup? popup = null;
        DependencyObject? node = swatch;
        while (node is not null && (field is null || popup is null))
        {
            if (node is FrameworkElement fe)
            {
                if (field is null && fe.DataContext is FieldVm f)
                {
                    field = f;
                }

                if (popup is null && fe.Parent is Popup p)
                {
                    popup = p;
                }
            }

            node = VisualTreeHelper.GetParent(node);
        }

        if (field is not null)
        {
            field.TextValue = hex;
        }

        if (popup is not null)
        {
            // The two-way binding un-checks the swatch toggle.
            popup.IsOpen = false;
        }
    }
}
