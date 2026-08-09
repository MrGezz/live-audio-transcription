import sys
import numpy as np
import queue
import difflib
import argparse
import threading
import time
import warnings
import tkinter as tk
from datetime import datetime

from whisper_backends import create_backend, BackendError
from speech_gate import SpeechGate

# Console encoding
# -------------------------------
# Transcript lines contain characters outside cp1252 (U+2192 "->"). A real
# Windows console handles those fine, but a REDIRECTED stdout (> log.txt, a
# pipe, pythonw) falls back to the locale code page and the first print would
# raise UnicodeEncodeError inside the worker thread - which then dies silently
# while the overlay keeps running. Under pythonw both streams are None.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Argument parsing
# -------------------------------
parser = argparse.ArgumentParser(description="Live system audio transcription with optional overlay")
parser.add_argument("--translate", action="store_true", help="Translate to English")
parser.add_argument("--language", type=str, default="auto",
                    help="Spoken language as a Whisper code (en, ms, ja, ...), or auto (default) "
                         "to detect it on every buffer. Pin it when you know it: detection reruns "
                         "per buffer and can disagree with itself on short or noisy windows")
parser.add_argument("--save", action="store_true", help="Save transcript to text file")
parser.add_argument("--output", type=str, default=None, help="Output text file path")
parser.add_argument("--buffer", type=int, default=4, help="Rolling buffer length in seconds")
parser.add_argument("--slide", type=int, default=2, help="Sliding step in seconds")
parser.add_argument("--no-overlay", action="store_true", help="Disable live overlay window")
parser.add_argument("--backend", type=str, default="auto", choices=["auto", "server", "local"],
                    help="Transcription backend: server (whisper.cpp GPU), local (CPU faster-whisper), "
                         "or auto - server when it is up, CPU while it is not, switching either way on its own")
parser.add_argument("--server-url", type=str, default="http://127.0.0.1:8080",
                    help="whisper-server URL (see start_whisper_server.bat)")
parser.add_argument("--model", type=str, default=r"_models\faster-whisper-medium",
                    help="faster-whisper model path (local/CPU backend only)")
parser.add_argument("--silence-threshold", type=float, default=0.01,
                    help="Mean-abs level below which a buffer is skipped as silence (default 0.01)")
parser.add_argument("--no-vad", action="store_true",
                    help="Disable Silero voice-activity detection and gate on loudness alone. "
                         "Loud non-speech (fans, music) then reaches Whisper, which invents "
                         "text for it")
parser.add_argument("--vad-threshold", type=float, default=0.5,
                    help="Silero speech probability above which a 32 ms frame counts as "
                         "speech (default 0.5; lower catches quieter speech and more noise). "
                         "For singing, lower --vad-min-speech-ms instead - see its help")
parser.add_argument("--vad-min-speech-ms", type=float, default=250,
                    help="How much speech a buffer needs before it is transcribed "
                         "(default 250 ms = 8 frames). Lower this for sung vocals: at the "
                         "default threshold, measured over 4 s windows, speech scores 35-97 "
                         "frames and singing only 0-15, while fans and room tone score exactly "
                         "0 - so 60 catches most singing and still cannot let steady noise "
                         "through. Lowering --vad-threshold does NOT work here; it admits room "
                         "tone at about the same point it admits music")
parser.add_argument("--vad-model", type=str, default=None,
                    help="Path to a silero_vad*.onnx. Defaults to the one bundled with "
                         "faster-whisper")
parser.add_argument("--capture", type=str, default="loopback", choices=["loopback", "input"],
                    help="loopback: WASAPI-capture any OUTPUT device (headset, speakers - no Stereo Mix needed). "
                         "input: classic recording device (Stereo Mix / microphone)")
args = parser.parse_args()

BUFFER_LENGTH_SEC = args.buffer
BUFFER_SLIDE_SEC = args.slide
TRANSLATE = args.translate
SAVE_TO_FILE = args.save
OUTPUT_FILE = args.output
SHOW_OVERLAY = not args.no_overlay
SILENCE_THRESHOLD = args.silence_threshold

if SAVE_TO_FILE and OUTPUT_FILE is None:
    OUTPUT_FILE = f"transcript_{datetime.now():%Y%m%d_%H%M%S}.txt"

outfile = open(OUTPUT_FILE, "a", encoding="utf-8") if SAVE_TO_FILE else None
if outfile:
    print(f"Saving transcript to: {OUTPUT_FILE}")

