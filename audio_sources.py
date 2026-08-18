"""
audio_sources.py - every way audio gets into this program, behind one object.

Why this exists
---------------
live_transcription.py inlines its two capture paths at import time and picks
the device with input() on the console. That is fine for one person at one
terminal and impossible for anything else:

  * a browser cannot answer a console prompt, so the web UI could never offer
    the device list the CLI already prints
  * capture is built once, at module level, so switching device or mode means
    killing the process and starting over - which also drops the loaded model
  * the two paths do not agree on anything. Loopback resamples in the capture
    loop, input resamples in a PortAudio callback, and both call the same
    enqueue_audio() by closure rather than by argument

Here each path is an AudioSource: build it, start() it, stop() it, build the
next one. It hands finished audio to a callback instead of reaching into a
global queue, so the same four sources serve the CLI, the overlay and the web
UI without knowing any of them exist.

What is preserved verbatim from the old script, because it was all learned the
hard way and none of it is obvious:

  * loopback tries 16 kHz first and only falls back to 48 kHz, filters
    soundcard's per-block "data discontinuity" warning, and says which rate it
    settled on when it is not 16 kHz
  * input opens at the device's NATIVE rate and channel count, because many
    WDM/MME devices answer a forced 16 kHz with PaErrorCode -9997
  * audio is downmixed and resampled but NOT normalized - see to_mono_16k()
  * device selection range-checks explicitly, since devices[-1] is a valid
    index that silently opens the wrong device

list_devices() must never raise: a machine without the soundcard package still
has recording devices, and the UI still has to show them.
"""

import contextlib
import ctypes
import os
import threading
import time
import warnings
import wave

import numpy as np

SAMPLERATE = 16000          # Whisper's input rate; everything resamples to it

# ~128 ms per block. Small enough that the worker's buffer fills smoothly,
# large enough that a 4 s window is 31 callbacks rather than hundreds.
BLOCK_SEC = 0.128

# How long stop() waits for a capture thread. Anything that has not noticed
# the stop flag by now is stuck inside a driver call, and hanging the UI on it
# is worse than leaking one daemon thread.
STOP_TIMEOUT = 5.0

_FFMPEG_HINT = ("ffmpeg -i \"{0}\" -ar 16000 -ac 1 -c:a pcm_s16le out.wav")

# Every soundcard call is a COM call and COM is per THREAD, but soundcard
# initializes only the one thread that imports it. That was enough for the old
# script, which never imported sounddevice at all in loopback mode. It is not
# enough here, because this module drives both backends in one process: if
# PortAudio gets to COM first, soundcard's own CoInitializeEx comes back
# RPC_E_CHANGED_MODE, it gives up, and every soundcard call from any thread
# but the main one fails with CO_E_NOTINITIALIZED, 0x800401f0. Measured: after
# one InputSource, switching capture to loopback recorded 0 blocks and
# list_devices() on the web server's thread reported 0 output devices - the
# two things this module exists to make possible.
_COINIT_MULTITHREADED = 0x0
_RPC_E_CHANGED_MODE = -2147417850       # 0x80010106 as a signed HRESULT


@contextlib.contextmanager
def _com_thread():
    """A COM apartment for the calling thread, for the length of the block."""
    ole32 = getattr(getattr(ctypes, "windll", None), "ole32", None)
    hresult = None
    if ole32 is not None:       # not Windows: soundcard is not COM there
        hresult = ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
    try:
        yield
    finally:
        # Balance only an init that took. After RPC_E_CHANGED_MODE this
        # thread's apartment belongs to whoever set it, and uninitializing it
        # would pull COM out from under them.
        if hresult is not None and hresult != _RPC_E_CHANGED_MODE:
            ole32.CoUninitialize()


class AudioSourceError(RuntimeError):
    """
    Capture cannot be set up at all: package missing, file unreadable, no
    device. Raised only while BUILDING a source, never from a running one -
    a capture thread reports through on_log and degrades instead.
    """


