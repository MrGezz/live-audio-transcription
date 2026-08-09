"""
Transcription backends for live-audio-transcription.

Interchangeable backends behind one transcribe(buffer, translate) interface:

  ServerBackend  -> whisper.cpp `whisper-server` over HTTP (GPU; the server
                    can be a Vulkan build for AMD/Intel or a CUDA build for
                    NVIDIA - this layer does not care which).
  LocalBackend   -> faster-whisper on CPU (int8). Fallback / no server.
  AutoBackend    -> both of the above: runs on the server, drops to CPU if it
                    dies mid-session, and returns to the server when it comes
                    back. What "--backend auto" builds.

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
import time
import wave

import numpy as np

SAMPLERATE = 16000


class BackendError(RuntimeError):
    """Raised when a backend cannot be created or reached."""


# -------------------------------
# whisper.cpp server backend (GPU: Vulkan or CUDA build)
# -------------------------------
class ServerBackend(object):
    name = "whisper.cpp server (GPU)"
    active_name = "server"      # see AutoBackend.active_name

    def __init__(self, url="http://127.0.0.1:8080", timeout=30, check=True):
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
        # check=False builds a handle to a server that is NOT up yet, so
        # AutoBackend can keep polling it after a fallback. Without it the
        # only way to get a ServerBackend is to already have a live server.
        if check:
            self.ping()

    def ping(self, timeout=3):
        """Cheap liveness check. Raises BackendError if the server is not up."""
        try:
            # Root returns the server index page; any HTTP response means alive.
            self._requests.get(self.url + "/", timeout=timeout)
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
    active_name = "local"       # see AutoBackend.active_name

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
# auto: server-preferred, CPU fallback, self-healing
# -------------------------------
class AutoBackend(object):
    """
    Prefers the GPU server and survives it dying mid-session.

    A ServerBackend on its own checks reachability exactly once, in __init__.
    If the server goes away later, every pass fails - roughly every --slide
    seconds, forever - and there is no path back to GPU without restarting the
    script. This wrapper keeps both backends behind one transcribe() and moves
    between them:

      GPU -> CPU   after FAILURES_BEFORE_FALLBACK consecutive failures (~10 s
                   at the default 2 s slide). Deliberately not on the first
                   one: a single timeout is a hiccup, and paying for it with
                   CPU inference for the rest of the session is a bad trade.
      CPU -> GPU   as soon as a cheap probe, tried every PROBE_INTERVAL_SEC,
                   gets an answer.

    Exactly one line is printed per transition, so a flapping server cannot
    fill the console.

    Only "--backend auto" builds this. Explicit "server" / "local" get the bare
    backend - those are choices to respect, not defaults to second-guess.
    """

    name = "auto (whisper.cpp server + CPU fallback)"

    FAILURES_BEFORE_FALLBACK = 5
    PROBE_INTERVAL_SEC = 60.0
    PROBE_TIMEOUT_SEC = 3.0

    def __init__(self, server_url, model_path):
        self.server_url = server_url
        self._model_path = model_path
        self._local = None          # built on first CPU pass, see _cpu()
        self._local_error = None    # sticky: set if the CPU model won't load
        self._failures = 0
        self._last_probe = time.monotonic()

        try:
            self._server = ServerBackend(server_url)
            self._on_server = True
            print("Backend: {0} @ {1}".format(self._server.name, server_url))
            # _local stays None here: a session that never loses its server
            # never pays the faster-whisper load time or the RAM.
        except BackendError as e:
            self._on_server = False
            print("[warn] {0}".format(e))
            print("[warn] Falling back to CPU faster-whisper (slower); will retry "
                  "the server every {0:.0f}s.".format(self.PROBE_INTERVAL_SEC))
            try:
                self._server = ServerBackend(server_url, check=False)
            except BackendError:
                self._server = None   # no 'requests' - no server path at all
            # The server is already known-down, so CPU is needed immediately.
            # Loading it now keeps "model missing" a startup failure, the way
            # it has always been, rather than a surprise several minutes in.
            self._cpu()

    @property
    def active_name(self):
        """
        Which backend is serving RIGHT NOW ("server" or "local").

        Anything that phrases advice around the hardware has to ask, rather
        than assume: with this class in play the answer changes mid-session,
        so a hardcoded "the GPU is slow" hint can end up printed while running
        on CPU with no server at all.
        """
        return "server" if self._on_server else "local"

    def _cpu(self):
        """The faster-whisper backend, constructed on first actual use."""
        if self._local is not None:
            return self._local
        if self._local_error is not None:
            raise self._local_error
        try:
            self._local = LocalBackend(self._model_path)
        except BackendError as e:
            # Remember the failure: retrying a missing or broken model would
            # stall the worker for seconds, on every pass, forever.
            self._local_error = e
            raise
        print("Backend: {0}".format(self._local.name))
        return self._local

    def _fall_back(self):
        """GPU -> CPU. False if there is no usable CPU backend to move to."""
        try:
            self._cpu()
        except BackendError as e:
            # Reached at most once: _local_error is now set, and transcribe()
            # stops calling us as soon as it is.
            print("[backend] whisper-server has failed {0}x in a row and the CPU "
                  "fallback is unavailable ({1}) - staying on the server."
                  .format(self._failures, e))
            return False
        self._on_server = False
        self._last_probe = time.monotonic()   # first probe one interval from now
        # No exception text here on purpose: the caller has already printed the
        # first few failures verbatim, and a requests connection error is ~400
        # characters of HTTPConnectionPool noise. This line reports the decision.
        print("[backend] whisper-server failed {0}x in a row - switched to CPU "
              "faster-whisper (slower). Retrying the server every {1:.0f}s."
              .format(self._failures, self.PROBE_INTERVAL_SEC))
        return True

    def _probe(self):
        """'Are you back yet?' - only ever called while running on CPU."""
        if self._server is None:
            return
        now = time.monotonic()
        if now - self._last_probe < self.PROBE_INTERVAL_SEC:
            return
        self._last_probe = now
        try:
            # This runs on the caller's thread, so a miss costs
            # PROBE_TIMEOUT_SEC of stalled captions once per interval - the
            # reason the probe timeout is short and the interval is long.
            self._server.ping(timeout=self.PROBE_TIMEOUT_SEC)
        except BackendError:
            return
        self._on_server = True
        self._failures = 0
        print("[backend] whisper-server is back - switched to GPU.")

    def transcribe(self, buffer, translate=False):
        if not self._on_server:
            self._probe()
        if self._on_server:
            try:
                results = self._server.transcribe(buffer, translate=translate)
                self._failures = 0
                return results
            except Exception:
                self._failures += 1
                # Too early to judge, or nothing to fall back to (_local_error
                # is sticky, so this never re-enters _cpu() once it has failed
                # - re-raising one stored exception every 2s would grow its
                # traceback for the rest of the session).
                if (self._failures < self.FAILURES_BEFORE_FALLBACK
                        or self._local_error is not None):
                    raise          # caller logs it, throttled
                if not self._fall_back():
                    raise          # nothing better to switch to
                # Serve this buffer from CPU rather than dropping it.
        return self._cpu().transcribe(buffer, translate=translate)


# -------------------------------
# Factory
# -------------------------------
def create_backend(backend="auto", server_url="http://127.0.0.1:8080",
                   model_path=r"_models\faster-whisper-medium"):
    """
    backend: "auto" | "server" | "local"
      auto   -> AutoBackend: whisper-server, with CPU fallback and recovery
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
        return AutoBackend(server_url, model_path)
    raise BackendError("Unknown backend '{0}' (use auto|server|local)".format(backend))