# Create transcription backend
# -------------------------------
# GPU path: whisper.cpp whisper-server (Vulkan or CUDA build)
# CPU path: faster-whisper int8 fallback
try:
    backend = create_backend(args.backend, server_url=args.server_url, model_path=args.model,
                             language=args.language)
except BackendError as e:
    raise SystemExit(f"Backend error: {e}")

# Speech gate
# -------------------------------
# Built once, used by the worker. create() returns None (with one line saying
# why) when onnxruntime or the model is missing, and the worker falls back to
# the loudness threshold - a server-only install without faster-whisper still
# runs, just without the VAD.
speech_gate = None
if not args.no_vad:
    speech_gate = SpeechGate.create(model_path=args.vad_model,
                                    threshold=args.vad_threshold,
                                    min_speech_ms=args.vad_min_speech_ms)
    if speech_gate is not None:
        print(f"Speech gate: Silero VAD (threshold {speech_gate.threshold}, "
              f"needs {speech_gate.min_frames} frames = "
              f"{args.vad_min_speech_ms:.0f} ms of speech)")


# Audio plumbing
# -------------------------------
SAMPLERATE = 16000            # Whisper input rate; capture is resampled to this
audio_queue = queue.Queue()
result_queue = queue.Queue()  # worker -> UI thread (only the UI thread touches Tk)
stop_event = threading.Event()

def enqueue_audio(data, src_rate):
    """Downmix to mono, resample to 16 kHz, and queue. data: (frames,) or (frames, channels)."""
    if data.ndim > 1 and data.shape[1] > 1:
        audio = data.mean(axis=1)
    else:
        audio = np.asarray(data, dtype=np.float32).reshape(-1)
    if src_rate != SAMPLERATE:
        n_out = int(len(audio) * SAMPLERATE / src_rate)
        audio = np.interp(
            np.linspace(0.0, len(audio), n_out, endpoint=False),
            np.arange(len(audio)),
            audio,
        )
    # Deliberately NOT normalized here. Scaling every ~128 ms chunk to full
    # scale destroys the only evidence --silence-threshold and the VAD have to
    # work with: quiet room tone measures 0.0064 raw - below the threshold, so
    # it should be skipped - and 0.2214 once normalized, which is
    # indistinguishable from speech at 0.2279. It is also a crude per-chunk
    # AGC that pumps the gain around inside a single word. The window is
    # normalized once instead, in the worker, right before inference.
    audio_queue.put(audio.astype(np.float32))

# Capture: WASAPI loopback (default) or classic input device
# -------------------------------
capture_thread = None
stream = None

if args.capture == "loopback":
    # Capture what an OUTPUT device is playing (Jabra headset, Realtek speakers, ...)
    # via WASAPI loopback. No Stereo Mix required; follows whichever device you pick.
    try:
        import soundcard as sc
    except ImportError:
        raise SystemExit("Loopback capture needs the 'soundcard' package: pip install soundcard "
                         "(or run with --capture input to use Stereo Mix)")
    warnings.filterwarnings("ignore", message=".*data discontinuity.*")

    speakers = sc.all_speakers()
    default_spk = sc.default_speaker()
    print("Output devices (loopback capture - pick the one you LISTEN on):")
    for i, s in enumerate(speakers):
        marker = "  <- Windows default" if s.id == default_spk.id else ""
        print(f"{i} {s.name}{marker}")
    while True:
        sel = input(f"Select output device [Enter = default: {default_spk.name}]: ").strip()
        if sel == "":
            spk = default_spk
            break
        try:
            idx = int(sel)
        except ValueError:
            print("Invalid choice - enter a number from the list, or Enter for default.")
            continue
        # Explicit range check: speakers[-1] would silently pick the last device.
        if 0 <= idx < len(speakers):
            spk = speakers[idx]
            break
        print(f"Invalid choice - enter 0-{len(speakers) - 1}, or Enter for default.")
    loop_mic = sc.get_microphone(str(spk.id), include_loopback=True)
    print(f"Capturing loopback of: {spk.name}")

    def capture_loop():
        # Prefer capturing straight at 16 kHz (WASAPI auto-converts);
        # fall back to 48 kHz + local resample if the driver refuses.
        for rec_sr in (SAMPLERATE, 48000):
            try:
                with loop_mic.recorder(samplerate=rec_sr, channels=2) as rec:
                    if rec_sr != SAMPLERATE:
                        print(f"[audio] loopback running at {rec_sr} Hz (resampling to {SAMPLERATE} Hz)")
                    frames = int(rec_sr * 0.128)  # ~128 ms blocks
                    while not stop_event.is_set():
                        enqueue_audio(rec.record(numframes=frames), rec_sr)
                return
            except Exception as e:
                if stop_event.is_set():
                    return
                print(f"[audio] loopback at {rec_sr} Hz failed: {e}")
        print("[audio] Loopback capture failed entirely - try --capture input (Stereo Mix).")

    capture_thread = threading.Thread(target=capture_loop, name="capture", daemon=True)
    capture_thread.start()
    print("Streaming system audio... Press Ctrl+C to stop")

