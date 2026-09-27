using System;
using System.Collections.Generic;
using NAudio.CoreAudioApi;
using NAudio.Wave;

namespace LiveTranscription.Ui.Audio;

/// <summary>
/// Captures one WASAPI endpoint - a microphone, or a render device in
/// loopback - and hands out 16 kHz mono PCM16 in exact 2048-sample blocks:
/// the same bytes, in the same 128 ms framing, that the browser panel's
/// audio worklet puts on its socket.
/// </summary>
/// <remarks>
/// <para>
/// This owns the endpoint and NAudio's capture thread; the arithmetic -
/// downmix, the worklet's box-average decimation, the exact-length blocks -
/// is <see cref="PcmDecimator"/>, split out so it can be tested without a
/// device.
/// </para>
/// <para>
/// Both callbacks fire on NAudio's capture thread. The consumer pushes the
/// block over the bridge from that thread - Pipeline.feed appends under a
/// lock and returns, exactly as the websocket path calls it - and must not
/// touch anything thread-affine.
/// </para>
/// </remarks>
public sealed class MicStreamer : IDisposable
{
    /// <summary>2048 samples = 128 ms at 16 kHz, matching the worklet.</summary>
    public const int BlockSamples = PcmDecimator.BlockSamples;

    public const int TargetRate = PcmDecimator.TargetRate;

    private readonly PcmDecimator _decimator;
    private readonly Action<string> _onStopped;

    private WasapiCapture? _capture;

    /// <param name="onBlock">takes each finished 4096-byte block, on the
    /// capture thread</param>
    /// <param name="onStopped">called once when capture ends for a reason the
    /// caller did not ask for - a device unplugged mid-stream</param>
    public MicStreamer(Action<byte[]> onBlock, Action<string> onStopped)
    {
        if (onBlock is null)
        {
            throw new ArgumentNullException(nameof(onBlock));
        }

        _decimator = new PcmDecimator(block =>
        {
            try
            {
                onBlock(block);
            }
            catch (Exception)
            {
                // The consumer ends in Python. An exception there must not
                // tear down NAudio's capture thread - dropping one block is
                // 128 ms of audio; a dead capture thread is the session.
            }
        });
        _onStopped = onStopped ?? (_ => { });
    }

    public bool IsRunning => _capture is not null;

    /// <summary>The capture endpoints - the microphone picker's contents.</summary>
    /// <remarks>
    /// Enumerated with the API that will do the capturing. The engine's own
    /// device list (audio_sources.list_devices) speaks PortAudio and
    /// soundcard ids, which mean nothing to WASAPI - offering that list here
    /// would produce devices that cannot be opened.
    /// </remarks>
    public static List<(string Id, string Name, bool IsDefault)> CaptureDevices()
    {
        var found = new List<(string, string, bool)>();
        try
        {
            using var enumerator = new MMDeviceEnumerator();
            string defaultId = "";
            try
            {
                using MMDevice def = enumerator.GetDefaultAudioEndpoint(
                    DataFlow.Capture, Role.Communications);
                defaultId = def.ID;
            }
            catch (Exception)
            {
                // No default microphone is a real state (none plugged in).
            }

            foreach (MMDevice dev in enumerator.EnumerateAudioEndPoints(
                         DataFlow.Capture, DeviceState.Active))
            {
                using (dev)
                {
                    found.Add((dev.ID, dev.FriendlyName, dev.ID == defaultId));
                }
            }
        }
        catch (Exception)
        {
            // A dead audio service costs an empty list, not a crash.
        }

        return found;
    }

    /// <summary>
    /// Start capturing a microphone (<paramref name="deviceId"/> from
    /// <see cref="CaptureDevices"/>, or "" for the default), or the default
    /// output in loopback when <paramref name="loopback"/> is true.
    /// </summary>
    /// <exception cref="InvalidOperationException">already running, no such
    /// device, or a sample format this cannot convert.</exception>
    public void Start(string deviceId, bool loopback)
    {
        if (_capture is not null)
        {
            throw new InvalidOperationException("Capture is already running.");
        }

        WasapiCapture capture = loopback
            ? new WasapiLoopbackCapture()
            : new WasapiCapture(FindCaptureDevice(deviceId));

        // The format decides float-or-integer, width and rate; a format the
        // decimator cannot decode is refused here, before the device starts.
        try
        {
            _decimator.Configure(capture.WaveFormat);
        }
        catch (InvalidOperationException)
        {
            capture.Dispose();
            throw;
        }

        capture.DataAvailable += OnDataAvailable;
        capture.RecordingStopped += OnRecordingStopped;
        _capture = capture;
        try
        {
            capture.StartRecording();
        }
        catch (Exception)
        {
            _capture = null;
            capture.Dispose();
            throw;
        }
    }

    public void Stop()
    {
        WasapiCapture? capture = _capture;
        _capture = null;
        if (capture is null)
        {
            return;
        }

        capture.DataAvailable -= OnDataAvailable;
        capture.RecordingStopped -= OnRecordingStopped;
        try
        {
            capture.StopRecording();
        }
        catch (Exception)
        {
            // Stopping a device that already vanished throws; it is stopped.
        }

        capture.Dispose();
    }

    public void Dispose() => Stop();

    private static MMDevice FindCaptureDevice(string deviceId)
    {
        using var enumerator = new MMDeviceEnumerator();
        if (string.IsNullOrEmpty(deviceId))
        {
            return enumerator.GetDefaultAudioEndpoint(DataFlow.Capture,
                                                      Role.Communications);
        }

        foreach (MMDevice dev in enumerator.EnumerateAudioEndPoints(
                     DataFlow.Capture, DeviceState.Active))
        {
            if (dev.ID == deviceId)
            {
                return dev;
            }

            dev.Dispose();
        }

        throw new InvalidOperationException(
            "That microphone is not there any more - rescan and pick again.");
    }

    private void OnRecordingStopped(object? sender, StoppedEventArgs e)
    {
        // Only report a stop the caller did not ask for: Stop() detaches this
        // handler first, so arriving here means the device went away.
        if (_capture is not null)
        {
            _capture = null;
            (sender as WasapiCapture)?.Dispose();
            _onStopped(e.Exception?.Message ?? "the device stopped");
        }
    }

    private void OnDataAvailable(object? sender, WaveInEventArgs e)
        => _decimator.Feed(e.Buffer, e.BytesRecorded);
}
