using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Windows.Threading;
using LiveTranscription.Ui.Bridge;
using LiveTranscription.Ui.ViewModels;

namespace LiveTranscription.Ui.Settings;

/// <summary>
/// The whole settings pane: 8 groups, every field settings.py declares,
/// generated from the schema.
/// </summary>
/// <remarks>
/// <para>
/// This class is the reason the View layer is C# rather than Python. pythonnet
/// projects a Python object as a CLR type with zero properties, so
/// <c>{Binding}</c> against one renders an empty string silently - and a pane
/// that is generated entirely by binding cannot be built on top of that.
/// </para>
/// <para>
/// The invariant to protect: adding a <c>Field(...)</c> to settings.py must
/// make a control appear here with no edit to any C# or XAML file. Anything
/// that special-cases a key by name is a hole in that, and there is exactly
/// one such hole - <c>model</c>, see <see cref="SetModels"/> - which is
/// inherited from the browser panel so the two agree.
/// </para>
/// </remarks>
public sealed class SettingsVm : ViewModelBase
{
    /// <summary>
    /// Every kind the templates cover. Checked against the LIVE schema at
    /// startup so an eighth kind fails loudly instead of silently rendering
    /// as a TextBox that writes the wrong JSON type.
    /// </summary>
    public static readonly string[] KnownKinds =
    {
        "bool", "int", "float", "choice", "str", "path", "color",
    };

    /// <summary>
    /// How long after the last keystroke or slider move the patch goes.
    /// </summary>
    /// <remarks>
    /// The browser panel uses 60 ms and ONE shared timer for all fields, which
    /// has the effect that editing two fields inside 60 ms drops the first
    /// edit entirely. Here every edit lands in <see cref="_pending"/> and the
    /// timer only decides when to flush, so a longer window costs nothing and
    /// buys a dragged slider one patch instead of forty.
    /// </remarks>
    private const int DebounceMs = 250;

    private readonly IEngineBridge _bridge;
    private readonly Action<string, string, string> _toast;
    private readonly Dictionary<string, FieldVm> _byKey = new(StringComparer.Ordinal);
    private readonly Dictionary<string, JsonElement> _live = new(StringComparer.Ordinal);
    private readonly Dictionary<string, JsonElement> _pending = new(StringComparer.Ordinal);
    private readonly DispatcherTimer _debounce;

    private IReadOnlyList<ChoiceVm> _languages = Array.Empty<ChoiceVm>();
    private readonly Dictionary<string, JsonElement> _defaults = new(StringComparer.Ordinal);
    private Doc _devices = Doc.None;
    private Doc _models = Doc.None;
    private bool _showAdvanced;
    private string _search = "";
    private string _rebuildNote = "";
    private bool _loaded;

    /// <param name="toast">severity, title, message - shown to the user.</param>
    public SettingsVm(IEngineBridge bridge, Dispatcher dispatcher,
                      Action<string, string, string> toast)
    {
        _bridge = bridge;
        _toast = toast;
        _debounce = new DispatcherTimer(DispatcherPriority.Background, dispatcher)
        {
            Interval = TimeSpan.FromMilliseconds(DebounceMs),
        };
        _debounce.Tick += (_, _) => Flush();
    }

    public ObservableCollection<GroupVm> Groups { get; } = new();

    public ObservableCollection<string> Presets { get; } = new();

    public bool IsLoaded
    {
        get => _loaded;
        private set => Set(ref _loaded, value);
    }

    /// <summary>The 19 advanced fields are hidden until this is on.</summary>
    public bool ShowAdvanced
    {
        get => _showAdvanced;
        set { if (Set(ref _showAdvanced, value)) { Refilter(); } }
    }

    public string Search
    {
        get => _search;
        set { if (Set(ref _search, value ?? "")) { Refilter(); } }
    }