else:
    # Classic recording-device path (Stereo Mix, microphones, Line In)
    import sounddevice as sd

    devices = sd.query_devices()
    print("Available audio devices:")
    for i, dev in enumerate(devices):
        print(i, dev['name'], "Input channels:", dev['max_input_channels'])

    while True:
        sel = input("Enter input device ID (e.g. Stereo Mix): ").strip()
        try:
            device_id = int(sel)
        except ValueError:
            print("Invalid choice - enter one of the device IDs listed above.")
            continue
        # Reject output-only devices here rather than in query_devices() below.
        if 0 <= device_id < len(devices) and devices[device_id]['max_input_channels'] > 0:
            break
        print(f"Invalid choice - enter 0-{len(devices) - 1} for a device with at least 1 input channel.")

    # Open at the device's NATIVE rate/channels (many WDM/MME devices reject
    # a forced 16 kHz -> PaErrorCode -9997), then resample to 16 kHz mono.
    dev_info = sd.query_devices(device_id, "input")
    NATIVE_SAMPLERATE = int(dev_info["default_samplerate"])
    CHANNELS = max(1, min(2, int(dev_info["max_input_channels"])))
    BLOCKSIZE = int(NATIVE_SAMPLERATE * 0.128)  # ~128 ms blocks
    print(f"Device native rate: {NATIVE_SAMPLERATE} Hz, channels: {CHANNELS} -> resampling to {SAMPLERATE} Hz mono")

    def audio_callback(indata, frames, time_info, status):
        enqueue_audio(indata, NATIVE_SAMPLERATE)

    stream = sd.InputStream(
        channels=CHANNELS,
        samplerate=NATIVE_SAMPLERATE,
        device=device_id,
        blocksize=BLOCKSIZE,
        callback=audio_callback
    )
    stream.start()
    print("Streaming system audio... Press Ctrl+C to stop")

