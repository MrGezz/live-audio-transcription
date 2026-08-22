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
/// The decimation is the worklet's, deliberately (webui/audio-worklet.js):
/// downmix first so a stereo share does not lose one side, then box-average
/// the input samples that fall inside each output sample's span, carrying the
/// accumulator across DataAvailable calls so a block boundary does not put a
/// discontinuity into the signal ten times a second. It is not a windowed
/// sinc, but it averages rather than picks, so it rejects most of the
/// aliasing a take-every-third-sample would fold into the speech band - and
/// it is what the Python capture does too, so every path into the engine
/// sounds the same to Whisper.
/// </para>
/// <para>
/// Blocks are allocated at exactly their length. NAudio hands back a pooled
/// buffer that is usually longer than the frame it just filled, and the
/// bridge contract (see IEngineBridge.PushAudioChunk) is that the array IS
/// the audio - marshalling slack across pythonnet 10 times a second and
/// trusting Python to trim it puts the trim on the side that cannot see the
/// length.
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
    public const int BlockSamples = 2048;

    public const int TargetRate = 16000;

    private readonly Action<byte[]> _onBlock;
    private readonly Action<string> _onStopped;

    private WasapiCapture? _capture;
    private double _ratio;
    private int _channels;
    private int _bytesPerSample;
    private bool _sourceIsFloat;

    // The worklet's carried state: one output sample usually straddles an
    // input block boundary.
    private readonly short[] _out = new short[BlockSamples];
    private int _outLen;
    private double _acc;
    private int _accCount;
    private double _phase;

    /// <param name="onBlock">takes each finished 4096-byte block, on the
    /// capture thread</param>
    /// <param name="onStopped">called once when capture ends for a reason the
    /// caller did not ask for - a device unplugged mid-stream</param>
    public MicStreamer(Action<byte[]> onBlock, Action<string> onStopped)
    {
        _onBlock = onBlock ?? throw new ArgumentNullException(nameof(onBlock));
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

        WaveFormat wf = capture.WaveFormat;
        _channels = wf.Channels;
        _bytesPerSample = wf.BitsPerSample / 8;
        // WASAPI shared mode hands out 32-bit float; 16-bit PCM appears from
        // some drivers. Anything else is refused rather than decoded wrongly.
        _sourceIsFloat = wf.BitsPerSample == 32;
        if (wf.BitsPerSample is not (32 or 16))
        {
            capture.Dispose();
            throw new InvalidOperationException(
                "The device delivers " + wf.BitsPerSample + "-bit samples; "
                + "only 32-bit float and 16-bit PCM are supported.");
        }

        _ratio = wf.SampleRate / (double)TargetRate;
        _acc = 0;
        _accCount = 0;
        _phase = 0;
        _outLen = 0;

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
    {
        int frameBytes = _bytesPerSample * _channels;
        int frames = e.BytesRecorded / frameBytes;

        for (int frame = 0; frame < frames; frame++)
        {
            int at = frame * frameBytes;

            // Downmix before decimating, like the worklet.
            double mono = 0;
            for (int ch = 0; ch < _channels; ch++)
            {
                int o = at + (ch * _bytesPerSample);
                mono += _sourceIsFloat
                    ? BitConverter.ToSingle(e.Buffer, o)
                    : BitConverter.ToInt16(e.Buffer, o) / 32768.0;
            }

            mono /= _channels;

            _acc += mono;
            _accCount++;
            _phase++;
            if (_phase < _ratio)
            {
                continue;
            }

            _phase -= _ratio;
            double v = _accCount > 0 ? _acc / _accCount : 0;
            _acc = 0;
            _accCount = 0;

            // Clamp BOTH ways. `min(1, v) * 32767` on a -1.5 sample - ordinary
            // after any gain stage - wraps to a large positive value: a sign
            // flip mid-word, not soft clipping. Same fix as the worklet's.
            v = v > 1 ? 1 : v < -1 ? -1 : v;
            _out[_outLen++] = (short)Math.Round(v * 32767);
            if (_outLen < BlockSamples)
            {
                continue;
            }

            _outLen = 0;
            byte[] block = new byte[BlockSamples * 2];
            Buffer.BlockCopy(_out, 0, block, 0, block.Length);
            try
            {
                _onBlock(block);
            }
            catch (Exception)
            {
                // The consumer ends in Python. An exception there must not
                // tear down NAudio's capture thread - dropping one block is
                // 128 ms of audio; a dead capture thread is the session.
            }
        }
    }
}
