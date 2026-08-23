using System.Windows.Controls;

namespace LiveTranscription.Ui.Views;

/// <summary>
/// Code-behind for EngineTab.xaml. It exists for exactly one reason: a XAML
/// file with an x:Class gets its InitializeComponent() generated, but nothing
/// CALLS it - the implicit constructor of a partial class with no other half
/// does nothing. The control then builds, instantiates and renders as an empty
/// UserControl with no error anywhere: the BAML was compiled into the assembly,
/// just never loaded. That was the blank Engine tab the first visual smoke on
/// real hardware caught.
/// </summary>
public partial class EngineTab : UserControl
{
    public EngineTab()
    {
        InitializeComponent();
    }
}
