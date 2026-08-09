import sys
import sounddevice as sd
import numpy as np
import queue

from whisper_backends import create_backend, BackendError

# Transcript lines contain characters outside cp1252 (U+2192 "->"), which a
# redirected stdout would refuse to encode. Same guard as live_transcription.py.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Create transcription backend
# -------------------------------
# auto: whisper.cpp server (Vulkan GPU, works on AMD) -> CPU faster-whisper fallback
try:
    backend = create_backend("auto",
                             server_url="http://127.0.0.1:8080",
                             model_path=r"_models\faster-whisper-medium")
except BackendError as e:
    raise SystemExit("Backend error: {0}".format(e))

# Audio settings
# -------------------------------
samplerate = 16000         # Whisper input rate (resampled to, not forced on device)
buffer_length_sec = 4      # 4-second rolling buffer
buffer_slide_sec = 2       # slide buffer by 2 seconds
silence_threshold = 0.01   # skip very quiet audio

# Queue for audio chunks
# -------------------------------
audio_queue = queue.Queue()

# Detect your Stereo Mix device
# -------------------------------
devices = sd.query_devices()
print("Available audio devices:")
for i, dev in enumerate(devices):
    print(i, dev['name'], "Input channels:", dev['max_input_channels'])

while True:
    _sel = input("Enter Stereo Mix device ID: ").strip()
    try:
        device_id = int(_sel)
    except ValueError:
        print("Invalid choice - enter one of the device IDs listed above.")
        continue
    if 0 <= device_id < len(devices) and devices[device_id]['max_input_channels'] > 0:
        break
    print(f"Invalid choice - enter 0-{len(devices) - 1} for a device with at least 1 input channel.")

# Open at the device's NATIVE rate/channels (many WDM/MME devices reject
# a forced 16 kHz -> PaErrorCode -9997), then resample to 16 kHz mono here.
dev_info = sd.query_devices(device_id, "input")
native_samplerate = int(dev_info["default_samplerate"])
channels = max(1, min(2, int(dev_info["max_input_channels"])))
blocksize = int(native_samplerate * 0.128)  # ~128 ms blocks
print(f"Device native rate: {native_samplerate} Hz, channels: {channels} -> resampling to {samplerate} Hz mono")

def audio_callback(indata, frames, time_info, status):
    # Downmix to mono
    if indata.ndim > 1 and indata.shape[1] > 1:
        audio = indata.mean(axis=1)
    else:
        audio = indata.reshape(-1).astype(np.float32)
    # Resample native rate -> 16 kHz (linear, fine for speech)
    if native_samplerate != samplerate:
        n_out = int(len(audio) * samplerate / native_samplerate)
        audio = np.interp(
            np.linspace(0.0, len(audio), n_out, endpoint=False),
            np.arange(len(audio)),
            audio,
        ).astype(np.float32)
    max_amp = np.max(np.abs(audio))
    if max_amp > 0.02:
        audio = audio / max_amp
    audio_queue.put(audio)

# Start audio stream
# -------------------------------
stream = sd.InputStream(
    channels=channels,
    samplerate=native_samplerate,
    device=device_id,
    blocksize=blocksize,
    callback=audio_callback
)
stream.start()
print("Streaming system audio... Press Ctrl+C to stop")

# Live transcription loop
# -------------------------------
buffer = np.zeros(0, dtype=np.float32)

try:
    while True:
        # Add new audio chunk
        chunk = audio_queue.get()
        buffer = np.concatenate((buffer, chunk))

        # Only process if buffer has enough audio
        if len(buffer) >= samplerate * buffer_length_sec:
            # Skip mostly silent audio
            if np.mean(np.abs(buffer)) < silence_threshold:
                buffer = buffer[int(samplerate * buffer_slide_sec):]  # slide buffer
                continue

            # Transcribe current buffer (translate to English automatically)
            try:
                results = backend.transcribe(buffer, translate=True)
            except Exception as e:
                # The auto backend re-raises the first few failures on purpose,
                # so it can tell a hiccup from a dead server. Dying here would
                # mean the CPU fallback never gets a chance to engage; it logs
                # the switch itself once it does.
                print("Error: {0}".format(e))
                results = []
            for text, lang in results:
                if text:
                    print(f"[{lang}→EN] {text}")

            # Slide the buffer forward for live streaming
            buffer = buffer[int(samplerate * buffer_slide_sec):]

except KeyboardInterrupt:
    print("Stopping transcription...")
    stream.stop()
    stream.close()
