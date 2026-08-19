namespace LiveTranscription.Ui.Bridge;

/// <summary>
/// The whole boundary between the panel and the transcription engine.
/// Python implements this (wpf_panel.py); C# only ever calls it.
/// </summary>
/// <remarks>
/// <para>
/// This is the "true button function": a click reaches
/// <see cref="Restart"/> as a compile-time symbol and a virtual call.
/// There is no command-name string to misspell, no socket, no port, no token
/// and no dispatch table. Rename a method here and the build breaks on both
/// sides rather than at runtime in front of a user.
/// </para>
/// <para>
/// Two rules every implementation must keep, because breaking either one
/// deadlocks or stalls the UI:
/// </para>
/// <list type="number">
///   <item>
///     Return promptly. These run ON the WPF dispatcher thread and hold the
///     GIL while they run. Anything slow belongs on a worker - the Python
///     side already has <c>App._lifecycle</c> for exactly that, which spawns
///     a daemon thread and returns immediately.
///   </item>
///   <item>
///     Never call back into the panel synchronously from inside one of these.
///     A blocking <c>Dispatcher.Invoke</c> from a bridge method is the one
///     deadlock this architecture can produce. Use the Post* entry points on
///     <see cref="PanelHost"/>, which marshal asynchronously.
///   </item>
/// </list>
/// <para>
/// Signatures stay deliberately primitive - strings, bools, ints, string
/// arrays. pythonnet marshals those cleanly in both directions; generics and
/// custom structs are where it gets fragile.
/// </para>
/// </remarks>
public interface IEngineBridge
{
    // ---- lifecycle -------------------------------------------------------
    // Each returns true if the command was accepted, false if refused because
    // another lifecycle command still holds the single slot. That mirrors
    // App._lifecycle's return (the name, or "" when busy) - and note this is
    // NOT the result of the work: the real outcome arrives later as events.

    bool Start();
    bool Stop();
    bool Restart();
    bool Rebuild(string[] components);

    void Pause();
    void Resume();
    void ClearTranscript();

    /// <summary>Ask the program to exit. The panel closes itself first.</summary>
    void RequestExit();

    // ---- whisper-server --------------------------------------------------

    /// <param name="action">status | start | stop | restart</param>
    /// <param name="model">a model file name, or "" for the launcher default</param>
    bool EngineAction(string action, string model);

    // ---- settings --------------------------------------------------------

    /// <summary>settings.schema_json() - groups, fields, defaults, languages.</summary>
    string GetSchemaJson();

    /// <summary>The live values of every setting, as a JSON object.</summary>
    string GetSettingsJson();

    /// <summary>
    /// Apply a patch of {key: value}. One call per patch rather than one per
    /// key, because validation is cross-field: <c>slide &gt; buffer</c> is
    /// rejected, so the benchmark's Apply must send strategy, buffer and slide
    /// together or it fails on the first key.
    /// </summary>
    /// <returns>{"changed": {...}, "errors": [...]} as JSON.</returns>
    string ApplySettings(string patchJson);

    // ---- documents -------------------------------------------------------
    // JSON strings rather than typed calls: these are open-ended documents
    // whose shape belongs to Python, and freezing them into CLR types would
    // mean a rebuild every time a field is added.

    string GetStatusJson();
    string GetHistoryJson(int limit);
    string GetModelsJson();
    string GetDevicesJson();
    string GetEngineJson();

    /// <param name="format">txt | jsonl | srt | vtt</param>
    /// <returns>{"filename": ..., "content": ...} as JSON.</returns>
    string ExportJson(string format);

    // ---- presets ---------------------------------------------------------

    string GetPresetsJson();
    string PresetSave(string name);
    string PresetLoad(string name);
    string PresetDelete(string name);

    // ---- misc ------------------------------------------------------------

    void RescanDevices();
    bool RunBenchmark(string argsJson);
}
