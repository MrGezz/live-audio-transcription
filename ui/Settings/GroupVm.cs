using System.Collections.ObjectModel;
using LiveTranscription.Ui.ViewModels;

namespace LiveTranscription.Ui.Settings;

/// <summary>
/// One collapsible section of the settings pane - one entry of
/// <c>settings.schema_json()["groups"]</c>.
/// </summary>
public sealed class GroupVm : ViewModelBase
{
    private bool _expanded;
    private bool _isShown = true;
    private int _shownCount;

    public GroupVm(string key, string label, string help, bool expanded)
    {
        Key = key;
        Label = label;
        Help = help;
        _expanded = expanded;
    }

    public string Key { get; }

    public string Label { get; }

    public string Help { get; }

    public ObservableCollection<FieldVm> Fields { get; } = new();

    public bool IsExpanded
    {
        get => _expanded;
        set => Set(ref _expanded, value);
    }

    /// <summary>
    /// Hidden entirely when the filter leaves it with no visible fields.
    /// </summary>
    /// <remarks>
    /// Without this, searching for "overlay" leaves seven empty group headers
    /// above the one that matched, and the pane reads as though the search
    /// found nothing.
    /// </remarks>
    public bool IsShown
    {
        get => _isShown;
        set => Set(ref _isShown, value);
    }

    public int ShownCount
    {
        get => _shownCount;
        set { if (Set(ref _shownCount, value)) { Raise(nameof(CountLabel)); } }
    }

    public string CountLabel => _shownCount == Fields.Count
        ? string.Format("{0} settings", Fields.Count)
        : string.Format("{0} of {1} settings", _shownCount, Fields.Count);
}
