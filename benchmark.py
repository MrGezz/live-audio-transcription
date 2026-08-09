"""
benchmark.py - measure real inference latency and pick --buffer / --slide.

Why this exists
---------------
The intuition "smaller buffer = lower latency" is wrong for Whisper. The
encoder always runs over a fixed 30-second padded window, so a 4-second buffer
costs almost exactly what a 25-second buffer costs. Tuning by feel therefore
tends to land on the worst setting available: a small buffer that cannot be
transcribed in time, which makes live_transcription.py skip audio to stay
current - silently dropping speech that was captured perfectly well.

This measures the machine actually in front of you, through the same
create_backend() the app uses, and reports the settings that hold real time.

Usage
-----
    python benchmark.py                                  # synthetic audio
    python benchmark.py --wav samples\\jfk.wav            # real speech (better)
    python benchmark.py --buffers 4,8,16,24 --reps 5
    python benchmark.py --backend local                  # benchmark the CPU path

The sustainability rule it applies is the one the worker loop actually obeys:
a pass transcribes --buffer seconds and consumes --slide seconds, so keeping
up requires inference <= slide, and overlap for the duplicate filter requires
slide < buffer.
"""

import argparse
import sys
import time
import wave

import numpy as np

from whisper_backends import create_backend, BackendError, SAMPLERATE

# Whisper pads every window to 30 s. Past that, whisper.cpp splits the audio
# into multiple windows and the flat-cost property this tool relies on stops
# holding, so a longer buffer stops being free.
WHISPER_WINDOW_SEC = 30

# Output stays ASCII on purpose: a plain Windows console is cp1252, and a
# stray arrow or multiplication sign raises UnicodeEncodeError mid-run.


def parse_args():
    p = argparse.ArgumentParser(
        description="Measure inference latency and recommend --buffer / --slide",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--buffers", type=str, default="4,8,16,24",
                   help="Comma-separated buffer lengths in seconds (default 4,8,16,24)")
    p.add_argument("--reps", type=int, default=3,
                   help="Timed repetitions per size, after a discarded warm-up (default 3)")
    p.add_argument("--wav", type=str, default=None,
                   help="WAV file of real speech. Strongly preferred: synthetic audio "
                        "decodes to almost no text and measures only the encoder floor")
    p.add_argument("--translate", action="store_true",
                   help="Benchmark the translate path (decoding differs, so timing does too)")
    p.add_argument("--language", type=str, default="auto",
                   help="Pin the spoken language, or auto (default). Pinning skips the "
                        "detection pass, so it is slightly cheaper")
    p.add_argument("--backend", type=str, default="auto", choices=["auto", "server", "local"],
                   help="Which backend to measure (default auto)")
    p.add_argument("--server-url", type=str, default="http://127.0.0.1:8080",
                   help="whisper-server URL")
    p.add_argument("--model", type=str, default=r"_models\faster-whisper-medium",
                   help="faster-whisper model path (local/CPU backend only)")
    p.add_argument("--headroom", type=float, default=1.5,
                   help="Safety factor applied to measured latency when recommending "
                        "--slide (default 1.5). Benchmarks run on an idle machine; live "
                        "capture shares the CPU with whatever is playing the audio, and "
                        "measured 7-12s against a 4.7s benchmark on one setup")
    p.add_argument("--min-overlap", type=int, default=3,
                   help="Seconds of overlap between consecutive windows to insist on "
                        "(default 3). This is what the duplicate filter matches against; "
                        "below about 3s Whisper re-segments differently enough that partial "
                        "repeats slip past it and show up twice in the transcript")
    return p.parse_args()


def load_wav(path):
    """Read a WAV to float32 mono 16 kHz, the format the backends expect."""
    wf = wave.open(path, "rb")
    try:
        if wf.getsampwidth() != 2:
            raise SystemExit(
                "benchmark.py needs 16-bit PCM WAV; '{0}' is {1}-bit. Convert it "
                "first, e.g. ffmpeg -i in.wav -ar 16000 -ac 1 -c:a pcm_s16le out.wav"
                .format(path, wf.getsampwidth() * 8))
        rate = wf.getframerate()
        channels = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    finally:
        wf.close()

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLERATE:
        n_out = int(len(audio) * SAMPLERATE / float(rate))
        audio = np.interp(
            np.linspace(0.0, len(audio), n_out, endpoint=False),
            np.arange(len(audio)),
            audio,
        ).astype(np.float32)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak > 0.02:
        audio = audio / peak
    return audio.astype(np.float32)