    /// <summary>
    /// What the last edit is about to rebuild, in a sentence, or "".
    /// </summary>
    /// <remarks>
    /// Changing the backend takes seconds to load a CPU model, and changing
    /// the capture device silently re-opens the device. Without a line saying
    /// so, both look like the panel ignoring the click.
    /// </remarks>
    public string RebuildNote
    {
        get => _rebuildNote;
        private set { if (Set(ref _rebuildNote, value ?? "")) { Raise(nameof(HasRebuildNote)); } }
    }

    public bool HasRebuildNote => _rebuildNote.Length > 0;

    // ---- building from the schema ----------------------------------------

    /// <summary>
    /// Build the pane. Called once, with <c>settings.schema_json()</c>.
    /// </summary>
    /// <exception cref="InvalidOperationException">
    /// A field kind that no template covers. Deliberately fatal at startup
    /// rather than a control that renders as the wrong thing: a float that
    /// draws as a TextBox still LOOKS editable, and writes a string into a
    /// setting the engine will coerce or reject at some later, unrelated
    /// moment.
    /// </exception>
    public void LoadSchema(string schemaJson)
    {
        // The promise ConditionEvaluator's remarks make: Match is asserted at
        // startup rather than left to be noticed as twelve fields that never
        // appear. It exercises exactly the comparison that the obvious
        // ToString() implementation gets wrong.
        JsonElement jTrue = JsonSerializer.SerializeToElement(true);
        JsonElement jFile = JsonSerializer.SerializeToElement("file");
        if (!ConditionEvaluator.Match(jTrue, JsonSerializer.SerializeToElement(true))
            || ConditionEvaluator.Match(jTrue, JsonSerializer.SerializeToElement(false))
            || !ConditionEvaluator.Match(jFile, JsonSerializer.SerializeToElement("file"))
            || !ConditionEvaluator.Match(JsonSerializer.SerializeToElement(1),
                                         JsonSerializer.SerializeToElement(1.0)))
        {
            throw new InvalidOperationException(
                "ConditionEvaluator.Match no longer compares JSON values by "
                + "kind - every boolean-gated showIf would evaluate hidden.");
        }

        Doc schema = Doc.Parse(schemaJson);
        if (!schema.Exists)
        {
            _toast("Error", "Settings unavailable",
                   "The engine did not return a readable settings schema.");
            return;
        }

        var locked = new HashSet<string>(StringComparer.Ordinal);
        foreach (Doc k in schema["remoteLocked"].Items())
        {
            locked.Add(k.Str());
        }

        var languages = new List<ChoiceVm>();
        foreach (Doc l in schema["languages"].Items())
        {
            languages.Add(new ChoiceVm(l["value"].Str(), l["label"].Str()));
        }

        _languages = languages;

        _defaults.Clear();
        foreach (KeyValuePair<string, Doc> kv in schema["defaults"].Fields())
        {
            _defaults[kv.Key] = kv.Value.Element;
        }

        var fields = new List<FieldVm>();
        var unknown = new List<string>();
        foreach (Doc f in schema["fields"].Items())
        {
            var vm = new FieldVm(f);
            if (Array.IndexOf(KnownKinds, vm.Kind) < 0)
            {
                unknown.Add(vm.Key + " (" + vm.Kind + ")");
                continue;
            }

            vm.LocalOnly = locked.Contains(vm.Key);
            vm.Edited += OnFieldEdited;
            fields.Add(vm);
            _byKey[vm.Key] = vm;
        }

        if (unknown.Count > 0)
        {
            throw new InvalidOperationException(
                "settings.py has field kinds this panel has no template for: "
                + string.Join(", ", unknown)
                + ". Add a DataTemplate to Views/FieldTemplates.xaml, a case to "
                + "FieldTemplateSelector, and the kind to SettingsVm.KnownKinds.");
        }

        Groups.Clear();
        int index = 0;
        foreach (Doc g in schema["groups"].Items())
        {
            // The first four open, like the browser panel: the audio, chunking,
            // gate and transcription groups are the ones anyone actually
            // touches, and eight expanded groups is a page of scrolling before
            // the first control.
            var group = new GroupVm(g["key"].Str(), g["label"].Str(),
                                    g["help"].Str(), expanded: index < 4);
            foreach (FieldVm f in fields)
            {
                if (string.Equals(f.Group, group.Key, StringComparison.Ordinal))
                {
                    group.Fields.Add(f);
                }
            }

            if (group.Fields.Count > 0)
            {
                Groups.Add(group);
            }

            index++;
        }

        IsLoaded = true;
        Refilter();
    }

