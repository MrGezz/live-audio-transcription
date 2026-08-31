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

    /// <summary>
    /// Every model that COULD be fetched, next to whether it is here already.
    /// The other half of <see cref="GetModelsJson"/>, which can only ever
    /// report what is in _models and so cannot offer a size nobody has
    /// downloaded yet.
    /// </summary>
    string GetModelCatalogJson();

    /// <summary>
    /// Start fetching one catalogued model into _models. Returns at once - the
    /// transfer runs on a Python daemon thread and reports through the log.
    /// </summary>
    /// <param name="kind">ggml | faster_whisper</param>
    /// <param name="name">
    /// A name from <see cref="GetModelCatalogJson"/> and nothing else. Never a
    /// URL, a path or a repo id: Python resolves this against its own catalog
    /// and derives every path from the row it matched, so the panel cannot
    /// name a destination even by accident.
    /// </param>
    /// <returns>{"started": name} or {"error": ...} as JSON.</returns>
    string DownloadModel(string kind, string name);
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

    // ---- audio in --------------------------------------------------------

    /// <summary>
    /// Hand the engine a block of captured audio: 16 kHz mono little-endian
    /// PCM16, the same bytes the browser panel puts on its socket.
    /// </summary>
    /// <remarks>
    /// <para>
    /// This is the desktop half of what <c>capture: browser</c> does, and the
    /// reason it exists is that a browser cannot reliably give you the
    /// speakers. <c>getUserMedia</c> is the microphone only;
    /// <c>getDisplayMedia</c> will hand over a tab's audio, but system audio
    /// needs a whole-screen share, is Chromium-only and is refused outright on
    /// Firefox and Safari. WASAPI loopback has none of those conditions, so
    /// the panel captures natively and pushes the result through here.
    /// </para>
    /// <para>
    /// The array is exactly as long as the audio in it. That is not tidiness:
    /// NAudio hands back a pooled buffer that is usually longer than the frame
    /// it just filled, and marshalling the slack across pythonnet 30 times a
    /// second - then trusting Python to trim it - costs more than the copy and
    /// puts the trim on the side that cannot see the length.
    /// </para>
    /// <para>
    /// Like every other method here it runs on the caller's thread and must
    /// return promptly. <c>Pipeline.feed</c> does: it appends to a buffer under
    /// a lock and returns, exactly as the websocket path already calls it.
    /// </para>
    /// </remarks>
    /// <returns>false if nothing consumed it - the engine is not in a push
    /// capture mode, or is stopped.</returns>
    bool PushAudioChunk(byte[] pcm16le);

    // ---- misc ------------------------------------------------------------

    void RescanDevices();
    bool RunBenchmark(string argsJson);
}
