using System;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Text;
using System.Text.Json;
using LiveTranscription.Ui.Bridge;
using LiveTranscription.Ui.Settings;

namespace LiveTranscription.Ui.ViewModels;

/// <summary>
/// The whisper-server card: what is on the port, and the three buttons.
/// </summary>
/// <remarks>
/// <para>
/// The server is another program, in another window, that this one does not
/// own and cannot assume anything about. Every field here comes from
/// <c>App._engine_status()</c>, which finds the PID by looking at the port and
/// then asks the OS what image is behind it - so the card can say "something
/// else is on 8080" rather than "not running", which are very different
/// problems with very different fixes.
/// </para>
/// <para>
/// The buttons go through the one <see cref="IEngineBridge.EngineAction"/>
/// call rather than three, because that is the shape the Python side already
/// has: <c>App._engine_action(action, args)</c> behind <c>_lifecycle</c>'s
/// single busy slot.
/// </para>
/// </remarks>
public sealed class EngineVm : ViewModelBase
{
    private readonly IEngineBridge _bridge;
    private readonly Func<bool> _isIdle;
    private readonly Action<string, string, string> _toast;

    private string _state = "unknown";
    private string _url = "";
    private string _host = "";
    private int _port;
    private int? _pid;
    private string _image = "";
    private bool _reachable;
    private bool _ours;
    private bool _launched;
    private bool _canStart;
    private string _backend = "";
    private ChoiceVm? _selectedModel;

    /// <summary>True once a human has chosen a model, not merely once one is set.</summary>
    private bool _pickedByUser;

    /// <summary>The item SetModels itself last selected, by reference.</summary>
    private ChoiceVm? _lastAssigned;
    private string _modelsDir = "";

    private Doc _catalog = Doc.None;
    private string _downloadKind = "ggml";
    private ChoiceVm? _downloadModel;
    private string _catalogNote = "";

    public EngineVm(IEngineBridge bridge, Func<bool> isIdle,
                    Action<string, string, string> toast)
    {
        _bridge = bridge;
        _isIdle = isIdle;
        _toast = toast;

        StartCommand = new RelayCommand(
            () => Act("start"),
            () => _isIdle() && !_reachable && _canStart,
            OnError);

        StopCommand = new RelayCommand(
            () => Act("stop"),
            () => _isIdle() && _pid is not null,
            OnError);

        RestartCommand = new RelayCommand(
            () => Act("restart"),
            () => _isIdle() && _canStart,
            OnError);

        RefreshCommand = new RelayCommand(
            () => _bridge.EngineAction("status", ""),
            () => true,
            OnError);

        // No _isIdle gate. A download is NOT a lifecycle command - it does not
        // take the single slot (App._model_download says why), so refusing it
        // while a Restart runs would be inventing a conflict that is not there.
        DownloadCommand = new RelayCommand(
            Download,
            () => _downloadModel is not null,
            OnError);
    }

    public RelayCommand StartCommand { get; }

    public RelayCommand StopCommand { get; }

    public RelayCommand RestartCommand { get; }

    public RelayCommand RefreshCommand { get; }

    public RelayCommand DownloadCommand { get; }

    public ObservableCollection<ChoiceVm> Models { get; } = new();

    // ---- the catalog: what could be here, beside what is --------------------

    /// <summary>The two kinds, as a picker. Values match settings.py's KINDS.</summary>
    public ObservableCollection<ChoiceVm> DownloadKinds { get; } = new()
    {
        new ChoiceVm("ggml", "GGML - whisper-server (GPU)"),
        new ChoiceVm("faster_whisper", "faster-whisper - CPU/CUDA fallback"),
    };

    public ObservableCollection<ChoiceVm> DownloadModels { get; } = new();

    /// <summary>Which kind the list below is showing.</summary>
    public ChoiceVm? SelectedDownloadKind
    {
        get => FindKind(_downloadKind);
        set
        {
            string next = value?.Value ?? "ggml";
            if (next == _downloadKind)
            {
                return;
            }

            _downloadKind = next;
            Raise(nameof(SelectedDownloadKind));
            FillCatalog();
        }
    }

    public ChoiceVm? SelectedDownloadModel
    {
        get => _downloadModel;
        set
        {
            if (Set(ref _downloadModel, value))
            {
                DownloadCommand.RaiseCanExecuteChanged();
            }
        }
    }

    /// <summary>"3 of 13 already in _models", or why the list is empty.</summary>
    public string CatalogNote
    {
        get => _catalogNote;
        private set => Set(ref _catalogNote, value);
    }

    // ---- what is on the port ------------------------------------------------

    /// <summary>running | not running | occupied | unknown</summary>
    public string State
    {
        get => _state;
        private set { if (Set(ref _state, value)) { Raise(nameof(StateTone)); } }
    }

    /// <summary>good | bad | warn - drives the card's tone.</summary>
    public string StateTone => _state switch
    {
        "running" => "good",
        "occupied" => "warn",
        "not running" => "bad",
        _ => "",
    };

    public string Url { get => _url; private set => Set(ref _url, value); }