    // ---- values from the engine -------------------------------------------

    /// <summary>Take the full settings document and re-hydrate every field.</summary>
    public void HydrateSettings(string settingsJson)
    {
        Doc doc = Doc.Parse(settingsJson);
        if (!doc.Exists)
        {
            return;
        }

        foreach (KeyValuePair<string, Doc> kv in doc.Fields())
        {
            _live[kv.Key] = kv.Value.Element;
            if (_byKey.TryGetValue(kv.Key, out FieldVm? f))
            {
                f.Hydrate(kv.Value.Element);
            }
        }

        // capture drives which device list the device field offers, so a
        // settings echo can change the CONTENTS of a dropdown, not just its
        // selection.
        RefreshDeviceChoices();
        Refilter();
    }

    /// <summary>
    /// The result of an <c>ApplySettings</c> call: <c>{changed, errors}</c>.
    /// </summary>
    /// <remarks>
    /// <para>
    /// <c>errors</c> is a flat list of human sentences, NOT keyed by field -
    /// settings.validate builds them with the values already interpolated in.
    /// So a rejected field cannot be identified from the error text; it is
    /// identified by ABSENCE, as a key that was sent and did not come back in
    /// <c>changed</c>.
    /// </para>
    /// <para>
    /// The absence test is safe against the case where a field was set to the
    /// value it already held - <c>Pipeline.apply</c> filters those out of
    /// <c>changed</c> too, but snapping such a field back to its last good
    /// value is a no-op by definition.
    /// </para>
    /// <para>
    /// <c>changed</c> can also contain keys that were never sent:
    /// settings.validate turns word_timestamps on by itself when the format
    /// becomes srt or vtt. Those are applied here, so the checkbox ticks
    /// itself and the user can see it happen.
    /// </para>
    /// </remarks>
    public void ApplyAck(string ackJson)
    {
        Doc ack = Doc.Parse(ackJson);
        Doc changed = ack["changed"];

        var accepted = new HashSet<string>(StringComparer.Ordinal);
        foreach (KeyValuePair<string, Doc> kv in changed.Fields())
        {
            accepted.Add(kv.Key);
            _live[kv.Key] = kv.Value.Element;
            if (_byKey.TryGetValue(kv.Key, out FieldVm? f))
            {
                f.Hydrate(kv.Value.Element);
            }
        }

        var messages = new List<string>();
        foreach (Doc e in ack["errors"].Items())
        {
            messages.Add(e.Str());
        }

        if (messages.Count > 0)
        {
            foreach (string key in _sentKeys)
            {
                if (!accepted.Contains(key)
                    && _byKey.TryGetValue(key, out FieldVm? f))
                {
                    f.SnapBack(messages[0]);
                }
            }

            _toast("Error",
                   messages.Count == 1 ? "That setting was refused"
                                       : messages.Count + " settings were refused",
                   string.Join("\n", messages));
        }

        _sentKeys.Clear();
        Refilter();
    }

    /// <summary>Fill the device dropdown from a fresh enumeration.</summary>
    public void SetDevices(string devicesJson)
    {
        _devices = Doc.Parse(devicesJson);
        RefreshDeviceChoices();
    }

