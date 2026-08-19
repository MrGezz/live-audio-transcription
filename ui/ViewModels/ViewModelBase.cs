using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace LiveTranscription.Ui.ViewModels;

/// <summary>
/// The minimum that makes <c>{Binding}</c> live. Deliberately hand-rolled
/// rather than pulled from a MVVM package: this assembly ships as committed
/// build output, so every dependency added here is another DLL a cloner has
/// to receive.
/// </summary>
public abstract class ViewModelBase : INotifyPropertyChanged
{
    public event PropertyChangedEventHandler? PropertyChanged;

    protected void Raise([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));

    /// <summary>
    /// Assign and notify, but only when the value actually moved.
    /// </summary>
    /// <remarks>
    /// The equality check is not a micro-optimisation here. Meter frames
    /// arrive at 8 Hz and state pushes repeat unchanged fields constantly; a
    /// notification per assignment would re-run every converter and binding on
    /// the window several times a second for values that did not change.
    /// </remarks>
    protected bool Set<T>(ref T field, T value, [CallerMemberName] string? name = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value))
        {
            return false;
        }

        field = value;
        Raise(name);
        return true;
    }
}
