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

# Argument parsing
# -------------------------------
parser = argparse.ArgumentParser(description="Live system audio transcription with optional overlay")
parser.add_argument("--translate", action="store_true", help="Translate to English")
parser.add_argument("--save", action="store_true", help="Save transcript to text file")
parser.add_argument("--output", type=str, default=None, help="Output text file path")
parser.add_argument("--buffer", type=int, default=4, help="Rolling buffer length in seconds")
parser.add_argument("--slide", type=int, default=2, help="Sliding step in seconds")
parser.add_argument("--no-overlay", action="store_true", help="Disable live overlay window")
parser.add_argument("--backend", type=str, default="auto", choices=["auto", "server", "local"],
                    help="Transcription backend: whisper.cpp server (GPU/Vulkan), local CPU faster-whisper, or auto")
parser.add_argument("--server-url", type=str, default="http://127.0.0.1:8080",
                    help="whisper-server URL (see start_whisper_server.bat)")
parser.add_argument("--model", type=str, default=r"_models\faster-whisper-medium",
                    help="faster-whisper model path (local/CPU backend only)")
parser.add_argument("--silence-threshold", type=float, default=0.01,
                    help="Mean-abs level below which a buffer is skipped as silence (default 0.01)")
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
# GPU path: whisper.cpp whisper-server with Vulkan (AMD Radeon Pro W5500 etc.)
# CPU path: faster-whisper int8 fallback
try:
    backend = create_backend(args.backend, server_url=args.server_url, model_path=args.model)
except BackendError as e:
    raise SystemExit(f"Backend error: {e}")


# Audio plumbing
# -------------------------------
SAMPLERATE = 16000            # Whisper input rate; capture is resampled to this
audio_queue = queue.Queue()
result_queue = queue.Queue()  # worker -> UI thread (only the UI thread touches Tk)
stop_event = threading.Event()

def enqueue_audio(data, src_rate):
    """Downmix to mono, resample to 16 kHz, normalize, and queue. data: (frames,) or (frames, channels)."""
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
    audio = audio.astype(np.float32)
    max_amp = np.max(np.abs(audio))
    if max_amp > 0.02:
        audio = audio / max_amp
    audio_queue.put(audio)

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
    sel = input(f"Select output device [Enter = default: {default_spk.name}]: ").strip()
    spk = default_spk if sel == "" else speakers[int(sel)]
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

    print("Available audio devices:")
    for i, dev in enumerate(sd.query_devices()):
        print(i, dev['name'], "Input channels:", dev['max_input_channels'])

    device_id = int(input("Enter input device ID (e.g. Stereo Mix): "))

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
    buffer = np.zeros(0, dtype=np.float32)
    recent_texts = []          # normalized recent lines for overlap dedupe
    last_silence_report = 0.0
    last_perf_report = 0.0

    def is_duplicate(text):
        # Overlapping windows (--slide < --buffer) transcribe the same speech
        # twice with small wording differences; fuzzy-match instead of ==.
        key = "".join(c for c in text.lower() if c.isalnum() or c.isspace()).strip()
        if not key:
            return True
        for prev in recent_texts:
            if difflib.SequenceMatcher(None, key, prev).ratio() > 0.80:
                return True
        recent_texts.append(key)
        del recent_texts[:-5]  # keep last 5
        return False

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

        level = float(np.mean(np.abs(buffer)))
        if level >= SILENCE_THRESHOLD:
            t0 = time.monotonic()
            try:
                results = backend.transcribe(buffer, translate=TRANSLATE)
            except Exception as e:
                print("Error:", e)
                results = []
            infer_s = time.monotonic() - t0
            if infer_s > BUFFER_SLIDE_SEC and time.monotonic() - last_perf_report > 15.0:
                last_perf_report = time.monotonic()
                print(f"[perf] inference {infer_s:.1f}s per {BUFFER_LENGTH_SEC}s buffer (> slide {BUFFER_SLIDE_SEC}s) - "
                      "GPU can't keep real-time pace with this model; try ggml-small-q5_1 "
                      "in start_whisper_server.bat for low-latency captions")
            for text, lang_code in results:
                if text and not is_duplicate(text):
                    print(f"[{lang_code}→EN] {text}") if TRANSLATE else print(f"[{lang_code}→{lang_code.upper()}] {text}")
                    if outfile:
                        outfile.write(text + "\n")
                        outfile.flush()
                    result_queue.put(text)
        else:
            # Loud feedback instead of silent skipping (throttled to every 10 s)
            now = time.monotonic()
            if now - last_silence_report > 10.0:
                last_silence_report = now
                print(f"[audio] level {level:.5f} < threshold {SILENCE_THRESHOLD} — capturing silence. "
                      "If audio IS playing: check you picked the output device you are listening on, "
                      "raise Windows volume, or lower --silence-threshold.")

        buffer = buffer[int(SAMPLERATE * BUFFER_SLIDE_SEC):]

        # Real-time catch-up: if inference falls behind, drop stale audio and
        # jump to the newest window instead of letting latency grow unbounded.
        max_len = SAMPLERATE * (BUFFER_LENGTH_SEC + 2 * BUFFER_SLIDE_SEC)
        if len(buffer) > max_len:
            behind = (len(buffer) - SAMPLERATE * BUFFER_LENGTH_SEC) / float(SAMPLERATE)
            buffer = buffer[-SAMPLERATE * BUFFER_LENGTH_SEC:]
            print(f"[perf] {behind:.1f}s behind real time - skipped ahead to stay live")

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