def synth_audio(seconds):
    """Formant-ish tones. Deterministic, dependency-free, and NOT speech."""
    t = np.arange(int(SAMPLERATE * seconds)) / float(SAMPLERATE)
    sig = np.zeros_like(t)
    for f in (140, 280, 560, 1100, 2200):
        sig += np.sin(2 * np.pi * f * t) / (f / 140.0)
    sig *= 0.5 + 0.5 * np.sin(2 * np.pi * 2.5 * t)
    peak = float(np.max(np.abs(sig))) or 1.0
    return (sig / peak).astype(np.float32)


def make_buffer(source, seconds):
    """Exactly `seconds` of audio, tiling the source if it is too short."""
    want = int(SAMPLERATE * seconds)
    if len(source) < want:
        source = np.tile(source, int(np.ceil(want / float(len(source)))))
    return np.ascontiguousarray(source[:want])


def median(values):
    s = sorted(values)
    n = len(s)
    if n == 0:
        return float("nan")
    if n % 2:
        return s[n // 2]
    return 0.5 * (s[n // 2 - 1] + s[n // 2])


def measure(backend, buf, translate, reps):
    """One warm-up plus `reps` timed passes. Returns (times, characters)."""
    times = []
    chars = 0
    for i in range(reps + 1):
        t0 = time.monotonic()
        results = backend.transcribe(buf, translate=translate)
        dt = time.monotonic() - t0
        if i:                       # discard the warm-up: first call pays for
            times.append(dt)        # model load, graph build and cache fill
            chars += sum(len(text) for text, _ in results)
    return times, chars


def slide_for(row, headroom, min_overlap):
    """
    The slide this buffer can sustain, or None if it cannot sustain any.

    Two constraints, both from the worker loop:
      inference <= slide          or it falls behind and starts skipping audio
      slide <= buffer - overlap   or consecutive windows stop overlapping, and
                                  the duplicate filter has nothing to match on
    """
    slide = max(1, int(np.ceil(row["median"] * headroom)))
    if slide <= row["buffer"] - min_overlap:
        return slide
    return None


def recommend(rows, headroom, min_overlap):
    """
    Smallest buffer that holds real time, and the slide to run it at.

    Smallest, not fastest: buffer length IS the delay before a caption can
    appear, so once a size sustains, larger ones are strictly worse to watch.
    """
    for row in rows:
        slide = slide_for(row, headroom, min_overlap)
        if slide is not None:
            return row, slide
    return None, None


def main():
    args = parse_args()

    try:
        sizes = sorted(set(int(s) for s in args.buffers.split(",") if s.strip()))
    except ValueError:
        raise SystemExit("--buffers must be comma-separated whole seconds, e.g. 4,8,16,24")
    if not sizes or min(sizes) < 1:
        raise SystemExit("--buffers must contain at least one size of 1 second or more")
    if args.reps < 1:
        raise SystemExit("--reps must be at least 1")

    if args.wav:
        source = load_wav(args.wav)
        audio_desc = "{0} ({1:.1f}s of real audio, tiled as needed)".format(
            args.wav, len(source) / float(SAMPLERATE))
        synthetic = False
    else:
        source = synth_audio(30.0)
        audio_desc = "synthetic tones - ENCODER FLOOR ONLY, see the warning below"
        synthetic = True

    try:
        backend = create_backend(args.backend, server_url=args.server_url,
                                 model_path=args.model, language=args.language)
    except BackendError as e:
        raise SystemExit("Backend error: {0}".format(e))

    started_on = getattr(backend, "active_name", args.backend)

    print("")
    print("=" * 70)
    print(" INFERENCE BENCHMARK")
    print("=" * 70)
    print(" Backend ....... {0} (running on: {1})".format(backend.name, started_on))
    print(" Audio ......... {0}".format(audio_desc))
    print(" Task .......... {0}".format("translate to English" if args.translate
                                        else "transcribe"))
    print(" Language ...... {0}".format(args.language))
    print(" Reps .......... {0} timed, plus 1 discarded warm-up, per size".format(args.reps))
    print(" Rules ......... slide >= {0:.2f}x inference, and at least {1}s of overlap"
          .format(args.headroom, args.min_overlap))
    if synthetic:
        print("")
        print(" [warn] Synthetic audio decodes to almost no text, so these numbers")
        print("        measure the encoder and little else. Real speech is slower.")
        print("        Re-run with --wav <file> for numbers you can quote.")
    # Tiling a short clip to fill a longer window splices its end straight back
    # onto its start. Whisper is prone to repetition loops on near-exact
    # repeats, and one costs many extra decoded tokens - which shows up as a
    # single wildly slow row that has nothing to do with the hardware.
    source_sec = len(source) / float(SAMPLERATE)
    tiled = [s for s in sizes if s > source_sec]
    if tiled and not synthetic:
        print("")
        print(" [warn] Source is only {0:.1f}s, so {1}s must be filled by repeating it."
              .format(source_sec, ", ".join(str(s) for s in tiled)))
        print("        Repeated audio can send Whisper into a repetition loop, which")
        print("        inflates that row's time. Treat outliers there as artifacts;")
        print("        for clean numbers use a clip at least {0}s long."
              .format(max(sizes)))

    over = [s for s in sizes if s > WHISPER_WINDOW_SEC]
    if over:
        print("")
        print(" [warn] {0}s exceeds Whisper's {1}s window, so it is split into"
              .format(", ".join(str(s) for s in over), WHISPER_WINDOW_SEC))
        print("        several windows and stops being flat-cost. Expect a jump.")
    print("")

    header = "  {0:>7}  {1:>8}  {2:>8}  {3:>8}  {4:>7}  {5:>7}  {6:>7}".format(
        "buffer", "median", "min", "max", "xRT", "chars", "slide")
    print(header)
    print("  " + "-" * (len(header) - 2))

    rows = []
    try:
        for secs in sizes:
            buf = make_buffer(source, secs)
            times, chars = measure(backend, buf, args.translate, args.reps)
            row = {
                "buffer": secs,
                "median": median(times),
                "min": min(times),
                "max": max(times),
                "chars": chars // args.reps,
            }
            row["xrt"] = row["median"] / float(secs)
            rows.append(row)
            # Printed per row rather than only for the winner, so the whole
            # trade-off is visible: which sizes work at all, and what each
            # one would cost in latency.
            slide = slide_for(row, args.headroom, args.min_overlap)
            print("  {0:>6}s  {1:>7.2f}s  {2:>7.2f}s  {3:>7.2f}s  {4:>6.2f}x  {5:>7}  {6:>7}"
                  .format(row["buffer"], row["median"], row["min"], row["max"],
                          row["xrt"], row["chars"],
                          "{0}s".format(slide) if slide else "-"))
    except KeyboardInterrupt:
        print("\n  (interrupted - reporting what completed)")
    except Exception as e:
        print("\n  [error] {0}".format(e))
        if not rows:
            raise SystemExit("No measurements completed.")

    ended_on = getattr(backend, "active_name", started_on)
    print("")
    if ended_on != started_on:
        print(" [warn] The backend changed from '{0}' to '{1}' during the run, so"
              .format(started_on, ended_on))
        print("        these rows are not all the same hardware. Re-run before")
        print("        trusting them.")
        print("")

    print("-" * 70)
    if not rows:
        return 1

    best, slide = recommend(rows, args.headroom, args.min_overlap)
    if best is None:
        fastest = min(rows, key=lambda r: r["xrt"])
        print(" NOT SUSTAINABLE at any size tested.")
        print("")
        print(" Closest was {0}s buffer at {1:.2f}s median ({2:.2f}x realtime). A size"
              .format(fastest["buffer"], fastest["median"], fastest["xrt"]))
        print(" only works once inference fits under it with room for the overlap the")
        print(" duplicate filter needs, so it has to come in well below 1.00x.")
        print("")
        print(" Options, in order of effect:")
        print("   1. Use a smaller/more-quantized model - the single biggest lever.")
        print("      start_whisper_server.bat ggml-base-q5_1.bin   (or a tiny build)")
        if ended_on == "local":
            print("   2. Start the GPU server: this run measured the CPU fallback.")
        else:
            print("   2. Confirm the server really is on the GPU - its startup log")
            print("      names the device - and not silently running on CPU.")
        print("   3. Try larger --buffers: cost is near-flat up to {0}s, so a longer"
              .format(WHISPER_WINDOW_SEC))
        print("      window can sustain where a short one cannot.")
        print("-" * 70)
        return 1

    overlap = best["buffer"] - slide
    print(" RECOMMENDED:  --buffer {0} --slide {1}".format(best["buffer"], slide))
    print("")
    print("   inference {0:.2f}s per {1}s window, run every {2}s"
          .format(best["median"], best["buffer"], slide))
    print("   {0:.0f}% headroom before it starts skipping audio"
          .format((slide / best["median"] - 1.0) * 100.0))
    print("   {0}s overlap between windows for the duplicate filter".format(overlap))
    print("   about {0}s from speech to caption".format(best["buffer"]))
    print("")
    print("   python live_transcription.py --buffer {0} --slide {1}"
          .format(best["buffer"], slide))
    if synthetic:
        print("")
        print(" Re-check this with --wav before relying on it: real speech decodes")
        print(" more tokens than tones do, and decoding is the part that varies.")
    print("")
    print(" This was measured with nothing else running. Live capture competes with")
    print(" whatever is playing the audio, so treat it as a starting point: if the")
    print(" app prints a [perf] line during real use, raise --slide until it stops.")
    print("-" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
