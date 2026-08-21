namespace LiveTranscription.Ui.Settings;

/// <summary>
/// One entry in a choice field's dropdown: the value the engine wants, and
/// the label a person reads.
/// </summary>
/// <remarks>
/// <see cref="Available"/> exists for one case, and it is the case that makes
/// device pickers infuriating. A session configured for "Speakers (Realtek)"
/// is reopened with the headset unplugged, so the live value is not in the
/// list any more. Dropping it silently makes the combo box show the FIRST
/// device instead - which is not what the engine is set to, and the panel is
/// now lying about the configuration. Dropping it and showing blank is no
/// better. So the missing value is added back, marked, and left selected:
/// the panel says what is actually configured and says that it is gone.
/// </remarks>
public sealed class ChoiceVm
{
    public ChoiceVm(string value, string label, bool available = true)
    {
        Value = value;
        Label = label;
        Available = available;
    }

    public string Value { get; }

    public string Label { get; }

    public bool Available { get; }

    /// <summary>What the dropdown actually renders.</summary>
    public string Display => Available ? Label : Label + "  (not available)";

    public override string ToString() => Display;
}