def to_mono_16k(data, src_rate):
    """
    Downmix to mono and resample to 16 kHz. data: (frames,) or (frames, ch).

    Deliberately NOT normalized here. Scaling every ~128 ms chunk to full
    scale destroys the only evidence the silence threshold and the VAD have to
    work with: quiet room tone measures 0.0064 raw - below the threshold, so
    it should be skipped - and 0.2214 once normalized, which is
    indistinguishable from speech at 0.2279. It is also a crude per-chunk AGC
    that pumps the gain around inside a single word. The pipeline normalizes
    the whole window once instead, right before inference.
    """
    audio = np.asarray(data, dtype=np.float32)
    if audio.ndim > 1 and audio.shape[1] > 1:
        audio = audio.mean(axis=1)
    else:
        audio = audio.reshape(-1)
    if src_rate != SAMPLERATE and len(audio):
        n_out = int(len(audio) * SAMPLERATE / src_rate)
        audio = np.interp(
            np.linspace(0.0, len(audio), n_out, endpoint=False),
            np.arange(len(audio)),
            audio,
        )
    return audio.astype(np.float32)


# -------------------------------
# Enumeration
# -------------------------------
def _list_loopback(out):
    """Output devices, for WASAPI loopback. Appends its own failures."""
    try:
        import soundcard as sc
    except ImportError:
        out["errors"].append(
            "Loopback capture unavailable: the 'soundcard' package is not "
            "installed (pip install soundcard). Recording devices below are "
            "unaffected.")
        return
    with _com_thread():
        try:
            speakers = sc.all_speakers()
        except Exception as e:
            out["errors"].append(
                "Could not list output devices: {0}".format(e))
            return

        default_id = ""
        try:
            default_id = str(sc.default_speaker().id)
        except Exception as e:
            # Every headset unplugged, or no audio endpoint at all. The
            # list is still worth returning; nothing is simply marked as
            # the default.
            out["errors"].append("No default output device: {0}".format(e))

        for spk in speakers:
            channels = getattr(spk, "channels", 2)
            if isinstance(channels, (list, tuple)):
                channels = len(channels)
            out["loopback"].append({
                "id": str(spk.id),
                "name": spk.name,
                "default": str(spk.id) == default_id,
                "channels": int(channels or 2),
            })


def _list_input(out):
    """Recording devices, for sounddevice. Appends its own failures."""
    try:
        import sounddevice as sd
    except ImportError:
        out["errors"].append(
            "Recording devices unavailable: the 'sounddevice' package is not "
            "installed (pip install sounddevice).")
        return
    try:
        devices = sd.query_devices()
    except Exception as e:
        out["errors"].append("Could not list recording devices: {0}".format(e))
        return

    default_index = -1
    try:
        default_index = int(sd.query_devices(kind="input")["index"])
    except Exception:
        pass        # no default recording device; not worth an error line

    for index, dev in enumerate(devices):
        if int(dev["max_input_channels"]) <= 0:
            continue        # output-only endpoint; opening it fails later
        try:
            hostapi = sd.query_hostapis(dev["hostapi"])["name"]
        except Exception:
            hostapi = ""
        out["input"].append({
            # A string, like the loopback ids, because settings["device"] is
            # one field for both kinds and soundcard has nothing but GUIDs.
            "id": str(index),
            "name": dev["name"],
            "default": index == default_index,
            "channels": int(dev["max_input_channels"]),
            "samplerate": int(dev["default_samplerate"]),
            "hostapi": hostapi,
        })


def list_devices():
    """
    Everything this machine can capture from:

        {"loopback": [{id, name, default, channels}],
         "input":    [{id, name, default, channels, samplerate, hostapi}],
         "errors":   ["why one of the two lists is short", ...]}

    Never raises. A missing package, a dead audio service or an unplugged
    headset each cost one line in "errors" and leave the other list intact,
    because a UI that cannot enumerate is a UI that cannot offer the fallback.
    """
    out = {"loopback": [], "input": [], "errors": []}
    _list_loopback(out)
    _list_input(out)
    return out


def describe_device(kind, device_id):
    """A human label for a stored device id, or a note that it is gone."""
    entries = list_devices().get(kind) or []
    wanted = "" if device_id is None else str(device_id).strip()
    if wanted == "":
        for entry in entries:
            if entry["default"]:
                return "{0} (system default)".format(entry["name"])
        return "(system default)"
    for entry in entries:
        if entry["id"] == wanted:
            return entry["name"]
    if kind == "input":
        for entry in entries:
            if entry["name"] == wanted:
                return entry["name"]
    return "{0} (not found)".format(wanted)