    /// <summary>
    /// Fill both model lists: faster-whisper folders and GGML files.
    /// </summary>
    /// <remarks>
    /// <para>
    /// <c>model</c> is declared in settings.py as a <c>path</c>, because on the
    /// command line it is one. In a panel it is a picker over what is actually
    /// in _models, and this is the one place a key is special-cased by name.
    /// webui/app.js does the same thing for the same reason; the two panels
    /// disagreeing about what the model field IS would be worse than the
    /// special case.
    /// </para>
    /// <para>
    /// <c>server_model</c> needs no such exception: it is declared a
    /// <c>choice</c> with the dynamic source <c>ggml_models</c>, so it takes
    /// the choice template on its own and is found here by that source rather
    /// than by name. New dynamic lists should follow it, not <c>model</c>.
    /// </para>
    /// </remarks>
    public void SetModels(string modelsJson)
    {
        _models = Doc.Parse(modelsJson);

        if (_byKey.TryGetValue("model", out FieldVm? model))
        {
            var folders = new List<ChoiceVm>();
            foreach (Doc m in _models["faster_whisper"].Items())
            {
                folders.Add(new ChoiceVm(m["path"].Str(), m["name"].Str(m["path"].Str())));
            }

            model.SetDynamicChoices(folders);
        }

        FieldVm? serverModel = FieldWithSource("ggml_models");
        if (serverModel is not null)
        {
            var files = new List<ChoiceVm>();
            foreach (Doc m in _models["ggml"].Items())
            {
                string name = m["name"].Str();
                double size = m["size_mb"].Num();

                // The size is in the label because for this one setting the
                // number IS the decision: when the perf line says the GPU
                // cannot hold real time, a smaller file is the fix.
                files.Add(new ChoiceVm(
                    name,
                    size > 0
                        ? string.Format(CultureInfo.InvariantCulture, "{0}   ({1:0} MB)", name, size)
                        : name));
            }

            // SetDynamicChoices re-adds the current value as unavailable if it
            // is not in the list, which is what keeps a remembered model that
            // has since been deleted visible rather than silently swapped for
            // whichever file happens to sort first.
            serverModel.SetDynamicChoices(files);
        }
    }

    /// <summary>The first field declaring <paramref name="source"/>, or null.</summary>
    private FieldVm? FieldWithSource(string source)
    {
        foreach (FieldVm f in _byKey.Values)
        {
            if (string.Equals(f.ChoiceSource, source, StringComparison.Ordinal))
            {
                return f;
            }
        }

        return null;
    }

    public void SetPresets(string presetsJson)
    {
        Doc doc = Doc.Parse(presetsJson);

        // Tolerated in two shapes because _list_presets returns a bare list
        // and the preset acks carry it under a "presets" key. Anything else
        // - an object without the key, a null, text that will not parse -
        // says nothing about the list, so the list is left as it is.
        // Clearing first and reading second emptied the dropdown on any ack
        // that forgot the key, which is the one gesture the shipped profiles
        // exist for. wpf_panel.py's _preset_ack now guarantees the key on
        // every ack; this is the panel not depending on that.
        Doc list = doc.Kind == JsonValueKind.Array ? doc : doc["presets"];
        if (list.Kind != JsonValueKind.Array)
        {
            return;
        }

        var names = new List<string>();
        foreach (Doc p in list.Items())
        {
            names.Add(p.Kind == JsonValueKind.String ? p.Str() : p["name"].Str());
        }

        // Edited into shape, never Clear()ed and refilled. PresetBox is an
        // EDITABLE ComboBox bound to this collection, and Clear() raises a
        // CollectionChanged Reset, which WPF answers by blanking the box's
        // Text. app.py rides the preset list on every ack precisely so a
        // refusal cannot empty the dropdown - so every successful Load, of a
        // list that a Load cannot possibly have changed, threw the selection
        // away the moment it worked. OnPresetDelete setting PresetBox.Text
        // to "" by hand is the evidence that the box was always meant to
        // keep its text otherwise.
        //
        // Removals first, then insertions at the position app.py sent, so
        // the result is that order without a Reset anywhere. Only the entry
        // actually removed loses a selection, which is what deleting the
        // selected preset should do.
        for (int i = Presets.Count - 1; i >= 0; i--)
        {
            if (!names.Contains(Presets[i]))
            {
                Presets.RemoveAt(i);
            }
        }

        for (int i = 0; i < names.Count; i++)
        {
            if (i >= Presets.Count)
            {
                Presets.Add(names[i]);
            }
            else if (!string.Equals(Presets[i], names[i], StringComparison.Ordinal))
            {
                Presets.Insert(i, names[i]);
            }
        }
    }

