// ui/Audio/MicStreamer.cs
using System;
using System.Collections.Generic;
using NAudio.Wave;
using LiveTranscription.Ui.Bridge;

namespace LiveTranscription.Ui.Audio
{
    public class MicStreamer : IDisposable
    {
        private WaveInEvent _waveIn;
        private readonly IEngineBridge _bridge;
        private readonly Action<double> _onLevelCalculated;
        
        public bool IsRecording { get; private set; }

        /// <summary>
        /// Initializes the streamer with the Python bridge and a callback for the volume meter.
        /// </summary>
        public MicStreamer(IEngineBridge bridge, Action<double> onLevelCalculated)
        {
            _bridge = bridge ?? throw new ArgumentNullException(nameof(bridge));
            _onLevelCalculated = onLevelCalculated;
        }

        public static List<string> GetAvailableDevices()
        {
            var devices = new List<string>();
            for (int i = 0; i < WaveInEvent.DeviceCount; i++)
            {
                devices.Add(WaveInEvent.GetCapabilities(i).ProductName);
            }
            return devices;
        }

        public void Start(int deviceNumber = 0)
        {
            if (IsRecording) return;

            _waveIn = new WaveInEvent
            {
                DeviceNumber = deviceNumber,
                WaveFormat = new WaveFormat(16000, 16, 1),
                BufferMilliseconds = 33 // ~30Hz update rate to match your ViewModel timer
            };

            _waveIn.DataAvailable += OnDataAvailable;
            _waveIn.StartRecording();
            IsRecording = true;
        }

        private void OnDataAvailable(object sender, WaveInEventArgs e)
        {
            if (e.BytesRecorded == 0) return;

            // 1. Calculate the Audio Level (RMS) for the UI
            double sumSquares = 0;
            int sampleCount = e.BytesRecorded / 2; // 16-bit = 2 bytes per sample

            for (int i = 0; i < e.BytesRecorded; i += 2)
            {
                // Convert little-endian bytes to 16-bit short
                short sample = BitConverter.ToInt16(e.Buffer, i);
                
                // Normalize to -1.0 to 1.0 range
                double normalized = sample / 32768.0; 
                sumSquares += normalized * normalized;
            }

            double rms = Math.Sqrt(sumSquares / sampleCount);

            // Scale to 0-100 for the ProgressBar. 
            // RMS naturally peaks lower than 1.0, so we multiply by a visibility scalar (e.g., 3.0) 
            // and cap it at 100.
            double displayLevel = Math.Min(100.0, rms * 300.0);
            
            // Pass the level back to the ViewModel without forcing a UI update on this thread
            _onLevelCalculated?.Invoke(displayLevel);

            // 2. Send the raw byte array across the CLR bridge to Python
            _bridge.PushAudioChunk(e.Buffer, e.BytesRecorded);
        }

        public void Stop()
        {
            if (!IsRecording) return;
            
            _waveIn?.StopRecording();
            _waveIn?.Dispose();
            _waveIn = null;
            IsRecording = false;
        }

        public void Dispose()
        {
            Stop();
        }
    }
}