def print_devices():
    """
    The --list-devices listing. Plain ASCII on purpose: this prints to a
    Windows console that may still be on cp437, where a box-drawing character
    or an arrow raises UnicodeEncodeError instead of printing the device list.
    """
    listing = list_devices()

    print("Loopback devices (--capture loopback) - capture what an OUTPUT")
    print("device is PLAYING. Pick the one you listen on.")
    if not listing["loopback"]:
        print("  (none)")
    for entry in listing["loopback"]:
        print("  {0}{1}".format(entry["name"],
                                "  <- Windows default" if entry["default"]
                                else ""))
        print("      --device {0}".format(entry["id"]))

    print("")
    print("Recording devices (--capture input) - Stereo Mix, microphones,")
    print("Line In.")
    if not listing["input"]:
        print("  (none)")
    for entry in listing["input"]:
        print("  {0:>3}  {1}{2}".format(
            entry["id"], entry["name"],
            "  <- Windows default" if entry["default"] else ""))
        print("       {0}, {1} ch, {2} Hz".format(
            entry["hostapi"] or "?", entry["channels"], entry["samplerate"]))

    if listing["errors"]:
        print("")
        print("Notes:")
        for line in listing["errors"]:
            print("  {0}".format(line))
    print("")
    print("Leave --device blank for the Windows default.")