    public string Host { get => _host; private set => Set(ref _host, value); }

    public int Port
    {
        get => _port;
        private set { if (Set(ref _port, value)) { Raise(nameof(PortText)); } }
    }

    public string PortText => _port > 0
        ? _port.ToString(CultureInfo.InvariantCulture)
        : "-";

    public int? Pid
    {
        get => _pid;
        private set { if (Set(ref _pid, value)) { Raise(nameof(PidText)); } }
    }

    public string PidText => _pid is int p
        ? p.ToString(CultureInfo.InvariantCulture)
        : "-";

    public string Image
    {
        get => _image;
        private set { if (Set(ref _image, value)) { Raise(nameof(ImageText)); } }
    }

    public string ImageText => _image.Length > 0 ? _image : "-";

    public bool Reachable { get => _reachable; private set => Set(ref _reachable, value); }

    /// <summary>Is the process on the port one of ours, by image name?</summary>
    public bool Ours { get => _ours; private set => Set(ref _ours, value); }

    public bool Launched { get => _launched; private set => Set(ref _launched, value); }

    /// <summary>Is the launcher script present at all?</summary>
    public bool CanStart { get => _canStart; private set => Set(ref _canStart, value); }

    public string Backend { get => _backend; private set => Set(ref _backend, value); }

    public string ModelsDir { get => _modelsDir; private set => Set(ref _modelsDir, value); }

    public ChoiceVm? SelectedModel
    {
        get => _selectedModel;
        set => Set(ref _selectedModel, value);
    }

    /// <summary>The sentence under the state, saying what to do about it.</summary>
    public string Advice => _state switch
    {
        "running" => _ours
            ? "The GPU server is up and answering."
            : "Something is answering on this port, but it is not the whisper "
              + "server this project launches. Check what " + ImageText + " is.",
        "occupied" => ImageText + " holds the port but is not answering as a "
                      + "whisper server. Stop it, or move the server to another port.",
        "not running" => _canStart
            ? "Nothing is on the port. Start it to move transcription onto the GPU."
            : "Nothing is on the port, and start_whisper_server.cmd was not found "
              + "next to this program, so the panel cannot start one.",
        _ => "",
    };

    /// <summary>Apply an <c>engine</c> document - App._engine_status().</summary>
    public void Apply(string json)
    {
        Doc e = Doc.Parse(json);
        if (!e.Exists)
        {
            return;
        }

        Url = e["url"].Str();
        Host = e["host"].Str();
        Port = e["port"].Int();
        Pid = e["pid"].IntOrNull();
        Image = e["image"].Str();
        Reachable = e["reachable"].Bool();
        Ours = e["ours"].Bool();
        Launched = e["launched"].Bool();
        CanStart = e["canStart"].Bool();
        Backend = e["backend"].Str();

        // Three states, not two. "A process holds the port but does not answer"
        // is the one that actually happens - a server still loading its model,
        // or an unrelated program on 8080 - and calling it "not running" sends
        // people to press Start, which then fails because the port is taken.
        State = Reachable ? "running" : Pid is not null ? "occupied" : "not running";

        SetModels(e["models"], e["modelsDir"].Str(), e["model"].Str());
        RaiseGates();
    }

    /// <summary>Apply a <c>models</c> document - App._list_models().</summary>
    public void ApplyModels(string json)
    {
        Doc m = Doc.Parse(json);
        SetModels(m["ggml"], m["dir"].Str());
    }

    private void SetModels(Doc ggml, string dir, string configured = "")
    {
        if (dir.Length > 0)
        {
            ModelsDir = dir;
        }

        // Has a HUMAN chosen, or is this just the value we put there ourselves?
        // Not the same question, and the difference decides whether the
        // configured model may fill the box. Two things make it awkward: the
        // launcher-default entry IS the empty value, so an untouched dropdown
        // and a deliberate "(the launcher's default)" both leave keep empty;
        // and the ComboBox's two-way binding writes SelectedModel back while
        // the tab loads, so a flag set in the setter says "the user picked"
        // when nobody has touched it. Reference identity answers it cleanly -
        // the binding echoes back the very instance we assigned, while a real
        // pick is a different item out of the list. Sticky once observed,
        // because the refresh after a pick assigns that same item back.
        if (_selectedModel is not null
            && !ReferenceEquals(_selectedModel, _lastAssigned))
        {
            _pickedByUser = true;
        }

        string keep = _selectedModel?.Value ?? "";
        var fresh = new ObservableCollection<ChoiceVm>();

        // The launcher default stays first and stays selectable: passing "" is
        // how App._engine_start is told to use whatever start_whisper_server.cmd
        // picks, which is not the same as picking the first file in the list.
        fresh.Add(new ChoiceVm("", "(the launcher's default)"));
        foreach (Doc m in ggml.Items())
        {
            string name = m["name"].Str();
            double size = m["size_mb"].Num();
            fresh.Add(new ChoiceVm(
                name,
                size > 0
                    ? string.Format(CultureInfo.InvariantCulture, "{0}   ({1:0} MB)", name, size)
                    : name));
        }

        Models.Clear();
        ChoiceVm? restored = null;
        ChoiceVm? preferred = null;
        foreach (ChoiceVm c in fresh)
        {
            Models.Add(c);
            if (_pickedByUser && string.Equals(c.Value, keep, StringComparison.Ordinal))
            {
                restored = c;
            }

            if (configured.Length > 0
                && string.Equals(c.Value, configured, StringComparison.Ordinal))
            {
                preferred = c;
            }
        }

        // The human's own choice first, then the model the engine will actually
        // start with, and only then the launcher default. Opening on
        // settings.server_model is the point: it is remembered across launches,
        // so this tab now shows what will really happen instead of an empty
        // entry that quietly meant "the smallest file in _models".
        _lastAssigned = restored ?? preferred ?? Models[0];
        SelectedModel = _lastAssigned;
    }