# Transcription worker (background thread)
# -------------------------------
# All slow work lives here: buffering, HTTP/GPU inference, file writes.
# Results are posted to result_queue; the Tk thread only updates the label.
def transcription_worker():
    global outfile           # cleared below if the transcript file goes away
    buffer = np.zeros(0, dtype=np.float32)
    prev_window = []           # normalized keys from the previous transcribe call
    last_silence_report = 0.0
    last_perf_report = 0.0
    last_skip_report = 0.0
    consecutive_errors = 0

    def normalize(text):
        return "".join(c for c in text.lower() if c.isalnum() or c.isspace()).strip()

    def is_duplicate(key):
        # Overlapping windows (--slide < --buffer) transcribe the same speech
        # twice with small wording differences; fuzzy-match instead of ==.
        # Only the immediately previous window can overlap, so matching against
        # a longer history just eats deliberate repeats: at 0.80, "yes" vs
        # "yeah" scores 0.86 and would silently vanish.
        return any(difflib.SequenceMatcher(None, key, prev).ratio() > 0.80
                   for prev in prev_window)

    while not stop_event.is_set():
        try:
            chunk = audio_queue.get(timeout=0.25)
        except queue.Empty:
            continue
        buffer = np.concatenate((buffer, chunk))
        # Drain whatever else is already queued
        while not audio_queue.empty():
            try:
                buffer = np.concatenate((buffer, audio_queue.get_nowait()))
            except queue.Empty:
                break

        if len(buffer) < SAMPLERATE * BUFFER_LENGTH_SEC:
            continue

        # Real-time catch-up, BEFORE transcribing. The drain above pulls the
        # whole backlog into the buffer, so trimming afterwards never bounds
        # what transcribe() actually sees: each pass would hand it 4s plus
        # everything captured during the previous (slower) call, which feeds
        # back on itself and grows without limit once the GPU stops keeping up.
        max_len = SAMPLERATE * (BUFFER_LENGTH_SEC + 2 * BUFFER_SLIDE_SEC)
        if len(buffer) > max_len:
            behind = (len(buffer) - SAMPLERATE * BUFFER_LENGTH_SEC) / float(SAMPLERATE)
            buffer = buffer[-SAMPLERATE * BUFFER_LENGTH_SEC:]
            if time.monotonic() - last_skip_report > 15.0:
                last_skip_report = time.monotonic()
                print(f"[perf] {behind:.1f}s behind real time - skipped ahead to stay live")

        # Two-stage gate, cheapest test first. The level check rejects true
        # silence for free; the VAD then rejects audio that is loud but not
        # speech - fans, music, keyboard, room tone - which is exactly what
        # Whisper invents text over. Both reject before any inference is paid
        # for, so a quiet room costs nothing instead of a full pass plus a
        # hallucinated caption.
        # speech_frames() rather than has_speech() only so the skip message can
        # report how far short the buffer fell - it is the same single pass, and
        # "6 of the 8 frames needed" tells you what to set --vad-min-speech-ms
        # to, where a bare "no speech detected" leaves you guessing.
        level = float(np.mean(np.abs(buffer)))
        frames = None
        if level < SILENCE_THRESHOLD:
            skip = "silence"
        elif speech_gate is not None:
            frames = speech_gate.speech_frames(buffer)
            skip = "no-speech" if frames < speech_gate.min_frames else None
        else:
            skip = None

        if skip is None:
            # Normalized once, for the whole window, instead of per chunk on
            # the way in - see enqueue_audio().
            peak = float(np.max(np.abs(buffer)))
            audio = buffer / peak if peak > 0.02 else buffer
            t0 = time.monotonic()
            try:
                results = backend.transcribe(audio, translate=TRANSLATE)
                consecutive_errors = 0
            except Exception as e:
                # A server that died mid-session fails every pass (~every
                # --slide seconds); log the first few, then only occasionally.
                consecutive_errors += 1
                if consecutive_errors <= 3 or consecutive_errors % 10 == 0:
                    print(f"Error ({consecutive_errors} in a row): {e}")
                results = []
            infer_s = time.monotonic() - t0
            if infer_s > BUFFER_SLIDE_SEC and time.monotonic() - last_perf_report > 15.0:
                last_perf_report = time.monotonic()
                # Ask which backend is actually serving. On --backend auto that
                # changes mid-session, and naming the wrong one sends people to
                # tune a server that is not even running.
                lag = (f"[perf] inference {infer_s:.1f}s per {BUFFER_LENGTH_SEC}s buffer "
                       f"(> slide {BUFFER_SLIDE_SEC}s) - ")
                if backend.active_name == "server":
                    print(lag + "the GPU can't keep real-time pace with this model; restart the "
                                "server with a smaller/more-quantized model "
                                "(start_whisper_server.bat <model.bin>) for low-latency captions")
                else:
                    print(lag + "this is the CPU fallback, which is expected to lag. Start the GPU "
                                "server (start_whisper_server.bat) to get back to real time, or "
                                "point --model at a smaller faster-whisper model")
            # Keys for every segment, including ones dropped as duplicates, so
            # the next window still compares against the full previous window.
            keys = [normalize(text) for text, _ in results]
            for (text, lang_code), key in zip(results, keys):
                if key and not is_duplicate(key):
                    print(f"[{lang_code}→EN] {text}") if TRANSLATE else print(f"[{lang_code}→{lang_code.upper()}] {text}")
                    if outfile:
                        try:
                            outfile.write(text + "\n")
                            outfile.flush()
                        except OSError as e:
                            # Disk full, or --save pointed at a removable drive
                            # that got unplugged. This runs outside the
                            # transcribe() try, so an unguarded raise here would
                            # kill the worker thread outright.
                            print(f"[save] transcript write failed, saving disabled: {e}")
                            outfile = None
                    result_queue.put(text)
            prev_window = keys
        else:
            # Loud feedback instead of silent skipping (throttled to every 10 s).
            # The two reasons need opposite advice: "too quiet" means the capture
            # is wrong, "audible but not speech" usually means it is working.
            now = time.monotonic()
            if now - last_silence_report > 10.0:
                last_silence_report = now
                if skip == "silence":
                    print(f"[audio] level {level:.5f} < threshold {SILENCE_THRESHOLD} — capturing silence. "
                          "If audio IS playing: check you picked the output device you are listening on, "
                          "raise Windows volume, or lower --silence-threshold.")
                elif frames == 0:
                    # Nothing in the window cleared the threshold at all, so
                    # asking for fewer frames cannot help - there are none to
                    # ask for. Instrumental passages and steady noise land here.
                    print(f"[audio] audible (level {level:.5f}) but Silero found no speech at "
                          "all — skipping. Loosening --vad-min-speech-ms cannot help, since "
                          "there is no frame to count. Use --no-vad to transcribe it anyway "
                          "(instrumentals, heavily processed vocals), or lower --vad-threshold "
                          "if you believe there is quiet speech in there.")
                else:
                    print(f"[audio] audible (level {level:.5f}) but only {frames} of the "
                          f"{speech_gate.min_frames} speech frames needed — skipping. "
                          f"Sung vocals score far below speech; try --vad-min-speech-ms "
                          f"{int(frames * 32)} — not --vad-threshold, which lets room tone in "
                          "at about the same point it lets music in. --no-vad disables the "
                          "check entirely.")

        # Slide stays after transcribe so consecutive windows keep overlapping.
        buffer = buffer[int(SAMPLERATE * BUFFER_SLIDE_SEC):]

