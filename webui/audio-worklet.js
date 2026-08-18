/*
  audio-worklet.js - browser capture, resampled and packed on the audio thread.

  Adapted from VoiceStreamAI (MIT, Alessandro Saccoia) - its
  realtime-audio-processor.js and the decreaseSampleRate /
  convertFloat32ToInt16 pair in utils.js. Three changes that matter:

  1. The original posts every 128-sample render quantum straight to the main
     thread - 375 messages a second at 48 kHz - and does the resampling and the
     int16 packing there. All of that runs on the audio thread here instead,
     and posts one message per ~128 ms block. The main thread is the one
     painting the transcript; it should not also be decimating audio.

  2. The original convertFloat32ToInt16 does `Math.min(1, buffer[l]) * 0x7FFF`, which
     clamps the positive side only. A sample of -1.5 - ordinary enough after
     any gain stage - becomes -49150, which does not fit in an Int16 and wraps
     around to a large POSITIVE value. That is not soft clipping, it is a
     sign flip in the middle of a loud word. Clamped both ways below.

  3. `inputs[0][0]` is undefined whenever the node has no connected input (it
     happens for a frame or two around device changes, and for the whole time a
     shared tab is muted). Posting undefined downstream throws in the handler.
     Guarded.

  The decimation is the same box average as the original: sum the input
  samples that fall inside each output sample's span and divide. It is not a
  windowed-sinc resampler, but it does average rather than pick, so it rejects
  most of the aliasing that a naive "take every third sample" would fold back
  into the speech band - and it is what the Python side does too, so both
  capture paths sound the same to Whisper.
*/

const TARGET_RATE = 16000;
const BLOCK_SAMPLES = 2048;   // 128 ms at 16 kHz, matching the Python capture

class RealtimeAudioProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || TARGET_RATE;
    this.ratio = sampleRate / this.targetRate;
    this.out = new Int16Array(BLOCK_SAMPLES);
    this.outLen = 0;
    // Carried across render quanta: one output sample usually straddles a
    // quantum boundary, and restarting the average at every boundary would put
    // a tiny discontinuity into the signal 375 times a second.
    this.acc = 0;
    this.accCount = 0;
    this.phase = 0;
    this.muted = 0;
  }

  process(inputs) {
    const channels = inputs[0];
    if (!channels || channels.length === 0 || !channels[0]) {
      // No connected input this quantum. Keep the processor alive - the input
      // usually comes back - but report a long silence once so the UI can say
      // "the tab you shared is muted" instead of "nothing is happening".
      if (++this.muted === 400) this.port.postMessage({ silent: true });
      return true;
    }
    this.muted = 0;

    // Downmix first, so a stereo tab share does not lose one side.
    const n = channels[0].length;
    let mono;
    if (channels.length === 1) {
      mono = channels[0];
    } else {
      mono = new Float32Array(n);
      for (let c = 0; c < channels.length; c++) {
        const ch = channels[c];
        for (let i = 0; i < n; i++) mono[i] += ch[i];
      }
      for (let i = 0; i < n; i++) mono[i] /= channels.length;
    }

    for (let i = 0; i < n; i++) {
      this.acc += mono[i];
      this.accCount++;
      this.phase++;
      if (this.phase >= this.ratio) {
        this.phase -= this.ratio;
        const v = this.accCount ? this.acc / this.accCount : 0;
        this.acc = 0;
        this.accCount = 0;
        // Clamp BOTH ways - see note 2 at the top of this file.
        const clamped = v > 1 ? 1 : (v < -1 ? -1 : v);
        this.out[this.outLen++] = Math.round(clamped * 32767);
        if (this.outLen === BLOCK_SAMPLES) {
          const copy = this.out.slice(0, BLOCK_SAMPLES);
          this.port.postMessage(copy.buffer, [copy.buffer]);
          this.outLen = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('realtime-audio-processor', RealtimeAudioProcessor);
