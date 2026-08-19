using System;
using System.Globalization;
using System.Windows;
using System.Windows.Data;
using Wpf.Ui.Controls;

namespace LiveTranscription.Ui.Converters;

/// <summary>
/// Severity arrives from Python as a string ("Error", "Warning", ...), because
/// the engine has no business knowing about a WPF enum. Unknown values fall
/// back to Informational rather than throwing - a banner that renders with the
/// wrong icon still tells the user what happened; one that throws during a
/// layout pass takes the window down.
/// </summary>
public sealed class SeverityConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter,
                          CultureInfo culture)
        => Enum.TryParse(value as string, ignoreCase: true, out InfoBarSeverity s)
            ? s
            : InfoBarSeverity.Informational;

    public object ConvertBack(object? value, Type targetType, object? parameter,
                              CultureInfo culture)
        => value?.ToString() ?? "Informational";
}

/// <summary>true -&gt; Visible, false -&gt; Collapsed. Pass "invert" to flip it.</summary>
public sealed class BoolToVisibilityConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter,
                          CultureInfo culture)
    {
        bool on = value is bool b && b;
        if (string.Equals(parameter as string, "invert", StringComparison.OrdinalIgnoreCase))
        {
            on = !on;
        }

        return on ? Visibility.Visible : Visibility.Collapsed;
    }

    public object ConvertBack(object? value, Type targetType, object? parameter,
                              CultureInfo culture)
        => value is Visibility v && v == Visibility.Visible;
}

/// <summary>Inverts a bool. For IsEnabled bindings that read better positive.</summary>
public sealed class NotConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter,
                          CultureInfo culture)
        => value is not bool b || !b;

    public object ConvertBack(object? value, Type targetType, object? parameter,
                              CultureInfo culture)
        => value is not bool b || !b;
}
