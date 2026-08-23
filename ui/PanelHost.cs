using System;
using System.Windows;
using System.Windows.Media;
using System.Windows.Threading;
using LiveTranscription.Ui.Bridge;
using LiveTranscription.Ui.ViewModels;
using LiveTranscription.Ui.Views;
using Wpf.Ui.Appearance;
using Wpf.Ui.Controls;
using Wpf.Ui.Markup;

namespace LiveTranscription.Ui;

/// <summary>
/// Everything Python touches. Python calls <see cref="Start"/> on a
/// .NET-created STA thread and that call blocks until the window closes;
/// the Post* members are how the engine pushes state in afterwards.
/// </summary>
public sealed class PanelHost
{
    private Application? _app;
    private MainWindow? _window;
    private MainVm? _vm;

    /// <summary>Set once the window exists and is safe to talk to.</summary>
    public bool IsReady { get; private set; }

    /// <summary>
    /// Build the WPF application and run its message loop. BLOCKS until the
    /// window closes, so the caller must already be on a dedicated STA thread.
    /// </summary>
    /// <param name="bridge">the Python object implementing the engine calls</param>
    /// <param name="accent">accent colour as #RRGGBB</param>
    /// <param name="dark">dark theme if true</param>
    public void Start(IEngineBridge bridge, string accent, bool dark)
    {
        if (bridge is null)
        {
            throw new ArgumentNullException(nameof(bridge));
        }

        // MUST come before anything loads a XAML component. The generated
        // InitializeComponent uses a RELATIVE pack URI, which resolves against
        // ResourceAssembly - and for a library hosted inside python.exe there
        // is no entry assembly to default to. Miss this and you get an
        // IOException about a missing resource, or worse, a raw untemplated
        // Win32 frame with no exception at all.
        Application.ResourceAssembly = typeof(PanelHost).Assembly;

        _app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };

        // The theme comes from code, not App.xaml. A library has no
        // ApplicationDefinition, and doing it here keeps the merge ORDER
        // explicit - which matters, because the charcoal override only wins if
        // it is merged after WPF-UI's own dictionaries.
        var themes = new ThemesDictionary
        {
            Theme = dark ? ApplicationTheme.Dark : ApplicationTheme.Light,
        };
        _app.Resources.MergedDictionaries.Add(themes);
        _app.Resources.MergedDictionaries.Add(new ControlsDictionary());
        _app.Resources.MergedDictionaries.Add(new ResourceDictionary
        {
            Source = new Uri(
                dark
                    ? "/LiveTranscription.Ui;component/Themes/Charcoal.xaml"
                    : "/LiveTranscription.Ui;component/Themes/CharcoalLight.xaml",
                UriKind.Relative),
        });

        // The panel's own semantic keys - confidence bands, pill tones -
        // which exist in no WPF-UI dictionary. Hand-written, not generated;
        // merged last so nothing can shadow them.
        _app.Resources.MergedDictionaries.Add(new ResourceDictionary
        {
            Source = new Uri(
                dark
                    ? "/LiveTranscription.Ui;component/Themes/Panel.xaml"
                    : "/LiveTranscription.Ui;component/Themes/PanelLight.xaml",
                UriKind.Relative),
        });

        ApplicationThemeManager.Apply(
            dark ? ApplicationTheme.Dark : ApplicationTheme.Light,
            WindowBackdropType.Mica,
            updateAccent: false);

        ApplyAccent(accent, dark);

        _vm = new MainVm(bridge, Dispatcher.CurrentDispatcher);
        _window = new MainWindow { DataContext = _vm };

        // ShutdownMode is OnExplicitShutdown, so without this the close
        // button would remove the window and leave the message loop - and
        // Python's "panel is alive" answer - running with nothing on screen.
        // Closing the window means the front end is gone, exactly like
        // closing the browser tab; the engine's lifetime is app.py's call.
        _window.Closed += (_, _) =>
        {
            IsReady = false;
            _vm?.Audio.Stop();
            _app?.Shutdown();
        };

        // A Python exception that escapes a handler must not take the process
        // down - the transcription engine is in this process too.
        _app.DispatcherUnhandledException += (_, e) =>
        {
            _vm?.ShowBanner("Error", "The panel hit an error", e.Exception.Message);
            e.Handled = true;
        };

