using System;
using NAudio.Wave;

namespace LiveTranscription.Ui.Audio;

/// <summary>
/// Turns a WASAPI stream - any channel count, float or integer samples, any
/// rate - into 16 kHz mono PCM16 in exact 2048-sample blocks: the bytes the
/// browser panel's audio worklet puts on its socket, in the same 128 ms
/// framing.
/// </summary>
/// <remarks>
/// <para>
/// Split out of <see cref="MicStreamer"/> so that it can be tested without a
/// device. The streamer owns the endpoint and NAudio's capture thread; this
/// owns the arithmetic, and takes its input as a buffer and a byte count -
/// exactly what NAudio's DataAvailable hands over - so a test can feed it
/// synthetic frames and read the blocks back.
/// </para>
/// <para>
/// The decimation is the worklet's, deliberately (webui/audio-worklet.js):
/// downmix first so a stereo share does not lose one side, then box-average
/// the input samples that fall inside each output sample's span, carrying the
/// accumulator across calls so a buffer boundary does not put a discontinuity
/// into the signal ten times a second. It is not a windowed sinc, but it
/// averages rather than picks, so it rejects most of the aliasing a
/// take-every-third-sample would fold into the speech band - and it is what
/// the Python capture does too, so every path into the engine sounds the same
/// to Whisper.
/// </para>
/// <para>
/// Blocks are allocated at exactly their length. NAudio hands back a pooled
/// buffer that is usually longer than the frame it just filled, and the
/// bridge contract (see IEngineBridge.PushAudioChunk) is that the array IS
/// the audio - marshalling slack across pythonnet 10 times a second and
/// trusting Python to trim it puts the trim on the side that cannot see the
/// length.
/// </para>
/// </remarks>
public sealed class PcmDecimator
{
    /// <summary>2048 samples = 128 ms at 16 kHz, matching the worklet.</summary>
    public const int BlockSamples = 2048;

    public const int TargetRate = 16000;

    /// <summary>KSDATAFORMAT_SUBTYPE_IEEE_FLOAT - what a WAVEFORMATEXTENSIBLE
    /// says when its samples are floats.</summary>
    private static readonly Guid IeeeFloatSubFormat =
        new("00000003-0000-0010-8000-00aa00389b71");

    private readonly Action<byte[]> _onBlock;

    private double _ratio;
    private int _channels;
    private int _bytesPerSample;
    private bool _sourceIsFloat;

    // The worklet's carried state: one output sample usually straddles an
    // input buffer boundary.
    private readonly short[] _out = new short[BlockSamples];
    private int _outLen;
    private double _acc;
    private int _accCount;
    private double _phase;

    /// <param name="onBlock">takes each finished 4096-byte block, on the
    /// thread that called <see cref="Feed"/></param>
    public PcmDecimator(Action<byte[]> onBlock)
    {
        _onBlock = onBlock ?? throw new ArgumentNullException(nameof(onBlock));
    }

    /// <summary>The input format's sample rate over 16 kHz, after <see cref="Configure"/>.</summary>
    public double Ratio => _ratio;

    /// <summary>
    /// Take the format the frames will arrive in, and reset the carried state.
    /// </summary>
    /// <remarks>
    /// WASAPI shared mode hands out 32-bit IEEE float; 16-bit PCM appears from
    /// some drivers, and 32-bit INTEGER PCM (24 valid bits in a 32-bit
    /// container) from others. The float question is asked of the format's
    /// encoding, not of its width: 32 bits used to be read as "float", and a
    /// 32-bit integer stream decoded that way is not quiet or loud but noise -
    /// every sample reinterpreted as an exponent. Anything else is refused
    /// rather than decoded wrongly.
    /// </remarks>
    /// <exception cref="InvalidOperationException">a sample format this
    /// cannot convert.</exception>
    public void Configure(WaveFormat wf)
    {
        if (wf is null)
        {
            throw new ArgumentNullException(nameof(wf));
        }

        bool isFloat = IsIeeeFloat(wf);
        bool supported = isFloat
            ? wf.BitsPerSample == 32
            : wf.BitsPerSample is 32 or 16;
        if (!supported || wf.Channels < 1)
        {
            throw new InvalidOperationException(
                "The device delivers " + wf.BitsPerSample + "-bit samples; "
                + "only 32-bit float, 32-bit PCM and 16-bit PCM are supported.");
        }

        _sourceIsFloat = isFloat;
        _channels = wf.Channels;
        _bytesPerSample = wf.BitsPerSample / 8;
        _ratio = wf.SampleRate / (double)TargetRate;
        _acc = 0;
        _accCount = 0;
        _phase = 0;
        _outLen = 0;
    }

    /// <summary>
    /// Decode and decimate <paramref name="bytesRecorded"/> bytes of frames,
    /// raising the block callback for every 2048 output samples completed.
    /// A partial frame at the end is ignored, as NAudio never delivers one.
    /// </summary>
    public void Feed(byte[] buffer, int bytesRecorded)
    {
        if (_channels == 0)
        {
            throw new InvalidOperationException("Configure() first.");
        }

        int frameBytes = _bytesPerSample * _channels;
        int frames = Math.Min(bytesRecorded, buffer.Length) / frameBytes;

        for (int frame = 0; frame < frames; frame++)
        {
            int at = frame * frameBytes;

            // Downmix before decimating, like the worklet.
            double mono = 0;
            for (int ch = 0; ch < _channels; ch++)
            {
                int o = at + (ch * _bytesPerSample);
                mono += _sourceIsFloat
                    ? BitConverter.ToSingle(buffer, o)
                    : _bytesPerSample == 4
                        ? BitConverter.ToInt32(buffer, o) / 2147483648.0
                        : BitConverter.ToInt16(buffer, o) / 32768.0;
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
            _onBlock(block);
        }
    }

    /// <summary>
    /// Are the samples IEEE floats? The shared-mode mix format arrives as a
    /// WAVEFORMATEXTENSIBLE, whose Encoding reads Extensible and whose
    /// sub-format carries the answer; a plain WAVEFORMATEX says it outright.
    /// </summary>
    public static bool IsIeeeFloat(WaveFormat wf)
        => wf.Encoding == WaveFormatEncoding.IeeeFloat
           || (wf is WaveFormatExtensible ext && ext.SubFormat == IeeeFloatSubFormat);
}
