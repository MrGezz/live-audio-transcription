using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.Json;
using LiveTranscription.Ui.Bridge;
using LiveTranscription.Ui.ViewModels;

namespace LiveTranscription.Ui.Settings;

/// <summary>
/// One row of the settings pane, built from one entry of
/// <c>settings.schema_json()["fields"]</c>.
/// </summary>
/// <remarks>
/// <para>
/// Nothing about any particular setting is written down here or in the XAML.
/// The pane is every field settings.py declares, across 8 groups, and every
/// one of them is this class
/// with different strings in it, which is the whole point: adding a
/// <c>Field(...)</c> to settings.py has to make a control appear with no C#
/// and no XAML edit, or the generated pane is not generated at all - it is
/// hand-written with extra steps.
/// </para>
/// <para>
/// The authoritative value is the <see cref="JsonElement"/>, not the typed
/// properties. The typed properties are projections for binding, because a
/// ToggleSwitch needs a bool and a TextBox needs a string; round-tripping the
/// JSON through them would quietly retype a value - an int field edited
/// through a double-valued control and sent back as 4.0 where the engine
/// wants 4.
/// </para>
/// </remarks>
public sealed class FieldVm : ViewModelBase
{
    private JsonElement _value;
    private JsonElement _lastGood;
    private bool _hydrating;
    private bool _visible = true;
    private bool _matchesSearch = true;
    private string _error = "";

    public FieldVm(Doc schema)
    {
        Key = schema["key"].Str();
        Kind = schema["kind"].Str("str");
        Group = schema["group"].Str();
        Label = schema["label"].Str(Key);
        Help = schema["help"].Str();
        Unit = schema["unit"].Str();
        Placeholder = schema["placeholder"].Str();
        Cli = schema["cli"].Str();
        Rebuild = schema["rebuild"].Str("none");
        Advanced = schema["advanced"].Bool();
        Min = schema["min"].NumOrNull();
        Max = schema["max"].NumOrNull();
        Step = schema["step"].NumOrNull();
        Default = schema["default"].Element;

        ShowIf = ReadShowIf(schema["showIf"]);

        // "choices" is either a literal list of {value,label} or a STRING
        // naming a source that is only known at run time - "devices" today.
        // A literal list is frozen here; a dynamic one is filled by
        // SettingsVm every time the engine sends a fresh list.
        Doc choices = schema["choices"];
        if (choices.Kind == JsonValueKind.String)
        {
            ChoiceSource = choices.Str();
        }
        else if (choices.Kind == JsonValueKind.Array)
        {
            foreach (Doc c in choices.Items())
            {
                Choices.Add(new ChoiceVm(c["value"].Str(),
                                         c["label"].Str(c["value"].Str())));
            }
        }

        _value = Default;
        _lastGood = Default;
    }

    // ---- schema, fixed for the life of the panel -------------------------

    public string Key { get; }
    public string Kind { get; }
    public string Group { get; }
    public string Label { get; }
    public string Help { get; }
    public string Unit { get; }
    public string Placeholder { get; }
    public string Cli { get; }
    public string Rebuild { get; }
    public bool Advanced { get; }
    public double? Min { get; }
    public double? Max { get; }
    public double? Step { get; }
    public JsonElement Default { get; }
    public IReadOnlyDictionary<string, JsonElement[]> ShowIf { get; }

    /// <summary>
    /// "devices", "languages" or "ggml_models", or "" when the choices are a
    /// literal list. Not an enum on purpose - the string comes from the Python
    /// schema, and each UI resolves the ones it knows.
    /// </summary>
    public string ChoiceSource { get; } = "";

    /// <summary>
    /// One of settings.REMOTE_LOCKED - a field the browser panel is not
    /// allowed to move.
    /// </summary>
    /// <remarks>
    /// It stays EDITABLE here. That rule is enforced by transport, not by
    /// locality: those settings are stripped from browser patches - a listener
    /// should not be reconfigurable through itself, and a path a browser names
    /// should not become a fetch, a native parser's input or a file write -
    /// while this panel is local by construction. A desktop panel forbidden
    /// from configuring the listener, or from pointing the model at a folder
    /// on its own disk, would be obeying a rule written for a different
    /// threat. The flag exists only so the row can SAY that it is one of them,
    /// which is otherwise invisible and surprising when the same field greys
    /// out in the other panel.
    /// </remarks>
    public bool LocalOnly { get; set; }

    public ObservableCollection<ChoiceVm> Choices { get; } = new();

    /// <summary>Raised when the USER moved this field. Not on hydrate.</summary>
    public event Action<FieldVm>? Edited;

    // ---- the value -------------------------------------------------------

    public JsonElement Value => _value;

    /// <summary>
    /// Take a value from the engine. Never raises <see cref="Edited"/> - that
    /// would send the value straight back and, with two panels open, ping-pong
    /// a patch between them forever.
    /// </summary>
    public void Hydrate(JsonElement value)
    {
        _hydrating = true;
        try
        {
            _value = value;
            _lastGood = value;
            Error = "";
            RaiseValueProjections();
        }
        finally
        {
            _hydrating = false;
        }
    }

    /// <summary>
    /// The engine refused this edit. Put back the last value it accepted.
    /// </summary>
    /// <remarks>
    /// Needed because <c>Pipeline.apply</c> returns early with
    /// <c>({}, errors)</c> when a patch changes nothing - so a rejected edit
    /// produces NO settings event, and a panel that waits for the echo sits
    /// there showing a value the engine never took. The browser panel has
    /// exactly that hole: webui/app.js:163 refreshes only on the echo.
    /// </remarks>
    public void SnapBack(string error)
    {
        _hydrating = true;
        try
        {
            _value = _lastGood;
            Error = error;
            RaiseValueProjections();
        }
        finally
        {
            _hydrating = false;
        }
    }

