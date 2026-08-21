using System;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows.Threading;

namespace LiveTranscription.Ui.ViewModels
{
    public class StatusVm : INotifyPropertyChanged
    {
        private double _internalAudioLevel;
        private double _displayAudioLevel;
        private readonly DispatcherTimer _meterTimer;

        public double DisplayAudioLevel
        {
            get => _displayAudioLevel;
            private set
            {
                if (_displayAudioLevel != value)
                {
                    _displayAudioLevel = value;
                    OnPropertyChanged();
                }
            }
        }

        public StatusVm()
        {
            // Pull the meter value at 30 Hz as mandated by the docs
            _meterTimer = new DispatcherTimer
            {
                Interval = TimeSpan.FromMilliseconds(1000.0 / 30.0) 
            };
            _meterTimer.Tick += (s, e) => DisplayAudioLevel = _internalAudioLevel;
            _meterTimer.Start();
        }

        public void UpdateAudioLevelFromEngine(double level)
        {
            _internalAudioLevel = level; 
        }

        private string _runBannerState = "Stopped";
        public string RunBannerState
        {
            get => _runBannerState;
            set { _runBannerState = value; OnPropertyChanged(); }
        }

        public ObservableCollection<ToastNotification> ActiveToasts { get; } = new();

        public void ShowToast(string message, ToastLifetime lifetime)
        {
            var toast = new ToastNotification(message, lifetime);
            ActiveToasts.Add(toast);
            
            if (lifetime != ToastLifetime.Never)
            {
                var closeTimer = new DispatcherTimer { Interval = GetLifetimeDuration(lifetime) };
                closeTimer.Tick += (s, e) =>
                {
                    closeTimer.Stop();
                    ActiveToasts.Remove(toast);
                };
                closeTimer.Start();
            }
        }

        private TimeSpan GetLifetimeDuration(ToastLifetime lifetime)
        {
            return lifetime switch
            {
                ToastLifetime.Short => TimeSpan.FromSeconds(4.5),
                ToastLifetime.Medium => TimeSpan.FromSeconds(9),
                ToastLifetime.Long => TimeSpan.FromSeconds(30),
                _ => TimeSpan.MaxValue
            };
        }

        public event PropertyChangedEventHandler PropertyChanged;
        protected void OnPropertyChanged([CallerMemberName] string propertyName = null)
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
        }
    }

    public enum ToastLifetime { Short, Medium, Long, Never }

    public class ToastNotification
    {
        public string Message { get; }
        public ToastLifetime Lifetime { get; }

        public ToastNotification(string message, ToastLifetime lifetime)
        {
            Message = message;
            Lifetime = lifetime;
        }
    }
}