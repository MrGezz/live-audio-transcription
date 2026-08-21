using System.Collections.ObjectModel;
using System.Threading.Tasks;
using System.Windows.Input;
using LiveTranscription.Ui.Bridge;

namespace LiveTranscription.Ui.ViewModels
{
    public class BenchmarkVm : INotifyPropertyChanged
    {
        private bool _isRunning;
        private string _benchmarkStatus = "Ready";

        public bool IsRunning 
        { 
            get => _isRunning; 
            set 
            {
                _isRunning = value;
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(IsRunning)));
                (RunBenchmarkCommand as RelayCommand)?.RaiseCanExecuteChanged();
            }
        }

        public string BenchmarkStatus 
        { 
            get => _benchmarkStatus; 
            set 
            { 
                _benchmarkStatus = value; 
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(BenchmarkStatus)));
            }
        }

        public ObservableCollection<string> Results { get; } = new();

        public ICommand RunBenchmarkCommand { get; }

        public BenchmarkVm(IEngineBridge bridge)
        {
            RunBenchmarkCommand = new RelayCommand(
                async _ => await ExecuteBenchmarkAsync(bridge), 
                _ => !IsRunning
            );
        }

        private async Task ExecuteBenchmarkAsync(IEngineBridge bridge)
        {
            IsRunning = true;
            BenchmarkStatus = "Running benchmark...";
            Results.Clear();

            // Example of how the bridge might trigger the Python benchmark.py logic
            var result = await Task.Run(() => bridge.RunBenchmark());
            
            Results.Add(result);
            BenchmarkStatus = "Completed";
            IsRunning = false;
        }

        public event PropertyChangedEventHandler PropertyChanged;
    }
}