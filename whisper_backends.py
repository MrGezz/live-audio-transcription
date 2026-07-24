"""
Transcription backends for live-audio-transcription.

Two interchangeable backends behind one interface:

  ServerBackend  -> whisper.cpp `whisper-server` over HTTP (Vulkan GPU:
                    works on AMD cards such as the Radeon Pro W5500,
                    where CUDA/ROCm are unavailable).
  LocalBackend   -> faster-whisper on CPU (int8). Fallback / no server.

Usage:
    from whisper_backends import create_backend
    backend = create_backend("auto", server_url="http://127.0.0.1:8080",
                             model_path=r"_models\faster-whisper-medium")
    results = backend.transcribe(float32_mono_16k_buffer, translate=False)
    for text, lang in results:
        ...
"""

import io
import struct
import wave

import numpy as np

SAMPLERATE = 16000


class BackendError(RuntimeError):
    """Raised when a backend cannot be created or reached."""


# -------------------------------
# whisper.cpp server backend (GPU via Vulkan)
# -------------------------------
class ServerBackend(object):
    name = "whisper.cpp server (Vulkan GPU)"

    def __init__(self, url="http://127.0.0.1:8080", timeout=30):
        try:
            import requests  # noqa: F401 - validated here, used per-call
        except ImportError:
            raise BackendError(
                "The 'requests' package is required for the server backend. "
                "Install it with: pip install requests"
            )
        self._requests = __import__("requests")
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._ping()

    def _ping(self):
        try:
            # Root returns the server index page; any HTTP response means alive.
            self._requests.get(self.url + "/", timeout=3)
        except Exception as e:
            raise BackendError(
                "Cannot reach whisper-server at {0} ({1}). "
                "Start it first (see start_whisper_server.bat / SETUP_AMD.md).".format(
                    self.url, e
                )
            )

    @staticmethod
    def _to_wav_bytes(buffer):
        """float32 [-1, 1] mono 16 kHz -> in-memory 16-bit PCM WAV."""
        pcm = np.clip(buffer, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype(np.int16)
        bio = io.BytesIO()
        wf = wave.open(bio, "wb")
        try:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLERATE)
            wf.writeframes(pcm.tobytes())
        finally:
            wf.close()
        return bio.getvalue()

    def transcribe(self, buffer, translate=False):
        """Returns list of (text, lang_code) tuples for the given buffer."""
        wav_bytes = self._to_wav_bytes(buffer)
        data = {
            "temperature": "0.0",
            "temperature_inc": "0.2",
            "response_format": "verbose_json",
            "language": "auto",
            "translate": "true" if translate else "false",
        }
        files = {"file": ("buffer.wav", wav_bytes, "audio/wav")}
        try:
            resp = self._requests.post(
                self.url + "/inference",
                data=data,
                files=files,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except Exception as e:
            raise BackendError("whisper-server request failed: {0}".format(e))

        payload = resp.json()
        lang = payload.get("language") or "??"
        results = []
        segments = payload.get("segments")
        if segments:
            for seg in segments:
                text = (seg.get("text") or "").strip()
                if text:
                    results.append((text, lang))
        else:
            text = (payload.get("text") or "").strip()
            if text:
                results.append((text, lang))
        return results


# -------------------------------
# faster-whisper CPU fallback
# -------------------------------
class LocalBackend(object):
    name = "faster-whisper (CPU int8)"

    def __init__(self, model_path):
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise BackendError(
                "faster-whisper is not installed. "
                "Install it with: pip install faster-whisper"
            )
        try:
            self.model = WhisperModel(model_path, device="cpu", compute_type="int8")
        except Exception as e:
            raise BackendError(
                "Could not load faster-whisper model at '{0}': {1}".format(
                    model_path, e
                )
            )

    def transcribe(self, buffer, translate=False):
        segments, info = self.model.transcribe(
            buffer,
            language=None,
            task="translate" if translate else "transcribe",
            word_timestamps=False,
            beam_size=1,
        )
        lang = getattr(info, "language", None) or "??"
        results = []
        for segment in segments:
            text = segment.text.strip()
            if text:
                results.append((text, lang))
        return results


# -------------------------------
# Factory
# -------------------------------
def create_backend(backend="auto", server_url="http://127.0.0.1:8080",
                   model_path=r"_models\faster-whisper-medium"):
    """
    backend: "auto" | "server" | "local"
      auto   -> try whisper-server first, fall back to CPU faster-whisper
      server -> whisper-server only; fail loudly if unreachable
      local  -> faster-whisper CPU only
    """
    if backend == "server":
        b = ServerBackend(server_url)
        print("Backend: {0} @ {1}".format(b.name, server_url))
        return b
    if backend == "local":
        b = LocalBackend(model_path)
        print("Backend: {0}".format(b.name))
        return b
    if backend == "auto":
        try:
            b = ServerBackend(server_url)
            print("Backend: {0} @ {1}".format(b.name, server_url))
            return b
        except BackendError as e:
            print("[warn] {0}".format(e))
            print("[warn] Falling back to CPU faster-whisper (slower).")
            b = LocalBackend(model_path)
            print("Backend: {0}".format(b.name))
            return b
    raise BackendError("Unknown backend '{0}' (use auto|server|local)".format(backend))
