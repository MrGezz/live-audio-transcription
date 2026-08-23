"""
Build tests/fixtures/speech_sample.wav: real speech with KNOWN gaps.

Six sentences from Windows SAPI, each split once at its quietest point with
a 0.30 s gap (a breath inside a sentence) and separated by 0.85 s gaps (the
end of one). SAPI's own leading and trailing silence is trimmed off every
utterance first, so the only gaps in the file are the ones inserted here -
which is what gives test_real_model.py a ground truth to count against.

Windows only (System.Speech). The output is gitignored; run this once:

    python tests\\make_speech_sample.py
"""
import json
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np

SR = 16000
SHORT_GAP = 0.30        # inside a sentence - this talker's word gap
LONG_GAP = 0.85         # between sentences
PHRASES = [
    "The quick brown fox jumps over the lazy dog",
    "Silero decides whether this buffer contains speech",
    "A gap only ends speech once it has lasted long enough",
    "Four hundred milliseconds is the first value that finds the sentences",
    "This is the fifth and final sentence of the sample",
    "One more line to make six in total",
]
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

PS = r'''
Add-Type -AssemblyName System.Speech
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
$i = 0
foreach ($p in $args[1..($args.Length - 1)]) {
  $i++
  $syn = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $syn.SetOutputToWaveFile((Join-Path $args[0] ("utt{0}.wav" -f $i)), $fmt)
  $syn.Rate = 0
  $syn.Speak($p)
  $syn.Dispose()
}
'''


def synthesize(tmp):
    # -File, not -Command: PowerShell binds positional arguments to $args
    # only for a script file, and folds them into the command text otherwise.
    ps1 = os.path.join(tmp, "say.ps1")
    with open(ps1, "w") as f:
        f.write(PS)
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
           "Bypass", "-File", ps1, tmp] + PHRASES
    subprocess.run(cmd, check=True)
    return [os.path.join(tmp, "utt%d.wav" % (i + 1))
            for i in range(len(PHRASES))]


def read(path):
    with wave.open(path, "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1, path
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def trim(sig, thresh=0.005, win=160):
    """Drop leading/trailing near-silence so inserted gaps are the only gaps."""
    n = len(sig) // win
    peaks = np.abs(sig[:n * win].reshape(n, win)).max(axis=1)
    loud = np.flatnonzero(peaks > thresh)
    return sig if not len(loud) else sig[loud[0] * win:(loud[-1] + 1) * win]


def quietest_split(sig, win=480):
    """A splice point near the middle, at the quietest 30 ms - not mid-phoneme."""
    mid = len(sig) // 2
    lo, hi = max(0, mid - SR), min(len(sig) - win, mid + SR)
    return min(range(lo, hi, win // 4),
               key=lambda s: np.abs(sig[s:s + win]).mean())


def main():
    tmp = tempfile.mkdtemp(prefix="sapi-")
    pieces, t, short, long_ = [], 0.0, [], []
    for i, path in enumerate(synthesize(tmp)):
        utt = trim(read(path))
        cut = quietest_split(utt)
        for half, gap, marks in ((utt[:cut], SHORT_GAP, short),
                                 (utt[cut:], LONG_GAP, long_)):
            pieces.append(half)
            t += len(half) / float(SR)
            if i == len(PHRASES) - 1 and gap == LONG_GAP:
                break                          # no silence after the last word
            marks.append(round(t, 3))
            pieces.append(np.zeros(int(gap * SR), dtype=np.float32))
            t += gap
    sample = np.concatenate(pieces)

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    wav = os.path.join(OUT_DIR, "speech_sample.wav")
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((sample * 32767).astype(np.int16).tobytes())
    meta = {"sentences": len(PHRASES), "short_gap_s": SHORT_GAP,
            "long_gap_s": LONG_GAP, "short_gaps_start": short,
            "long_gaps_start": long_,
            "speech_ends": round(len(sample) / float(SR), 3)}
    with open(os.path.join(OUT_DIR, "speech_sample.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print("wrote %s: %.2f s, %d sentences, %d word gaps of %.2f s, "
          "%d sentence gaps of %.2f s" % (wav, meta["speech_ends"],
                                           len(PHRASES), len(short), SHORT_GAP,
                                           len(long_), LONG_GAP))


if __name__ == "__main__":
    sys.exit(main())
