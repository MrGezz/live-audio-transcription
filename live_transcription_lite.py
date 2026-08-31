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
# auto: whisper.cpp server (GPU: Vulkan or CUDA build) -> CPU faster-whisper fallback
try:
    backend = create_backend("auto",
                             server_url="http://127.0.0.1:8771",
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

# Pick a capture path
# -------------------------------
# Two, and loopback is offered first because it is the one that needs no
# setup: WASAPI loopback records what an OUTPUT device is playing, so it
# transcribes the speakers on any machine. Stereo Mix - the recording-device
# route below - is disabled by default on current Windows and absent from many
# drivers entirely, so "enter your Stereo Mix ID" used to be the only thing
# this script would accept and there was frequently no such device to enter.
#
# LoopbackSource comes from audio_sources.py rather than being written again
# here. It hands back exactly what this script's callback produces - float32
# mono at 16 kHz - so it drops straight onto the same queue, and the COM
# apartment handling and the discontinuity-warning filter that WASAPI needs
# are already right in it.
print("Capture from:")
print("  0  speakers (WASAPI loopback - no Stereo Mix needed)")
print("  1  a recording device (Stereo Mix, microphone)")
_mode = input("Choice [Enter = 0]: ").strip() or "0"

loopback = None
stream = None

if _mode == "0":
    import audio_sources

    try:
        loopback = audio_sources.LoopbackSource(
            {"device": ""}, audio_queue.put,
            lambda level, msg: print("[{0}] {1}".format(level, msg)))
    except audio_sources.AudioSourceError as e:
        raise SystemExit("Loopback capture failed: {0}".format(e))
    loopback.start()
    print("Capturing:", loopback.name)
else:
    devices = sd.query_devices()
    print("Available audio devices:")
    for i, dev in enumerate(devices):
        print(i, dev['name'], "Input channels:", dev['max_input_channels'])

    while True:
        _sel = input("Enter recording device ID: ").strip()
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
        # Not normalized here: scaling every ~128 ms chunk to full scale wipes
        # out the level that silence_threshold below is supposed to measure, so
        # quiet room tone arrives looking exactly as loud as speech and is
        # transcribed. The window is normalized once instead, before inference.
        audio_queue.put(audio)

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

            # Normalize the whole window at once - see audio_callback().
            peak = np.max(np.abs(buffer))
            audio = buffer / peak if peak > 0.02 else buffer

            # Transcribe current buffer (translate to English automatically)
            try:
                results = backend.transcribe(audio, translate=True)
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
    # Whichever one is holding the device. Both stops are idempotent, but only
    # one of these was ever created, so the other is None and must not be
    # touched - Ctrl+C landing on an AttributeError is a worse exit than none.
    if stream is not None:
        stream.stop()
        stream.close()
    if loopback is not None:
        loopback.stop()
