using System;
using System.Windows.Input;

namespace LiveTranscription.Ui.Bridge;

/// <summary>
/// An <see cref="ICommand"/> over a plain delegate, with the two things a
/// naive implementation gets wrong - both of which were found by review of
/// the prototype rather than in production, which is the only reason they are
/// fixed here on day one.
/// </summary>
/// <remarks>
/// <para>
/// <b>1. Execute catches.</b> The delegate ends up in Python. An exception
/// raised there arrives as a .NET exception on the dispatcher thread, and an
/// unhandled dispatcher exception tears down <c>Application.Run</c> - taking
/// the window down and, because the CLR is hosted inside the transcription
/// process, doing it while a session is running. A prototype without this
/// looked fine only because the test driver invoked Execute through
/// <c>Dispatcher.Invoke</c> from Python, which marshals the exception back to
/// the caller. A real mouse click has no such caller.
/// </para>
/// <para>
/// <b>2. CanExecuteChanged is raised explicitly.</b> The usual idiom wires it
/// to <see cref="CommandManager.RequerySuggested"/>, which fires on mouse and
/// focus input - not when a Python daemon thread clears the busy slot in
/// <c>App._lifecycle</c>'s finally block. With RequerySuggested the lifecycle
/// buttons stay greyed after a Restart finishes until the user happens to
/// move the mouse, and un-grey on unrelated mouse movement mid-command. The
/// gate has to be driven by the busy transition itself.
/// </para>
/// </remarks>
public sealed class RelayCommand : ICommand
{
    private readonly Action _run;
    private readonly Func<bool>? _can;
    private readonly Action<Exception>? _onError;

    public RelayCommand(Action run, Func<bool>? can = null,
                        Action<Exception>? onError = null)
    {
        _run = run ?? throw new ArgumentNullException(nameof(run));
        _can = can;
        _onError = onError;
    }

    public event EventHandler? CanExecuteChanged;

    /// <summary>
    /// Re-ask <c>CanExecute</c>. Call this from whatever actually changes the
    /// answer - the busy transition, a connection change - marshalled onto the
    /// dispatcher thread.
    /// </summary>
    public void RaiseCanExecuteChanged()
        => CanExecuteChanged?.Invoke(this, EventArgs.Empty);

    public bool CanExecute(object? parameter)
    {
        if (_can is null)
        {
            return true;
        }

        try
        {
            return _can();
        }
        catch (Exception)
        {
            // A predicate that throws must not make the button unclickable
            // forever, and must not throw during a layout pass either.
            return false;
        }
    }

    public void Execute(object? parameter)
    {
        try
        {
            _run();
        }
        catch (Exception ex)
        {
            // Swallowing would be worse than crashing - the button would look
            // like it did nothing, which is the complaint this whole panel
            // exists to answer. Route it somewhere the user can see it.
            _onError?.Invoke(ex);
        }
    }
}
