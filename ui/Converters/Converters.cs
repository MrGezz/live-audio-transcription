using System;
using System.Globalization;
using System.Windows;
using System.Windows.Data;
using System.Windows.Media;
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

/// <summary>
/// 0..1 as a star <see cref="GridLength"/>, so a meter bar can be drawn as two
/// star-sized columns instead of pixel arithmetic against ActualWidth.
/// </summary>
/// <remarks>
/// Pass "rest" as the parameter for the complement column. The fill and its
/// remainder always sum to 1*, so the bar tracks the track's width through
/// every resize with no code-behind and no size-changed handler - which is
/// what keeps a 30 Hz meter update from also being a 30 Hz layout
/// measurement.
/// </remarks>
public sealed class StarWidthConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter,
                          CultureInfo culture)
    {
        double v = value is double d && !double.IsNaN(d) ? d : 0.0;
        v = Math.Min(1.0, Math.Max(0.0, v));
        if (string.Equals(parameter as string, "rest", StringComparison.OrdinalIgnoreCase))
        {
            v = 1.0 - v;
        }

        return new GridLength(v, GridUnitType.Star);
    }

    public object ConvertBack(object? value, Type targetType, object? parameter,
                              CultureInfo culture)
        => throw new NotSupportedException();
}

/// <summary>
/// "#rrggbb" text as a brush, for the colour field's live swatch. Anything
/// unparseable renders transparent rather than throwing - the text box is
/// being typed into, and "#ff" is a state every valid colour passes through.
/// </summary>
public sealed class ColorTextConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter,
                          CultureInfo culture)
    {
        try
        {
            if (ColorConverter.ConvertFromString(value as string) is Color c)
            {
                return new SolidColorBrush(c);
            }
        }
        catch (FormatException)
        {
        }

        return Brushes.Transparent;
    }

    public object ConvertBack(object? value, Type targetType, object? parameter,
                              CultureInfo culture)
        => throw new NotSupportedException();
}
