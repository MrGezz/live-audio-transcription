import sounddevice as sd
import numpy as np
from faster_whisper import WhisperModel
import queue
import argparse
import tkinter as tk
from datetime import datetime

# Argument parsing
# -------------------------------
parser = argparse.ArgumentParser(description="Live system audio transcription with optional overlay")
parser.add_argument("--translate", action="store_true", help="Translate to English")
parser.add_argument("--save", action="store_true", help="Save transcript to text file")
parser.add_argument("--output", type=str, default=None, help="Output text file path")
parser.add_argument("--buffer", type=int, default=4, help="Rolling buffer length in seconds")
parser.add_argument("--slide", type=int, default=2, help="Sliding step in seconds")
parser.add_argument("--no-overlay", action="store_true", help="Disable live overlay window")
args = parser.parse_args()

BUFFER_LENGTH_SEC = args.buffer
BUFFER_SLIDE_SEC = args.slide
TRANSLATE = args.translate
SAVE_TO_FILE = args.save
OUTPUT_FILE = args.output
SHOW_OVERLAY = not args.no_overlay

if SAVE_TO_FILE and OUTPUT_FILE is None:
    OUTPUT_FILE = f"transcript_{datetime.now():%Y%m%d_%H%M%S}.txt"

outfile = open(OUTPUT_FILE, "a", encoding="utf-8") if SAVE_TO_FILE else None
if outfile:
    print(f"Saving transcript to: {OUTPUT_FILE}")

# Load Faster-Whisper model
# -------------------------------
model_path = r"_models\faster-whisper-medium" # .bin model path
try:
    model = WhisperModel(
        model_path,
        device="cuda",
        compute_type="int8"
    )
except:
    model = WhisperModel(
        model_path,
        device="cpu",
        compute_type="int8"
    )
python live_transcription.py --translate

# Audio settings
# -------------------------------
SAMPLERATE = 16000
BLOCKSIZE = 2048
SILENCE_THRESHOLD = 0.01
audio_queue = queue.Queue()

# Audio callback
# -------------------------------
def audio_callback(indata, frames, time_info, status):
    audio = indata.mean(axis=1)
    max_amp = np.max(np.abs(audio))
    if max_amp > 0.02:
        audio = audio / max_amp
    audio_queue.put(audio)

# Select device
# -------------------------------
print("Available audio devices:")
for i, dev in enumerate(sd.query_devices()):
    print(i, dev['name'], "Input channels:", dev['max_input_channels'])

device_id = int(input("Enter Stereo Mix device ID: "))

# Start audio stream
# -------------------------------
stream = sd.InputStream(
    channels=2,
    samplerate=SAMPLERATE,
    device=device_id,
    blocksize=BLOCKSIZE,
    callback=audio_callback
)
stream.start()
print("Streaming system audio... Press Ctrl+C to stop")

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

# Transcription loop
# -------------------------------
buffer = np.zeros(0, dtype=np.float32)
last_text = ""

def update_overlay():
    global buffer, last_text
    try:
        while not audio_queue.empty():
            chunk = audio_queue.get_nowait()
            buffer = np.concatenate((buffer, chunk))

        if len(buffer) >= SAMPLERATE * BUFFER_LENGTH_SEC:
            if np.mean(np.abs(buffer)) >= SILENCE_THRESHOLD:
                segments, info = model.transcribe(
                    buffer,
                    language=None,
                    task="translate" if TRANSLATE else "transcribe",
                    word_timestamps=False,
                    beam_size=1
                )
                for segment in segments:
                    text = segment.text.strip()
                    if text and text != last_text:
                        last_text = text
                        # Print to console
                        # print(text)
                        lang_code = info.language if hasattr(info, 'language') else "??"
                        print(f"[{lang_code}→EN] {text}") if TRANSLATE else print(f"[{lang_code}→{lang_code.upper()}] {text}")
                        # Save to file if enabled
                        if outfile:
                            outfile.write(text + "\n")
                            outfile.flush()
                        # Update overlay if enabled
                        if SHOW_OVERLAY:
                            text_var.set(text)

            buffer = buffer[int(SAMPLERATE * BUFFER_SLIDE_SEC):]
    except Exception as e:
        print("Error:", e)

    if SHOW_OVERLAY:
        root.after(100, update_overlay)

# Run overlay or main loop
# -------------------------------
try:
    if SHOW_OVERLAY:
        root.after(100, update_overlay)
        root.mainloop()
    else:
        # Non-overlay loop
        while True:
            update_overlay()
except KeyboardInterrupt:
    print("Stopping transcription...")
finally:
    stream.stop()
    stream.close()
    if outfile:
        outfile.close()
    print("Stream closed.")