# -------------------------------
# Sources
# -------------------------------
class AudioSource(object):
    """
    One capture path.

        name     what to show the user
        info     whatever is worth putting on screen about it
        start()  begin capturing; spawns a daemon thread if it needs one
        stop()   idempotent, bounded, never hangs
        alive()  is it still producing audio

    on_audio(np.float32 mono 16 kHz 1-D) is called from the capture thread.
    on_log(level, message), level in "info" / "warn" / "error", is how a
    running source reports trouble - it never raises out of its own thread.
    """

    kind = "none"

    def __init__(self, on_audio, on_log=None):
        self.name = self.kind
        self.info = {"kind": self.kind}
        self._on_audio = on_audio
        self._on_log = on_log
        self._stop = threading.Event()
        self._thread = None

    # -- plumbing ---------------------------------------------------------
    def log(self, level, message):
        if self._on_log is not None:
            self._on_log(level, message)
        else:
            print("[audio] {0}".format(message))

    def emit(self, data, src_rate):
        """Hand one block on, as mono 16 kHz float32."""
        audio = to_mono_16k(data, src_rate)
        if len(audio):
            self._on_audio(audio)

    def _run(self):
        raise NotImplementedError

    def _guarded_run(self):
        try:
            self._run()
        except Exception as e:
            # Nothing is waiting on this thread's return value, so an escaping
            # exception would end capture with the app still running and no
            # message anywhere - the exact failure that is hardest to explain.
            self.log("error", "{0} capture stopped: {1}".format(self.kind, e))
        finally:
            self._stop.set()

    # -- lifecycle --------------------------------------------------------
    def start(self):
        if self._thread is not None or self._stop.is_set():
            return
        self._thread = threading.Thread(
            target=self._guarded_run, name=self.kind + "-capture", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        self._thread = None
        # A source that stops itself at the end of its own _run() (FileSource
        # does) would deadlock joining the thread it is running on.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=STOP_TIMEOUT)

    def alive(self):
        thread = self._thread
        return thread is not None and thread.is_alive()


class LoopbackSource(AudioSource):
    """WASAPI loopback of an OUTPUT device. No Stereo Mix required."""

    kind = "loopback"

    def __init__(self, settings, on_audio, on_log=None):
        AudioSource.__init__(self, on_audio, on_log)
        try:
            import soundcard as sc
        except ImportError:
            raise AudioSourceError(
                "Loopback capture needs the 'soundcard' package: pip install "
                "soundcard (or set capture to 'input' to use Stereo Mix).")
        # soundcard emits this warning per dropped block. WASAPI loopback
        # produces them routinely while nothing is playing, and left alone
        # they scroll the transcript off the console.
        warnings.filterwarnings("ignore", message=".*data discontinuity.*")

        with _com_thread():
            spk = self._pick(sc, str(settings.get("device") or "").strip())
            try:
                self._mic = sc.get_microphone(str(spk.id),
                                              include_loopback=True)
            except Exception as e:
                raise AudioSourceError(
                    "Could not open loopback of '{0}': {1}".format(
                        spk.name, e))
            self.name = "WASAPI loopback: {0}".format(spk.name)
            self.info = {"kind": "loopback", "device": str(spk.id),
                         "deviceName": spk.name, "samplerate": SAMPLERATE,
                         "channels": 2}

    def _pick(self, sc, wanted):
        """The output device to capture. Falls back to the system default."""
        try:
            speakers = sc.all_speakers()
        except Exception as e:
            raise AudioSourceError(
                "Could not list output devices: {0}".format(e))
        if not speakers:
            raise AudioSourceError("No output device to capture.")

        default_spk = None
        try:
            default_spk = sc.default_speaker()
        except Exception as e:
            self.log("warn", "no default output device ({0}) - using the "
                             "first one listed.".format(e))

        if wanted:
            for cand in speakers:
                if str(cand.id) == wanted:
                    return cand
            # Endpoint ids die when a headset is unplugged or a dock is
            # detached. Falling back keeps the session alive; raising here
            # would end a recording because someone moved a cable.
            self.log("warn", "output device '{0}' is gone - falling back to "
                             "the Windows default.".format(wanted))
        return default_spk or speakers[0]

    def _run(self):
        # Prefer capturing straight at 16 kHz - WASAPI converts in the mixer,
        # which is better than np.interp and costs nothing here. Fall back to
        # 48 kHz plus a local resample only if the driver refuses.
        with _com_thread():
            for rec_sr in (SAMPLERATE, 48000):
                try:
                    recorder = self._mic.recorder(samplerate=rec_sr,
                                                  channels=2)
                    with recorder as rec:
                        if rec_sr != SAMPLERATE:
                            self.log("info", "loopback running at {0} Hz "
                                             "(resampling to {1} Hz)".format(
                                                 rec_sr, SAMPLERATE))
                        self.info["samplerate"] = rec_sr
                        frames = int(rec_sr * BLOCK_SEC)   # ~128 ms blocks
                        while not self._stop.is_set():
                            self.emit(rec.record(numframes=frames), rec_sr)
                    return
                except Exception as e:
                    if self._stop.is_set():
                        return  # closing the recorder mid-record, not a fault
                    self.log("warn", "loopback at {0} Hz failed: {1}".format(
                        rec_sr, e))
        self.log("error", "Loopback capture failed entirely - try capture "
                          "'input' (Stereo Mix), or pick another output "
                          "device.")


class InputSource(AudioSource):
    """A recording device (Stereo Mix, microphone, Line In) via sounddevice."""

    kind = "input"

    def __init__(self, settings, on_audio, on_log=None):
        AudioSource.__init__(self, on_audio, on_log)
        try:
            import sounddevice as sd
        except ImportError:
            raise AudioSourceError(
                "Recording-device capture needs the 'sounddevice' package: "
                "pip install sounddevice.")
        self._sd = sd
        self._stream = None
        self._reported_drop = False

        index = self._resolve(sd, settings.get("device"))
        try:
            info = sd.query_devices(index, "input")
        except Exception as e:
            raise AudioSourceError(
                "Could not open recording device {0}: {1}".format(index, e))

        # Open at the device's NATIVE rate and channel count. Many WDM/MME
        # devices reject a forced 16 kHz outright - PortAudio reports
        # PaErrorCode -9997, "invalid sample rate" - so the resample to 16 kHz
        # has to happen on this side of the driver, not inside it.
        self._index = index
        self._rate = int(info["default_samplerate"])
        self._channels = max(1, min(2, int(info["max_input_channels"])))
        self._blocksize = int(self._rate * BLOCK_SEC)     # ~128 ms blocks
        self.name = "Recording device: {0}".format(info["name"])
        self.info = {"kind": "input", "device": str(index),
                     "deviceName": info["name"], "samplerate": self._rate,
                     "channels": self._channels}

    def _resolve(self, sd, wanted):
        """A usable device index. Falls back to the default with a warning."""
        try:
            devices = sd.query_devices()
        except Exception as e:
            raise AudioSourceError(
                "Could not list recording devices: {0}".format(e))

        wanted = str(wanted or "").strip()
        if wanted:
            index = None
            try:
                index = int(wanted)
            except ValueError:
                # Indices are reassigned when a device is added or removed,
                # so a saved preset's name outlives its number.
                for i, dev in enumerate(devices):
                    if dev["name"] == wanted \
                            and int(dev["max_input_channels"]) > 0:
                        index = i
                        break
            # Explicit range check: devices[-1] is a legal index that would
            # silently open the last device in the list instead of the one
            # asked for, and an output-only device fails later inside
            # PortAudio with a message that names neither.
            if index is not None and 0 <= index < len(devices) \
                    and int(devices[index]["max_input_channels"]) > 0:
                return index
            self.log("warn", "recording device '{0}' is gone or has no input "
                             "channels - falling back to the Windows "
                             "default.".format(wanted))

        try:
            return int(sd.query_devices(kind="input")["index"])
        except Exception as e:
            raise AudioSourceError(
                "No recording device is available: {0}".format(e))

    def start(self):
        # PortAudio runs the callback on its own high-priority thread, so
        # there is nothing here for a thread of ours to do.
        if self._stream is not None or self._stop.is_set():
            return

        def callback(indata, frames, time_info, status):
            # PortAudio treats any exception out of the callback as "abort the
            # stream", so one bad block would end capture for good. Report the
            # first and keep going.
            try:
                self.emit(indata, self._rate)
            except Exception as e:
                if not self._reported_drop:
                    self._reported_drop = True
                    self.log("error", "input block dropped: {0}".format(e))

        try:
            self._stream = self._sd.InputStream(
                channels=self._channels, samplerate=self._rate,
                device=self._index, blocksize=self._blocksize,
                callback=callback)
            self._stream.start()
        except Exception as e:
            self._stream = None
            self._stop.set()
            self.log("error", "could not start '{0}': {1}".format(
                self.name, e))

    def stop(self):
        self._stop.set()
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception as e:
                # A device removed while streaming throws here. close() has
                # to run anyway: skipping it leaves PortAudio holding the
                # stream, and its callback, for the life of the process.
                self.log("warn", "stopping '{0}': {1}".format(self.name, e))
            try:
                stream.close()
            except Exception as e:
                self.log("warn", "closing '{0}': {1}".format(self.name, e))

    def alive(self):
        stream = self._stream
        try:
            return stream is not None and bool(stream.active)
        except Exception:
            return False


class BrowserSource(AudioSource):
    """
    Audio pushed in from the page: the browser's microphone, or a tab it is
    sharing. There is no device and no thread - the web layer calls feed()
    from whichever thread its socket runs on.
    """

    kind = "browser"

    def __init__(self, settings, on_audio, on_log=None):
        AudioSource.__init__(self, on_audio, on_log)
        self.name = "Browser (microphone or shared tab)"
        self.info = {"kind": "browser", "device": "", "deviceName": "browser",
                     "samplerate": SAMPLERATE, "channels": 1}
        self._tail = b""
        self._lock = threading.Lock()
        self._running = False

    def start(self):
        self._stop.clear()
        self._running = True

    def stop(self):
        self._stop.set()
        self._running = False
        with self._lock:
            self._tail = b""

    def alive(self):
        return self._running

    def feed(self, data):
        """
        One message from the page: bytes of little-endian PCM16 at 16 kHz
        mono, or an already-decoded float32 ndarray.
        """
        if self._stop.is_set():
            return
        if isinstance(data, np.ndarray):
            self.emit(data, SAMPLERATE)
            return

        with self._lock:
            raw = self._tail + bytes(data)
            # A WebSocket frame boundary can land BETWEEN the two bytes of a
            # PCM16 sample, so an odd byte count is normal rather than a bug.
            # Dropping the orphan byte would pair the low half of every
            # following sample with the high half of the next one, shifting
            # the whole stream by one byte and turning the rest of the session
            # into noise. Hold it for the next message instead.
            usable = len(raw) - (len(raw) % 2)
            block, self._tail = raw[:usable], raw[usable:]
        if not block:
            return
        samples = np.frombuffer(block, dtype="<i2").astype(np.float32)
        self.emit(samples / 32768.0, SAMPLERATE)


class FileSource(AudioSource):
    """A 16-bit PCM WAV from disk, fed as if it were a live device."""

    kind = "file"

    def __init__(self, settings, on_audio, on_log=None):
        AudioSource.__init__(self, on_audio, on_log)
        path = str(settings.get("file_path") or "").strip()
        if not path:
            raise AudioSourceError(
                "Capture is set to 'file' but no WAV file was given.")
        try:
            wav = wave.open(path, "rb")
        except (OSError, EOFError, wave.Error) as e:
            raise AudioSourceError(
                "Could not read '{0}': {1}. Only uncompressed 16-bit PCM WAV "
                "is supported; convert it with {2}".format(
                    path, e, _FFMPEG_HINT.format(path)))

        width, comp = wav.getsampwidth(), wav.getcomptype()
        if wav.getframerate() <= 0:
            # A damaged header reaches every sample-rate division in here.
            # Rejected at build time so it degrades the same way a wrong
            # codec does, instead of as "division by zero" out of the
            # capture thread once playback has apparently started.
            wav.close()
            raise AudioSourceError(
                "'{0}' declares a sample rate of 0 - its header is damaged. "
                "Rebuild it with: {1}".format(path, _FFMPEG_HINT.format(path)))
        if width != 2 or comp != "NONE":
            wav.close()
            raise AudioSourceError(
                "'{0}' is {1}-bit {2} audio; this reads uncompressed 16-bit "
                "PCM WAV only. Convert it first: {3}".format(
                    path, width * 8, "PCM" if comp == "NONE" else comp,
                    _FFMPEG_HINT.format(path)))

        self._path = path
        self._wav = wav
        self._rate = wav.getframerate()
        self._channels = max(1, wav.getnchannels())
        self._realtime = bool(settings.get("file_realtime", True))
        self._block = max(1, int(self._rate * BLOCK_SEC))   # ~128 ms blocks
        duration = wav.getnframes() / float(self._rate)
        self.name = "WAV file: {0}".format(os.path.basename(path))
        self.info = {"kind": "file", "device": path,
                     "deviceName": os.path.basename(path),
                     "samplerate": self._rate, "channels": self._channels,
                     "duration": round(duration, 2),
                     "realtime": self._realtime}

    def stop(self):
        AudioSource.stop(self)
        # Built-but-never-started is the case that matters: _run()'s finally
        # never got to close the file, and Windows then refuses to delete,
        # move or overwrite that WAV for the life of the process - measured as
        # WinError 32 on a source the pipeline built and threw away.
        try:
            self._wav.close()
        except Exception:
            pass

    def _run(self):
        started = time.monotonic()
        fed = 0
        ended = False
        try:
            while not self._stop.is_set():
                raw = self._wav.readframes(self._block)
                if not raw:
                    ended = True
                    break
                # A WAV still being written, or truncated in transit, ends
                # mid-frame. Reshaping that raises "cannot reshape array of
                # size 3999 into shape (2)" out of the capture thread and
                # loses the whole file; keep the complete frames and stop.
                usable = len(raw) - (len(raw) % (2 * self._channels))
                if not usable:
                    ended = True
                    break
                block = np.frombuffer(raw[:usable], dtype="<i2")
                block = block.astype(np.float32) / 32768.0
                if self._channels > 1:
                    block = block.reshape(-1, self._channels)
                self.emit(block, self._rate)
                fed += len(block)
                if self._realtime:
                    # Hold 1x pace so the buffer fills, the gate fires and the
                    # captions appear exactly as they do on a live device.
                    # Off, the file arrives faster than inference can run and
                    # the pipeline's catch-up logic starts dropping audio -
                    # which is the point for bulk transcription and useless
                    # for watching captions appear.
                    ahead = fed / float(self._rate) \
                        - (time.monotonic() - started)
                    if ahead > 0 and self._stop.wait(ahead):
                        break
        finally:
            try:
                self._wav.close()
            except Exception:
                pass
        # Only when the file actually ran out. Saying "end of file" for a
        # source the user switched away from puts a claim in the log that the
        # rest of the transcript then has to be read against.
        if ended:
            seconds = fed / float(self._rate)
            self.log("info", "{0}: end of file after {1:.1f}s of audio "
                             "({2} frames at {3} Hz).".format(
                                 os.path.basename(self._path), seconds, fed,
                                 self._rate))
        self._stop.set()


_SOURCES = {
    "loopback": LoopbackSource,
    "input": InputSource,
    "browser": BrowserSource,
    "file": FileSource,
}


def create_source(settings, on_audio, on_log=None):
    """
    The AudioSource settings["capture"] asks for, not yet started.

    Raises AudioSourceError when it cannot be built at all, so the caller can
    say why in one line and offer another mode. Once start() has been called,
    every further problem arrives through on_log instead.
    """
    capture = str(settings.get("capture") or "loopback")
    cls = _SOURCES.get(capture)
    if cls is None:
        raise AudioSourceError(
            "Unknown capture mode '{0}' - expected one of {1}.".format(
                capture, ", ".join(sorted(_SOURCES))))
    return cls(settings, on_audio, on_log)