    private void RefreshDeviceChoices()
    {
        foreach (FieldVm f in _byKey.Values)
        {
            if (!string.Equals(f.ChoiceSource, "devices", StringComparison.Ordinal))
            {
                continue;
            }

            f.SetDynamicChoices(DeviceChoices());
        }

        foreach (FieldVm f in _byKey.Values)
        {
            if (string.Equals(f.ChoiceSource, "languages", StringComparison.Ordinal)
                && f.Choices.Count == 0)
            {
                f.SetDynamicChoices(_languages);
            }
        }
    }

    /// <summary>
    /// The device list for the CURRENT capture mode.
    /// </summary>
    /// <remarks>
    /// Loopback devices are outputs and input devices are inputs; offering
    /// both at once produces a list in which half the entries cannot work and
    /// nothing says which half. Mirrors webui/app.js:366 choicesFor.
    /// </remarks>
    private List<ChoiceVm> DeviceChoices()
    {
        string mode = _live.TryGetValue("capture", out JsonElement c)
                      && c.ValueKind == JsonValueKind.String
            ? c.GetString() ?? "loopback"
            : "loopback";

        var list = new List<ChoiceVm>();
        switch (mode)
        {
            case "loopback":
                list.Add(new ChoiceVm("", "(system default)"));
                foreach (Doc d in _devices["loopback"].Items())
                {
                    string tail = d["default"].Bool() ? "   <- Windows default" : "";
                    list.Add(new ChoiceVm(d["id"].Str(), d["name"].Str() + tail));
                }

                break;

            case "input":
                list.Add(new ChoiceVm("", "(system default)"));
                foreach (Doc d in _devices["input"].Items())
                {
                    list.Add(new ChoiceVm(
                        d["id"].Str(),
                        string.Format(CultureInfo.InvariantCulture, "{0}: {1} ({2}, {3} Hz)",
                                      d["id"].Str(), d["name"].Str(),
                                      d["hostapi"].Str(), d["samplerate"].Int())));
                }

                break;

            case "browser":
                list.Add(new ChoiceVm("", "(streamed into the panel)"));
                break;

            default:
                list.Add(new ChoiceVm("", "(not used for a file)"));
                break;
        }

        return list;
    }

    // ---- edits going out ---------------------------------------------------

    private readonly List<string> _sentKeys = new();

    private void OnFieldEdited(FieldVm field)
    {
        _pending[field.Key] = field.Value;
        RebuildNote = DescribeRebuild(field);

        // Visibility can depend on the field just moved - unticking "vad"
        // hides five fields - so the gate is re-evaluated on the edit rather
        // than waiting for the echo to come back from the engine.
        _live[field.Key] = field.Value;
        Refilter();

        _debounce.Stop();
        _debounce.Start();
    }

    /// <summary>
    /// Send everything that has been edited, in ONE call.
    /// </summary>
    /// <remarks>
    /// Not one call per key, and this is not an optimisation.
    /// settings.validate runs the cross-field rules against the whole merged
    /// patch: <c>slide</c> may not exceed <c>buffer</c>, <c>chunk_offset</c>
    /// must be under <c>chunk_length</c>, <c>chunk_max_length</c> may not be
    /// under <c>chunk_length</c>. Sending buffer and slide separately means
    /// whichever arrives first is validated against the OLD value of the other
    /// and rejected, so a legal pair of values cannot be entered at all.
    /// </remarks>
    private void Flush()
    {
        _debounce.Stop();
        if (_pending.Count == 0)
        {
            return;
        }

        var sb = new StringBuilder("{");
        bool first = true;
        _sentKeys.Clear();
        foreach (KeyValuePair<string, JsonElement> kv in _pending)
        {
            if (!first)
            {
                sb.Append(',');
            }

            sb.Append(JsonSerializer.Serialize(kv.Key))
              .Append(':')
              .Append(kv.Value.GetRawText());
            _sentKeys.Add(kv.Key);
            first = false;
        }

        sb.Append('}');
        _pending.Clear();

        // ApplySettings returns the ack synchronously - it is a direct call
        // into Pipeline.apply, not a round trip - so there is no correlation
        // problem and _sentKeys is still the right set when it comes back.
        ApplyAck(_bridge.ApplySettings(sb.ToString()));
    }