        _window.Show();
        IsReady = true;
        _app.Run();
        IsReady = false;
    }

    private void ApplyAccent(string accent, bool dark)
    {
        // The panel's own dictionary (Panel.xaml / PanelLight.xaml, merged
        // just above) is the source of truth: it is where the webui's
        // --accent and --accent-2 are transcribed PER THEME, so the light
        // theme gets #0097A7 rather than the dark #00BCD4 that used to be
        // passed in regardless. Handing them to the accent manager verbatim
        // matters as much as which ones: the (colour, theme) overload derives
        // Primary by brightening, and #00BCD4 came out as #4DEBFF on every
        // primary button and ON toggle - a cyan the browser panel never
        // shows. The string from Python is only the fallback for a
        // dictionary without the keys.
        if (_app?.Resources["PanelAccentBrush"] is SolidColorBrush accentBrush
            && _app.Resources["PanelAccent2Brush"] is SolidColorBrush hoverBrush)
        {
            // Tiers, as WPF-UI's dictionaries bind them: Primary is the ON
            // toggle, slider thumb and focus border; Secondary is the accent
            // button AT REST (measured: the button rendered the secondary
            // colour); Tertiary is its hover. So rest = --accent on both,
            // hover = --accent-2.
            ApplicationAccentColorManager.Apply(
                systemAccent: accentBrush.Color,
                primaryAccent: accentBrush.Color,
                secondaryAccent: accentBrush.Color,
                tertiaryAccent: hoverBrush.Color);
            return;
        }

        try
        {
            object? parsed = ColorConverter.ConvertFromString(accent);
            if (parsed is Color c)
            {
                // SystemAccent is stored verbatim; Primary/Secondary/Tertiary
                // are derived from it (+brightness, -saturation for dark).
                ApplicationAccentColorManager.Apply(
                    c,
                    dark ? ApplicationTheme.Dark : ApplicationTheme.Light,
                    systemGlassColor: false,
                    systemAccentColor: false);
            }
        }
        catch (FormatException)
        {
            // A bad accent string is not worth refusing to start over.
        }
    }

    // ---- pushing state in ------------------------------------------------
    // Every one of these is safe to call from ANY Python thread. They marshal
    // asynchronously on purpose: a blocking Dispatcher.Invoke from a Python
    // worker, while the UI thread is inside a bridge call waiting on the GIL,
    // is the one deadlock this architecture can produce.

    private void Post(Action action)
    {
        Dispatcher? d = _window?.Dispatcher;
        if (d is null || !IsReady)
        {
            return;
        }

        _ = d.BeginInvoke(action, DispatcherPriority.Background);
    }

    /// <summary>Which lifecycle command holds the slot, or "" when idle.</summary>
    public void PostBusy(string busy) => Post(() => { if (_vm is not null) { _vm.Busy = busy; } });

    /// <summary>
    /// The opening document - the same payload the browser gets on connect:
    /// schema, settings, status, devices, models, presets, history, engine
    /// and the busy slot, as one JSON object.
    /// </summary>
    public void PostHello(string json) => Post(() => _vm?.ApplyHello(json));

    /// <summary>
    /// One event off the engine's stream, as (kind, JSON payload) - the same
    /// pairs the websocket broadcasts.
    /// </summary>
    /// <remarks>
    /// Meter frames should go through <see cref="OfferMeter"/> instead: they
    /// arrive 8 times a second forever, and this method costs a BeginInvoke
    /// and a dispatcher pass per call. Everything else is rare enough that
    /// marshalling per event is the simple, correct answer.
    /// </remarks>
    public void PostEvent(string kind, string json)
        => Post(() => _vm?.ApplyEvent(kind, json));

    /// <summary>
    /// Take a meter frame, on the CALLER's thread. Safe from any thread and
    /// deliberately cheap - a parse and a field write under a lock, no
    /// dispatcher, no binding. The 30 Hz pump inside StatusVm publishes it.
    /// </summary>
    public void OfferMeter(string json) => _vm?.StatusBar.OfferMeter(json);

    public void PostRunning(bool running, bool paused) => Post(() =>
    {
        if (_vm is null)
        {
            return;
        }

        _vm.Running = running;
        _vm.Paused = paused;
    });

    public void PostStatus(string status) => Post(() => { if (_vm is not null) { _vm.Status = status; } });

    public void PostBanner(string severity, string title, string message)
        => Post(() => _vm?.ShowBanner(severity, title, message));

    public void PostBannerHidden() => Post(() => _vm?.HideBanner());

    /// <summary>Close the window and end <see cref="Start"/>.</summary>
    public void Shutdown()
    {
        Application? app = _app;
        if (app is null)
        {
            return;
        }

        _ = app.Dispatcher.BeginInvoke(new Action(() =>
        {
            IsReady = false;
            app.Shutdown();
        }));
    }
}