    /// <summary>
    /// Re-ask every button whether it is clickable.
    /// </summary>
    /// <remarks>
    /// Called on the busy transition as well as on a fresh engine document,
    /// because RelayCommand deliberately does not use CommandManager's
    /// RequerySuggested - a Python daemon thread clearing the busy slot raises
    /// no input event, so nothing else would ever re-ask.
    /// </remarks>
    public void RaiseGates()
    {
        StartCommand.RaiseCanExecuteChanged();
        StopCommand.RaiseCanExecuteChanged();
        RestartCommand.RaiseCanExecuteChanged();
    }

    /// <summary>A fresh catalog arrived - at connect, or after a download.</summary>
    public void ApplyCatalog(string json)
    {
        _catalog = Doc.Parse(json);
        FillCatalog();
    }

    private ChoiceVm? FindKind(string value)
    {
        foreach (ChoiceVm k in DownloadKinds)
        {
            if (k.Value == value)
            {
                return k;
            }
        }

        return DownloadKinds.Count > 0 ? DownloadKinds[0] : null;
    }

    /// <summary>
    /// Rebuild the download list for the selected kind.
    /// </summary>
    /// <remarks>
    /// Models already in _models are listed and MARKED, never filtered out.
    /// "is it already here" is the question this list is read to settle, so
    /// dropping those rows makes a model that IS present indistinguishable
    /// from one that cannot be had at all.
    /// </remarks>
    private void FillCatalog()
    {
        string keep = _downloadModel?.Value ?? "";
        DownloadModels.Clear();

        int here = 0;
        int total = 0;
        foreach (Doc m in _catalog[_downloadKind].Items())
        {
            string name = m["name"].Str();
            double mb = m["size_mb"].Num();
            bool have = m["installed"].Bool();
            total++;
            if (have)
            {
                here++;
            }

            var label = new StringBuilder(name);
            if (mb > 0)
            {
                label.Append(mb >= 1000
                    ? string.Format(CultureInfo.InvariantCulture, "   ({0:0.0} GB)", mb / 1000)
                    : string.Format(CultureInfo.InvariantCulture, "   ({0:0} MB)", mb));
            }

            if (have)
            {
                label.Append("   ✓ in _models");
            }

            string note = m["note"].Str();
            if (note.Length > 0)
            {
                label.Append("   - ").Append(note);
            }

            DownloadModels.Add(new ChoiceVm(name, label.ToString()));
        }

        CatalogNote = total > 0
            ? string.Format(CultureInfo.InvariantCulture,
                            "{0} of {1} already in _models", here, total)
            : "catalog not loaded yet";

        ChoiceVm? restore = null;
        foreach (ChoiceVm c in DownloadModels)
        {
            if (c.Value == keep)
            {
                restore = c;
            }
        }

        SelectedDownloadModel = restore
            ?? (DownloadModels.Count > 0 ? DownloadModels[0] : null);
    }

    private void Download()
    {
        string name = _downloadModel?.Value ?? "";
        if (name.Length == 0)
        {
            return;
        }

        // The name only. Python resolves it against its own catalog and builds
        // every path from the row it matched - see model_fetch.resolve.
        Doc ack = Doc.Parse(_bridge.DownloadModel(_downloadKind, name));
        string error = ack["error"].Str();
        if (error.Length > 0)
        {
            _toast("Error", "That model cannot be downloaded", error);
            return;
        }

        _toast("Success", "Downloading " + name,
               "It runs in the background and the pipeline keeps working. "
               + "Progress is on the Log tab, and the model joins the pickers "
               + "when it lands.");
    }

    private void Act(string action)
    {
        string model = _selectedModel?.Value ?? "";
        if (!_bridge.EngineAction(action, model))
        {
            _toast("Warning", "Something else is still running",
                   "A lifecycle command already holds the slot. It will finish "
                   + "in a moment - the panel does not queue a second one.");
        }
    }

    private void OnError(Exception ex)
        => _toast("Error", "The engine command failed", ex.Message);

    /// <summary>The patch that pins the transcription backend to the server.</summary>
    public static JsonElement ServerBackend
        => JsonSerializer.SerializeToElement("server");
}