    /// <summary>Send anything pending right now. Used before the panel closes.</summary>
    public void FlushNow() => Flush();

    private static string DescribeRebuild(FieldVm f)
        => f.Rebuild switch
        {
            "source" => f.Label + ": the capture device is being re-opened.",
            "backend" => f.Label + ": the transcription backend is being rebuilt "
                         + "- the CPU path takes a few seconds to load.",
            "gate" => f.Label + ": the speech gate is being retuned.",
            "strategy" => f.Label + ": the chunking is being rebuilt, so the "
                          + "window in progress is dropped.",
            "save" => f.Label + ": the transcript file is being reopened.",
            "overlay" => f.Label + ": the overlay is being restyled.",
            "restart" => f.Label + ": this one only takes effect when the "
                         + "program is started again.",
            "engine" => f.Label + ": whisper-server reads this once, at "
                        + "startup - press Restart on the Engine tab to load "
                        + "it now.",
            _ => "",
        };

    // ---- filtering ----------------------------------------------------------

    /// <summary>
    /// Re-evaluate every field's visibility: showIf, the advanced filter and
    /// the search box.
    /// </summary>
    public void Refilter()
    {
        string query = _search.Trim();
        foreach (GroupVm g in Groups)
        {
            int shown = 0;
            foreach (FieldVm f in g.Fields)
            {
                bool visible = (!f.Advanced || _showAdvanced)
                               && ConditionEvaluator.IsSatisfied(f.ShowIf, _live);
                f.IsVisible = visible;
                f.MatchesSearch = f.Matches(query);
                if (f.IsShown)
                {
                    shown++;
                }
            }

            g.ShownCount = shown;
            g.IsShown = shown > 0;

            // A search that matches something two groups down is useless if
            // the group is collapsed - the match is found and then hidden.
            if (query.Length > 0 && shown > 0)
            {
                g.IsExpanded = true;
            }
        }
    }

    /// <summary>Look a field up by key. For the tests and the benchmark tab.</summary>
    public FieldVm? Field(string key)
        => _byKey.TryGetValue(key, out FieldVm? f) ? f : null;

    /// <summary>
    /// Apply several settings at once, as if the user had edited them.
    /// </summary>
    /// <remarks>
    /// The benchmark's "use these numbers" button is the caller: it moves
    /// strategy, buffer and slide together, which is precisely the set that
    /// cannot be sent one at a time.
    /// </remarks>
    public void ApplyPatch(IReadOnlyDictionary<string, JsonElement> patch)
    {
        foreach (KeyValuePair<string, JsonElement> kv in patch)
        {
            _pending[kv.Key] = kv.Value;
            _live[kv.Key] = kv.Value;
        }

        Flush();
    }

    /// <summary>
    /// Every setting back to its schema default, as one patch - the "Reset
    /// all" button. One patch for the same reason the benchmark sends one:
    /// defaults are only guaranteed valid TOGETHER, against the cross-field
    /// rules.
    /// </summary>
    public void ResetToDefaults()
    {
        if (_defaults.Count > 0)
        {
            ApplyPatch(_defaults);
        }
    }

    // ---- presets -------------------------------------------------------------