worker = threading.Thread(target=transcription_worker, name="transcriber", daemon=True)
worker.start()

# Tkinter overlay setup (optional)
# -------------------------------
if SHOW_OVERLAY:
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-transparentcolor", "black")
    root.configure(bg="black")
    # Position at center-bottom of the screen
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    #x_pos = int(screen_width / 2 - 400)   # assuming wraplength=800
    #y_pos = int(screen_height * 0.85)     # appx. 15% from bottom
    #root.geometry(f"+{x_pos}+{y_pos}")
    root.geometry(f"{screen_width}x100+0+{int(screen_height*0.85)}")  # full width, 15% from bottom

    text_var = tk.StringVar()
    label = tk.Label(
        root,
        textvariable=text_var,
        font=("Segoe UI", 24, "bold"),
        fg="white",
        bg="black",
        justify="center", # left, center, right
        anchor="center",   # center the text in the label box
        wraplength=800
    )

    # Make overlay draggable
    def start_move(event):
        root.x = event.x
        root.y = event.y

    def do_move(event):
        dx = event.x - root.x
        dy = event.y - root.y
        x = root.winfo_x() + dx
        y = root.winfo_y() + dy
        root.geometry(f"+{x}+{y}")

    label.bind("<Button-1>", start_move)
    label.bind("<B1-Motion>", do_move)
    root.attributes("-alpha", 0.8)
    label.configure(bg="black")
    #label.pack(padx=20, pady=20, fill="x") # fill horizontally
    label.pack(fill="both", expand=True)

    # UI pump: drain worker results; never blocks, overlay stays smooth
    def poll_results():
        try:
            while True:
                text_var.set(result_queue.get_nowait())
        except queue.Empty:
            pass
        # The worker is a daemon thread. If it ever dies, mainloop() would
        # otherwise keep running forever showing the last caption, while the
        # capture thread keeps filling audio_queue with nobody draining it.
        # The console path already exits via `while worker.is_alive()`.
        if not worker.is_alive() and not stop_event.is_set():
            print("[worker] transcription thread stopped - closing overlay")
            text_var.set("Transcription stopped - see console")
            stop_event.set()
            root.after(2000, root.destroy)
            return
        root.after(100, poll_results)

# Run overlay or console loop
# -------------------------------
try:
    if SHOW_OVERLAY:
        root.after(100, poll_results)
        root.mainloop()
    else:
        # Console-only: worker prints everything; just idle until Ctrl+C
        while worker.is_alive():
            time.sleep(0.5)
except KeyboardInterrupt:
    print("Stopping transcription...")
finally:
    stop_event.set()
    worker.join(timeout=5)
    if capture_thread is not None:
        capture_thread.join(timeout=5)
    if stream is not None:
        stream.stop()
        stream.close()
    if outfile:
        outfile.close()
    print("Stream closed.")