    private void Commit(JsonElement next)
    {
        if (_hydrating || ConditionEvaluator.Match(next, _value))
        {
            return;
        }

        _value = next;
        Error = "";
        RaiseValueProjections();
        Edited?.Invoke(this);
    }

    private void RaiseValueProjections()
    {
        Raise(nameof(Value));
        Raise(nameof(BoolValue));
        Raise(nameof(NumberValue));
        Raise(nameof(TextValue));
        Raise(nameof(SelectedChoice));
    }

    // ---- typed projections, one per template ------------------------------

    public bool BoolValue
    {
        get => _value.ValueKind == JsonValueKind.True;
        set => Commit(JsonSerializer.SerializeToElement(value));
    }

    /// <summary>
    /// The numeric projection, shared by the int and float templates.
    /// </summary>
    /// <remarks>
    /// Rounding for an int field happens HERE rather than in the control,
    /// because every numeric control WPF has is double-valued, and 4.0
    /// arriving where settings.py expects an int is coerced by _coerce into
    /// either an error or a silently different value.
    /// </remarks>
    public double NumberValue
    {
        get => _value.ValueKind == JsonValueKind.Number
               && _value.TryGetDouble(out double d)
            ? d
            : 0.0;
        set
        {
            double v = value;
            if (Min is double lo && v < lo)
            {
                v = lo;
            }

            if (Max is double hi && v > hi)
            {
                v = hi;
            }

            Commit(Kind == "int"
                ? JsonSerializer.SerializeToElement((int)Math.Round(v))
                : JsonSerializer.SerializeToElement(v));
        }
    }

    /// <summary>The string projection: str, path, color and choice all use it.</summary>
    public string TextValue
    {
        get => _value.ValueKind == JsonValueKind.String
            ? _value.GetString() ?? ""
            : "";
        set => Commit(JsonSerializer.SerializeToElement(value ?? ""));
    }

    /// <summary>
    /// The dropdown's selected item. Bound as an OBJECT rather than through
    /// SelectedValue/SelectedValuePath so that the "(not available)" entry -
    /// which is a real item whose value is not in the engine's list - stays
    /// selected instead of being cleared on the next refresh.
    /// </summary>
    public ChoiceVm? SelectedChoice
    {
        get
        {
            string current = TextValue;
            foreach (ChoiceVm c in Choices)
            {
                if (string.Equals(c.Value, current, StringComparison.Ordinal))
                {
                    return c;
                }
            }

            return null;
        }

        set
        {
            if (value is not null)
            {
                TextValue = value.Value;
            }
        }
    }

    // ---- presentation ----------------------------------------------------

    /// <summary>Everything under the label: the help, then the CLI flag.</summary>
    public string HelpLine
    {
        get
        {
            if (Cli.Length == 0)
            {
                return Help;
            }

            return Help.Length > 0 ? Help + "   " + Cli : Cli;
        }
    }

    public string UnitSuffix => Unit.Length > 0 ? " " + Unit : "";

    public bool HasUnit => Unit.Length > 0;

    public bool HasHelp => Help.Length > 0 || Cli.Length > 0;

    /// <summary>showIf is satisfied AND the advanced filter allows it.</summary>
    public bool IsVisible
    {
        get => _visible;
        set { if (Set(ref _visible, value)) { Raise(nameof(IsShown)); } }
    }

    public bool MatchesSearch
    {
        get => _matchesSearch;
        set { if (Set(ref _matchesSearch, value)) { Raise(nameof(IsShown)); } }
    }

    public bool IsShown => _visible && _matchesSearch;

    public string Error
    {
        get => _error;
        set { if (Set(ref _error, value ?? "")) { Raise(nameof(HasError)); } }
    }

    public bool HasError => _error.Length > 0;

    /// <summary>Does this field match the search box?</summary>
    public bool Matches(string query)
    {
        if (query.Length == 0)
        {
            return true;
        }

        return Key.Contains(query, StringComparison.OrdinalIgnoreCase)
            || Label.Contains(query, StringComparison.OrdinalIgnoreCase)
            || Help.Contains(query, StringComparison.OrdinalIgnoreCase)
            || Cli.Contains(query, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// Replace the dropdown contents from a live list, keeping the current
    /// value selected even when it is no longer on offer.
    /// </summary>
    public void SetDynamicChoices(IEnumerable<ChoiceVm> fresh)
    {
        string current = TextValue;
        Choices.Clear();
        bool found = false;
        foreach (ChoiceVm c in fresh)
        {
            Choices.Add(c);
            if (string.Equals(c.Value, current, StringComparison.Ordinal))
            {
                found = true;
            }
        }

        if (!found && current.Length > 0)
        {
            Choices.Add(new ChoiceVm(current, current, available: false));
        }

        Raise(nameof(SelectedChoice));
    }

    private static IReadOnlyDictionary<string, JsonElement[]> ReadShowIf(Doc showIf)
    {
        var map = new Dictionary<string, JsonElement[]>(StringComparer.Ordinal);
        foreach (KeyValuePair<string, Doc> cond in showIf.Fields())
        {
            var allowed = new List<JsonElement>();
            foreach (Doc v in cond.Value.Items())
            {
                allowed.Add(v.Element);
            }

            map[cond.Key] = allowed.ToArray();
        }

        return map;
    }

    public override string ToString()
        => string.Format(CultureInfo.InvariantCulture, "{0} ({1}) = {2}",
                         Key, Kind, _value.GetRawText());
}