    /// <summary>
    /// Refill the preset list from an ack, toast whatever it refused, and
    /// return the name the engine actually acted on - empty if it refused.
    /// </summary>
    /// <remarks>
    /// <para>
    /// A preset ack fails in two shapes and app.py returns both. The call can
    /// fail whole - no preset by that name, a file that will not parse, a
    /// built-in declining to be deleted - and that arrives as <c>error</c>,
    /// one sentence, with no work done. Or the file was read and handed to
    /// <c>Pipeline.apply</c>, which kept the fields it accepted and refused
    /// the rest; that arrives as <c>errors</c>, the same flat list of
    /// sentences <see cref="ApplyAck"/> reads, and it means the preset landed
    /// PARTLY. Both were dropped on the floor here until this method existed,
    /// so a preset written against an older schema - or hand-edited into
    /// nonsense - toasted "Preset loaded" and left the panel holding settings
    /// nobody asked for. handleAck in webui/app.js has read both shapes since
    /// the browser panel shipped; this is the desktop half of it.
    /// </para>
    /// <para>
    /// Success is tested POSITIVELY, on <paramref name="okKey"/>, rather than
    /// inferred from the absence of an error the way <see cref="ApplyAck"/>
    /// has to. The preset acks carry a key naming what they did, so an ack
    /// that arrives empty or malformed can say nothing instead of
    /// congratulating the user on a call that never happened.
    /// </para>
    /// <para>
    /// That key also carries the name app.py used, which is not always the one
    /// that was typed: _safe_preset_name strips a preset name to alphanumerics
    /// and 64 characters, because the browser panel can be served to a network
    /// and a preset name is a file name. Toasting what came back names the
    /// preset that now exists rather than the one that was asked for.
    /// </para>
    /// <para>
    /// The refusals are joined into ONE toast rather than one toast each, the
    /// way <see cref="ApplyAck"/> joins them. StatusVm.Toast holds four and
    /// evicts oldest-first, so a preset refusing five fields would push its
    /// own first complaint off the screen before it could be read. The browser
    /// affords the loop; four slots do not.
    /// </para>
    /// </remarks>
    private string PresetAck(string ackJson, string okKey, string asked)
    {
        // Before the error tests, and on every path: app.py rides the list on
        // EVERY return, error shapes included, precisely so that a refusal
        // leaves the dropdown populated instead of empty.
        SetPresets(ackJson);

        Doc ack = Doc.Parse(ackJson);

        string whole = ack["error"].Str();
        if (whole.Length > 0)
        {
            _toast("Error", "Preset '" + asked + "' was refused", whole);
            return "";
        }

        var messages = new List<string>();
        foreach (Doc e in ack["errors"].Items())
        {
            messages.Add(e.Str());
        }

        string acted = ack[okKey].Str();
        if (messages.Count == 0)
        {
            return acted;
        }

        // Deliberately returns "" even though the preset partly landed, so the
        // caller stays silent. "Preset loaded" sitting beside "2 settings were
        // refused" is the ambiguity this method exists to remove.
        _toast("Error",
               messages.Count == 1
                   ? "One setting in '" + acted + "' was refused"
                   : messages.Count + " settings in '" + acted + "' were refused",
               string.Join("\n", messages));
        return "";
    }

    public void SavePreset(string name)
    {
        if (string.IsNullOrWhiteSpace(name))
        {
            return;
        }

        string saved = PresetAck(_bridge.PresetSave(name), "saved", name);
        if (saved.Length > 0)
        {
            _toast("Success", "Preset saved", saved);
        }
    }

    public void LoadPreset(string name)
    {
        if (string.IsNullOrWhiteSpace(name))
        {
            return;
        }

        string loaded = PresetAck(_bridge.PresetLoad(name), "loaded", name);

        // Re-read on BOTH paths, and the partial one is why. The fields that
        // landed have to show their new values and the fields that were refused
        // have to show their old ones - which is what ApplyAck does by hand
        // with SnapBack, off the list of keys it sent. Nothing here knows which
        // keys the preset file held, so asking the engine what it actually
        // holds now gets both halves right without that bookkeeping.
        HydrateSettings(_bridge.GetSettingsJson());

        if (loaded.Length > 0)
        {
            _toast("Success", "Preset loaded", loaded);
        }
    }

    public void DeletePreset(string name)
    {
        if (string.IsNullOrWhiteSpace(name))
        {
            return;
        }

        string deleted = PresetAck(_bridge.PresetDelete(name), "deleted", name);
        if (deleted.Length > 0)
        {
            _toast("Informational", "Preset deleted", deleted);
        }
    }
}
