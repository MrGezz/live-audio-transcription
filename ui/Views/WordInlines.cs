using System.Collections.Generic;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;
using LiveTranscription.Ui.ViewModels;

namespace LiveTranscription.Ui.Views;

/// <summary>
/// Renders a caption's words into a TextBlock as coloured <see cref="Run"/>s.
/// </summary>
/// <remarks>
/// <para>
/// Per-word confidence colouring needs one coloured span per word inside ONE
/// flowing paragraph. The obvious XAML answer - an ItemsControl of words with
/// a WrapPanel - gives a box per word instead of a line of text: the spacing
/// is wrong, the words cannot be selected across, and a long caption wraps
/// like a tag cloud. Runs inside a single TextBlock are the only thing that
/// wraps as prose.
/// </para>
/// <para>
/// Runs cannot be produced by a template, because <c>TextBlock.Inlines</c> is
/// not a dependency property and so cannot be bound. Hence this attached
/// property, which builds them in code when the collection is set.
/// </para>
/// <para>
/// It stays cheap because the rows above and below it are virtualised: this
/// runs only for lines that are actually on screen, and again when a recycled
/// container is handed a different line.
/// </para>
/// </remarks>
public static class WordInlines
{
    public static readonly DependencyProperty WordsProperty =
        DependencyProperty.RegisterAttached(
            "Words",
            typeof(IReadOnlyList<WordVm>),
            typeof(WordInlines),
            new PropertyMetadata(null, OnWordsChanged));

    public static void SetWords(DependencyObject target, IReadOnlyList<WordVm>? value)
        => target.SetValue(WordsProperty, value);

    public static IReadOnlyList<WordVm>? GetWords(DependencyObject target)
        => (IReadOnlyList<WordVm>?)target.GetValue(WordsProperty);

    private static void OnWordsChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is not TextBlock block)
        {
            return;
        }

        block.Inlines.Clear();
        if (e.NewValue is not IReadOnlyList<WordVm> words)
        {
            return;
        }

        foreach (WordVm w in words)
        {
            var run = new Run(w.Text);
            Brush? brush = BandBrush(block, w.Band);
            if (brush is not null)
            {
                run.Foreground = brush;
            }

            block.Inlines.Add(run);
        }
    }

    /// <summary>
    /// The brush for a confidence band, looked up as a resource.
    /// </summary>
    /// <remarks>
    /// DynamicResource cannot be used from code on a Run built like this, so
    /// the lookup is done once per word against the element's resource chain -
    /// which still reaches the merged charcoal dictionary. A missing key
    /// returns null and the word simply inherits the normal foreground, which
    /// is the right failure: an uncoloured transcript is readable, and a
    /// transcript that threw during a layout pass takes the window down.
    /// </remarks>
    private static Brush? BandBrush(FrameworkElement scope, string band)
    {
        if (band == "none")
        {
            return null;
        }

        string key = band switch
        {
            "low" => "ConfidenceLowBrush",
            "mid" => "ConfidenceMidBrush",
            _ => "ConfidenceHighBrush",
        };

        return scope.TryFindResource(key) as Brush;
    }
}
