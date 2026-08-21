using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows.Input;
// Assuming LiveTranscription.Ui.Bridge.RelayCommand exists as per the docs
using LiveTranscription.Ui.Bridge; 

namespace LiveTranscription.Ui.ViewModels
{
    public class EngineVm : INotifyPropertyChanged
    {
        private string _serverState = "Stopped";
        private int? _port;
        private int? _pid;
        private string _image = "None"; // e.g., "Vulkan", "CUDA", or "whisper.cpp"
        private string _selectedModel;
        private bool _isBusy;

        public string ServerState { get => _serverState; set => SetProperty(ref _serverState, value); }
        public int? Port { get => _port; set => SetProperty(ref _port, value); }
        public int? Pid { get => _pid; set => SetProperty(ref _pid, value); }
        public string Image { get => _image; set => SetProperty(ref _image, value); }
        
        public ObservableCollection<string> AvailableModels { get; } = new();
        
        public string SelectedModel 
        { 
            get => _selectedModel; 
            set => SetProperty(ref _selectedModel, value); 
        }

        // Bridge Commands mapped to the Python engine
        public ICommand StartCommand { get; }
        public ICommand RestartCommand { get; }
        public ICommand StopCommand { get; }

        public EngineVm(IEngineBridge bridge)
        {
            // The document notes that lifecycle buttons hang off a busy gate
            StartCommand = new RelayCommand(_ => bridge.StartEngine(SelectedModel), _ => !_isBusy && ServerState == "Stopped");
            RestartCommand = new RelayCommand(_ => bridge.RestartEngine(SelectedModel), _ => !_isBusy && ServerState == "Running");
            StopCommand = new RelayCommand(_ => bridge.StopEngine(), _ => !_isBusy && ServerState != "Stopped");
        }

        public void SetBusyState(bool isBusy)
        {
            _isBusy = isBusy;
            // Force commands to re-evaluate CanExecute
            (StartCommand as RelayCommand)?.RaiseCanExecuteChanged();
            (RestartCommand as RelayCommand)?.RaiseCanExecuteChanged();
            (StopCommand as RelayCommand)?.RaiseCanExecuteChanged();
        }

        public event PropertyChangedEventHandler PropertyChanged;
        protected void SetProperty<T>(ref T backingField, T value, [CallerMemberName] string propertyName = null)
        {
            if (!Equals(backingField, value))
            {
                backingField = value;
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
            }
        }
    }
}