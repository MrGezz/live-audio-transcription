"""
pipeline.py - the transcription engine, with nothing attached to it.

Why this exists
---------------
All of this used to live in live_transcription.py as module-level code: parse
argv, open the device, start a thread, and inside that thread a loop that also
owned the buffering policy, the speech gate, the duplicate filter, the file
writing and the print statements. That works exactly once, for one caller, with
every decision fixed at import time.

Two things broke it. A web UI cannot answer an input() prompt, and it needs to
change settings while the loop is running - which module-level constants cannot
express. And the CLI should not become the second copy of the same loop.

So the loop moved here, behind an object that:

  * owns the backend, the speech gate, the buffering strategy, the audio source
    and the transcript writer, and rebuilds only the ones a change touches
    (settings.rebuilds_for decides which)
  * reports through emit(kind, data) instead of print(), so the console front
    end and the browser front end are both just subscribers
  * accepts a settings patch from any thread at any time and applies it on the
    worker thread at a safe point, so nothing is ever reconfigured mid-inference

What it deliberately does NOT own: the Tk overlay. Tk has to be created and
pumped on the main thread, and a component that can be rebuilt from a worker
thread cannot also be that. The front end subscribes to transcript events and
drives the overlay itself.
"""

import collections
import queue
import threading
import time

import numpy as np

import audio_sources
import safe_paths
import settings as settings_mod
from buffering import create_strategy
from speech_gate import SpeechGate
from transcript import TranscriptWriter
from whisper_backends import (DECODE_OPTIONS, BackendError,
                              create_backend, looks_untranslated)

SAMPLERATE = 16000


def _new_stats():
    return {"windows": 0, "captions": 0, "dropped": 0, "skipped": 0,
            "infer_total": 0.0, "audio_total": 0.0}


# How often the level meter is pushed out. Fast enough that the bar tracks
# speech, slow enough that a websocket is not carrying 125 messages a second
# for a number nobody reads that precisely.
METER_INTERVAL_SEC = 0.12

# Throttles for the advisory console lines, carried over from the original
# worker. Same intervals: these are hints, and a hint repeated every two
# seconds stops being one.
SKIP_REPORT_SEC = 10.0
PERF_REPORT_SEC = 15.0
LAG_REPORT_SEC = 15.0

HISTORY_LIMIT = 2000


class Pipeline(object):
    """
    Capture -> gate -> chunk -> transcribe -> emit.

    Events (kind, payload):
      log        {level, msg}                  human-facing lines
      state      {running, backend, source, ...} whenever something changes
      settings   the full settings dict         after any accepted patch
      meter      {level, speech_frames, ...}    ~8/s while running
      transcript {id, text, words, ...}         one per accepted caption
      dropped    {text, reason}                 a caption the dedup filter ate
      perf       {infer_s, xrt, behind, ...}    per transcribed window
      benchmark  {status, rows, recommend}      while a benchmark runs
      error      {msg}
    """

    def __init__(self, settings, emit=None):
        self.settings = dict(settings_mod.DEFAULTS)
        self.settings.update(settings or {})
        self._emit_raw = emit or (lambda kind, data: None)

        self._lock = threading.RLock()
        self._audio = queue.Queue()
        self._stop = threading.Event()
        self._worker = None

        self.backend = None
        self.gate = None
        self.strategy = None
        self.source = None
        self.writer = None

        self._pending = {}          # settings patch waiting for the worker
        self._rebuild = set()        # components the patch invalidated
        self._paused = threading.Event()

        # Keys whose CURRENT value arrived over an untrusted channel. The
        # smallest thing that can answer the only question safe_paths asks -
        # who chose this path - and it has to be tracked rather than inferred,
        # because by the time _build_source reads settings["file_path"] the
        # string looks identical whether the CLI or a browser put it there.
        self._untrusted = set()

        self._history = collections.deque(maxlen=HISTORY_LIMIT)
        self._next_id = 1
        self._started_at = None
        self._last_error = ""
        self._consecutive_errors = 0
        self._last_meter = 0.0
        self._last_skip_report = 0.0
        self._last_perf_report = 0.0
        self._last_lag_report = 0.0
        self._live_level = 0.0
        self._last_gate = None
        self._drained = False
        self._recent_languages = collections.Counter()
        self._translate_warned = False
        self._stats = _new_stats()

    # -- plumbing ---------------------------------------------------------
    def emit(self, kind, data):
        try:
            self._emit_raw(kind, data)
        except Exception:
            # A front end that throws must not take the worker with it. There
            # is deliberately nowhere to report this: the reporting channel is
            # the thing that just failed.
            pass

    def log(self, level, msg):
        self.emit("log", {"level": level, "msg": msg})

    # -- lifecycle --------------------------------------------------------
    def start(self):
        """Build everything and start capturing. Raises BackendError only."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._started_at = time.monotonic()
            self._build_backend()
            self._build_gate()
            self._build_strategy()
            # A new session starts from nothing. _build_strategy reuses the
            # object, and everything it holds is about the session that just
            # ended: audio buffered at the moment of the Stop, and (sliding
            # window) the text of the last window transcribed. Measured on a
            # Stop after 3 s of held audio, a Start put those 3 s at the front
            # of the first new window - so the first window of the new session
            # straddled the gap - and matched the first new caption against a
            # window it is not adjacent to.
            #
            # Here rather than in _build_strategy, which _drain_pending also
            # calls: there the strategy is being retuned mid-session and
            # keeping the remembered text is the point, since the previous
            # window is still adjacent to the next one. (configure() drops the
            # buffered audio itself, but only when the window geometry moved -
            # see SlidingWindow.configure. It never drops the text.)
            # "Rebuild to match the settings" and "this is a new session" are
            # different questions and only start() can answer the second.
            #
            # Nothing feeds the strategy between here and _build_source below,
            # and the early return above means a Start against a running
            # session never reaches this line.
            self.strategy.reset()
            self._build_writer()
            self._worker = threading.Thread(target=self._run,
                                            name="transcriber", daemon=True)
            self._worker.start()
            self._build_source()
        self._push_state()

    def stop(self):
        self._stop.set()
        source, worker, writer = self.source, self._worker, self.writer
        self.source = None
        if source is not None:
            try:
                source.stop()
            except Exception as e:
                self.log("warn", "Capture did not stop cleanly: {0}".format(e))
        if worker is not None:
            worker.join(timeout=6)
        if writer is not None:
            writer.close()
            self.writer = None
        self._worker = None
        self._push_state()

    def restart(self):
        """
        Tear everything down and build it again from the current settings.

        This exists because a rebuild can fail. Change the capture device to
        one that has been unplugged, point the model at a file that is not
        there, or switch to the server backend before the server is up, and
        _build_source / _build_backend log the failure and leave that
        attribute None. The worker thread survives - deliberately, it is the
        only thing draining the audio queue - but nothing in it will ever
        retry, so the session sits there alive and deaf. Until now the only
        cure was closing the program and starting it again, which from a
        browser panel means walking over to the machine it runs on.

        The transcript survives, because words said before a device was
        swapped are still part of the session. The performance stats do not:
        averaged across a GPU run and the CPU run that replaced it, xRT
        describes neither.

        Raises whatever start() raises. Slow - the CPU backend takes seconds
        to load a model - so callers on an event loop must hand it to a
        thread.
        """
        self.log("info", "Restarting: stopping capture and rebuilding.")
        self.stop()
        with self._lock:
            self._stats = _new_stats()
            self._consecutive_errors = 0
            self._last_error = ""
            self._drained = False
            self._last_gate = None
        self.start()
        self.log("info", "Restarted. {0}".format(
            "Capturing: {0}".format(self.source.name) if self.source is not None
            else "Capture did NOT come back - see the errors above."))

    def alive(self):
        return self._worker is not None and self._worker.is_alive()

    def pause(self, paused):
        """Keep capturing, stop transcribing. Used while a benchmark runs."""
        if paused:
            self._paused.set()
        else:
            self._paused.clear()
        self._push_state()

    # -- configuration ----------------------------------------------------
    def _audio_roots(self):
        """
        Folders an untrusted caller may read audio from, or None for anywhere.

        This is the one judgement call in the read-path rules, so it is made
        here where the listener's own settings are, rather than inside
        safe_paths where it would be a policy with no facts behind it.

        Confining reads to a folder is the only one of those rules that costs a
        real capability: it stops the owner of the machine pointing the browser
        panel at a WAV in their music folder, which is a working feature and
        the common case, because the listener binds 127.0.0.1 and the browser
        on the other end of it is usually them. So it is charged only once
        there is genuinely somebody else who could be at the other end - a
        listener bound anywhere but loopback. The rules that cost nothing (no
        UNC, no device names, no data streams, .wav only) apply either way.
        """
        if not self.settings.get("web"):
            return None
        host = str(self.settings.get("web_host") or "").strip().lower()
        if host in ("", "127.0.0.1", "::1", "localhost"):
            return None
        return safe_paths.audio_roots()

    def apply(self, patch, remote=False):
        """
        Validate a settings patch and queue it for the worker.

        Returns (applied, errors). Applying happens on the worker thread at the
        top of its next iteration, so a rebuild can never land in the middle of
        an inference. That also means a patch sent while a 3-second transcribe
        is in flight takes effect up to 3 seconds later, which is the correct
        trade and worth knowing.

        `remote=True` refuses the settings.REMOTE_LOCKED fields - the listener
        itself, whether a window opens on the host machine, and the three paths
        that are each the only untrusted route to a sink of their kind (`model`
        to an outbound model fetch, `vad_model` to the native ONNX parser,
        `output` to the only makedirs+open in the tree).

        The keys are popped HERE, before validate() below, and that ordering is
        load-bearing rather than tidy: validate() itself touches the filesystem
        (the capture="file" rule stats file_path), so a check that ran after it
        would already have done the thing it was refusing.

        Note what this is not: a lock on a settings key is not a lock on a
        capability. The benchmark COMMAND reaches wave.open with its own wav
        argument and never comes through here at all - which is why `file_path`
        is checked for SHAPE here instead of being locked, and why the same
        check also guards that other door. See safe_paths.
        """
        if not patch:
            return {}, []
        errors = []
        if remote:
            blocked = [k for k in patch if k in settings_mod.REMOTE_LOCKED]
            for key in blocked:
                patch = dict(patch)
                patch.pop(key)
                errors.append(
                    "'{0}' can only be set when starting the program or from "
                    "the desktop panel.".format(key))
            # Same position, same reason as the pops above: validate()'s
            # capture="file" rule stats this value, and on Windows stat-ing a
            # UNC path IS the outbound authentication being refused. A blank
            # is left alone - clearing the box is not an attack, and validate
            # already has the rule for capture="file" with nothing chosen.
            if str(patch.get("file_path") or "").strip():
                patch = dict(patch)
                try:
                    patch["file_path"] = safe_paths.check_read_path(
                        patch["file_path"], roots=self._audio_roots())
                except safe_paths.PathRefused as e:
                    patch.pop("file_path")
                    errors.append(str(e))

        with self._lock:
            clean, verrors = settings_mod.validate(patch, self.settings)
            errors.extend(verrors)
            changed = dict((k, v) for k, v in clean.items()
                           if self.settings.get(k) != v)
            if not changed:
                return {}, errors
            rebuild = settings_mod.rebuilds_for(changed, self.settings)
            rebuild.discard("restart")
            rebuild.discard("overlay")     # the front end owns the overlay
            # Nothing here owns the model whisper-server loaded: it reads it
            # once at startup, and app.py restarts that process on request.
            # Left in, it matched no branch in _drain_pending but still made
            # `if rebuild:` true, so every edit of server_model pushed a state
            # document for a rebuild that never happened.
            rebuild.discard("engine")
            self.settings.update(changed)
            # Remember who chose these values. A later trusted patch clears the
            # mark, because the desktop panel typing over a browser's value
            # makes it the panel's value - the mark belongs to the value in the
            # dict now, not to the history of the key.
            if remote:
                self._untrusted.update(changed)
            else:
                self._untrusted.difference_update(changed)
            self._pending.update(changed)
            self._rebuild |= rebuild
            if not self.alive():
                # Nothing is running to pick the patch up, so do it here.
                self._drain_pending()

        self.emit("settings", dict(self.settings))
        for key in sorted(changed):
            self.log("info", "{0} -> {1}".format(key, changed[key]))
        return changed, errors

    def rebuild(self, *components):
        """
        Force a rebuild with no settings change behind it.

        rebuilds_for only fires when a value actually moves, which is right for
        a settings patch and wrong for everything the world does on its own:
        the GPU server coming back up, a device being plugged in again, a model
        file appearing where there wasn't one. Those need the same rebuild with
        the same settings, and there was no way to ask for it.

        Queued like any other patch, so it lands between inferences rather than
        inside one.
        """
        wanted = set(components) & {"backend", "gate", "strategy", "save",
                                    "source"}
        if not wanted:
            return
        with self._lock:
            self._rebuild |= wanted
            if not self.alive():
                self._drain_pending()

    def _drain_pending(self):
        """Apply queued settings. Runs on the worker thread once started."""
        with self._lock:
            if not self._pending and not self._rebuild:
                return
            patch, rebuild = self._pending, self._rebuild
            self._pending, self._rebuild = {}, set()

        # Language is deliberately NOT a backend rebuild: both backends take it
        # per request, so retuning is an attribute write rather than a model
        # reload. Doing it as a rebuild would stall captions for seconds on the
        # CPU path for a change that costs nothing.
        if "language" in patch and "backend" not in rebuild \
                and self.backend is not None:
            try:
                self.backend.set_language(self.settings["language"])
            except BackendError as e:
                self.log("error", str(e))
            except AttributeError:
                rebuild.add("backend")

        if "backend" in rebuild:
            self._build_backend()
        if "gate" in rebuild:
            self._build_gate()
        if "gate" in rebuild or "strategy" in rebuild:
            self._build_strategy()
        if "save" in rebuild:
            self._build_writer()
        if "source" in rebuild:
            self._build_source()
        if rebuild:
            self._push_state()

    # -- component construction -------------------------------------------
    def _build_backend(self):
        s = self.settings
        # A new backend is a new model, so it earns a fresh chance to say it
        # cannot translate - otherwise switching off the turbo model would
        # never clear the warning, and switching TO one would never raise it.
        self._translate_warned = False
        try:
            self.backend = create_backend(
                s["backend"], server_url=s["server_url"],
                model_path=s["model"], language=s["language"],
                local_device=s["local_device"],
                local_compute=s["local_compute"])
            self._last_error = ""
        except BackendError as e:
            self.backend = None
            self._last_error = str(e)
            self.log("error", str(e))

    def _build_gate(self):
        s = self.settings
        # The held result was measured against the old threshold, so keeping it
        # would leave the meter showing a frame count that the new setting would
        # not produce - which is exactly the moment someone is watching it to
        # see whether the change helped.
        self._last_gate = None
        if not s["vad"]:
            if self.gate is not None:
                self.log("info", "Speech detection off - every audible buffer "
                                 "now reaches Whisper, including fans and "
                                 "music.")
            self.gate = None
            return
        # A retune is not a rebuild: the ONNX session does not depend on any
        # of these four numbers - every one of them is applied to the
        # probabilities it returns - and reloading it would cost ~100 ms to
        # change an attribute.
        if self.gate is not None:
            self.gate.configure(threshold=s["vad_threshold"],
                                min_speech_ms=s["vad_min_speech_ms"],
                                neg_threshold=s["vad_neg_threshold"],
                                min_silence_ms=s["vad_min_silence_ms"])
            return
        self.gate = SpeechGate.create(
            model_path=(s["vad_model"] or None),
            threshold=s["vad_threshold"],
            min_speech_ms=s["vad_min_speech_ms"],
            neg_threshold=s["vad_neg_threshold"],
            min_silence_ms=s["vad_min_silence_ms"])
        if self.gate is not None:
            # Both halves of the tuning, because they answer different
            # questions and only the first one is about this buffer: the
            # threshold and the frame count decide whether the buffer is
            # worth an inference, while the end-of-speech threshold and the
            # silence length decide where the talker stopped - which is what
            # silence_at_end_of_chunk cuts on. Reporting only the first pair
            # is how two of these read as inert for as long as they were.
            self.log("info", "Speech gate: Silero VAD (threshold {0}, needs "
                             "{1} frames = {2:.0f} ms of speech; speech ends "
                             "below {3:.2f} after {4:.0f} ms of silence)"
                             .format(self.gate.threshold, self.gate.min_frames,
                                     s["vad_min_speech_ms"],
                                     self.gate.neg_threshold,
                                     s["vad_min_silence_ms"]))

    def _build_strategy(self):
        if self.strategy is None:
            self.strategy = create_strategy(self.settings, self.gate)
        else:
            self.strategy.configure(self.settings, self.gate)

    def _build_writer(self):
        if self.writer is not None:
            self.writer.close()
            self.writer = None
        self.writer = TranscriptWriter.open(self.settings, self.log)

    def _build_source(self):
        # A new source is a new stream: a file that already ended must not keep
        # the session marked finished when the user points it at another one.
        self._drained = False
        old, self.source = self.source, None
        if old is not None:
            try:
                old.stop()
            except Exception as e:
                self.log("warn", "Previous capture did not stop cleanly: "
                                 "{0}".format(e))
        try:
            self.source = audio_sources.create_source(
                self.settings, self._on_audio, self.log,
                trusted="file_path" not in self._untrusted,
                roots=self._audio_roots())
            self.source.start()
            self.log("info", "Capturing: {0}".format(self.source.name))
        except Exception as e:
            self.source = None
            self._last_error = str(e)
            self.log("error", "Capture failed: {0}".format(e))

    # -- audio in ---------------------------------------------------------
    def _on_audio(self, chunk):
        """Called from the capture thread. Must stay cheap."""
        self._audio.put(chunk)

    def feed(self, data):
        """Audio arriving from a browser rather than a local device."""
        source = self.source
        feeder = getattr(source, "feed", None)
        if feeder is None:
            return False
        feeder(data)
        return True

    # -- the worker -------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                self._drain_pending()
                self._tick()
            except Exception as e:
                # Nothing below is allowed to kill this thread: it is the only
                # thing draining the audio queue, and a dead worker leaves the
                # capture thread filling memory behind a UI that still looks
                # alive. The overlay's watchdog exists because this used to
                # happen.
                self._consecutive_errors += 1
                if self._consecutive_errors <= 3 \
                        or self._consecutive_errors % 25 == 0:
                    self.log("error", "Worker error ({0} in a row): {1}".format(
                        self._consecutive_errors, e))
                time.sleep(0.1)

    def _tick(self):
        try:
            chunk = self._audio.get(timeout=0.25)
        except queue.Empty:
            self._meter(None)
            self._check_drained()
            return
        chunks = [chunk]
        while True:
            try:
                chunks.append(self._audio.get_nowait())
            except queue.Empty:
                break
        block = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        if block.size:
            self._live_level = float(np.mean(np.abs(block)))
        self.strategy.feed(block)
        self._meter(None)

        if self._paused.is_set():
            return

        while not self._stop.is_set():
            decision = self.strategy.next()
            if decision.action == "wait":
                break
            if decision.behind and \
                    time.monotonic() - self._last_lag_report > LAG_REPORT_SEC:
                self._last_lag_report = time.monotonic()
                self.log("warn", decision.hint or
                         "{0:.1f}s behind real time - skipped ahead to stay "
                         "live.".format(decision.behind))
            if decision.action == "skip":
                self._stats["skipped"] += 1
                self._meter(decision)
                now = time.monotonic()
                if now - self._last_skip_report > SKIP_REPORT_SEC:
                    self._last_skip_report = now
                    if decision.hint:
                        self.log("warn", decision.hint)
                continue
            self._meter(decision)
            self._transcribe(decision)

    def _decode_options(self):
        """
        The per-request decode parameters, straight from settings.

        Derived from whisper_backends.DECODE_OPTIONS rather than listed here.
        This function used to name its eleven keys by hand, and when five more
        were added to settings.py and to the backend the copy here was not
        touched - so entropy_thold, logprob_thold, max_len, split_on_word and
        carry_initial_prompt each generated a CLI flag and a control in the
        panel, and then went nowhere. Every one of them was dead, silently,
        and only a review that traced the value all the way to the wire found
        it. A second list of option names is a second place to forget.
        """
        return dict((key, self.settings[key]) for key in DECODE_OPTIONS
                    if key in self.settings)

    def _transcribe(self, decision):
        s = self.settings
        if self.backend is None:
            self._build_backend()
            if self.backend is None:
                return

        # Normalised once here, for the whole window, rather than per ~128 ms
        # chunk on the way in - see audio_sources. Per-chunk normalisation is a
        # crude AGC that pumps the gain inside a single word, and it erases the
        # level the gate above just measured.
        peak = (float(np.max(np.abs(decision.audio)))
                if decision.audio.size else 0.0)
        audio = decision.audio / peak if peak > 0.02 else decision.audio

        t0 = time.monotonic()
        try:
            results = self.backend.transcribe(
                audio, translate=s["translate"], options=self._decode_options())
            self._consecutive_errors = 0
            self._last_error = ""
        except Exception as e:
            # A server that died mid-session fails every pass, roughly every
            # --slide seconds, forever. Log the first few then back off; the
            # auto backend needs those first failures to reach it in order to
            # decide it should fall back at all.
            self._consecutive_errors += 1
            self._last_error = str(e)
            if self._consecutive_errors <= 3 \
                    or self._consecutive_errors % 10 == 0:
                self.log("error", "Transcription failed ({0} in a row): "
                                  "{1}".format(self._consecutive_errors, e))
            return
        infer_s = time.monotonic() - t0

        self._stats["windows"] += 1
        self._stats["infer_total"] += infer_s
        self._stats["audio_total"] += decision.duration
        self._report_perf(infer_s, decision)

        kept, dropped = self.strategy.filter(results)
        for segment in dropped:
            self._stats["dropped"] += 1
            self.emit("dropped", {"text": _text_of(segment),
                                  "reason": "overlap duplicate"})
        for segment in kept:
            self._publish(segment, decision, infer_s)

    def _publish(self, segment, decision, infer_s):
        s = self.settings
        text = _text_of(segment).strip()
        if not text:
            return
        language = _attr(segment, "language", "??")
        detection = getattr(self.backend, "last_detection", None) or {}
        entry = {
            "id": self._next_id,
            "text": text,
            "language": language,
            "translated": self._translated(segment, text, language),
            "words": _attr(segment, "words", None) or [],
            "probability": _attr(segment, "probability", None),
            "no_speech_prob": _attr(segment, "no_speech_prob", None),
            "avg_logprob": _attr(segment, "avg_logprob", None),
            "t_start": round(decision.t_start, 3),
            "duration": round(decision.duration, 3),
            "level": round(decision.level, 5),
            "backend": getattr(self.backend, "active_name", "?"),
            "processing_time": round(infer_s, 3),
            "wall": time.time(),
            "detection": detection,
        }
        self._next_id += 1
        self._stats["captions"] += 1
        if language and language != "??":
            self._recent_languages[language] += 1
        self._history.append(entry)
        if self.writer is not None:
            self.writer.write(entry)
        self.emit("transcript", entry)

    def _translated(self, segment, text, language):
        """
        Whether this caption IS English - not whether English was asked for.

        This was `bool(settings["translate"])`, the checkbox, and that made the
        front ends' "ja -> EN" tag a restatement of the request: it could not
        disagree with itself, so a model that ignored the translate task
        produced Japanese captions labelled as English and no code path
        anywhere could notice. Measured now, from two independent pieces of
        evidence, and only ever downgraded - a caption is called translated
        only when nothing says otherwise:

        1. What the backend says it did (`Segment.translated`). whisper-server
           reports it per response and overrides the request itself for an
           English-only model; faster-whisper echoes the task it ran.
        2. What came back. A caption still in the source language's script
           cannot be an English translation, whatever either side claims -
           see looks_untranslated, which is also the ONLY thing that catches a
           model that accepts the translate task and quietly ignores it.

        The gap in (2) is stated where it matters: a Latin-script source
        decoded rather than translated reads as English here, so French
        captions can still slip through labelled as a translation. (1) still
        covers those whenever the backend answers honestly.
        """
        if not self.settings["translate"]:
            return False
        if getattr(segment, "translated", None) is False:
            self._warn_translate(
                "the engine reports it transcribed this window rather than "
                "translating it. An English-only model (ggml-*.en) cannot "
                "translate and whisper-server turns the option off by itself "
                "when one is loaded.")
            return False
        if looks_untranslated(text):
            self._warn_translate(
                "the caption came back in {0}. The model accepted the "
                "translate task and decoded the audio anyway, which is what a "
                "*-turbo model does - it is a transcription-only distillation. "
                "Load ggml-large-v3, medium or base to translate.".format(
                    language if language and language != "??"
                    else "the source language"))
            return False
        return True

    def _warn_translate(self, why):
        """Say it once per backend, not once per caption."""
        if self._translate_warned:
            return
        self._translate_warned = True
        self.log("warn", "Translation is on but " + why + " Captions are "
                         "labelled with the language they are actually in, "
                         "so the '-> EN' tag is gone until this is fixed.")

    def _report_perf(self, infer_s, decision):
        s = self.settings
        # What "keeping up" means depends on the strategy: a sliding window has
        # to finish inside its slide, a wait-for-silence chunk inside the chunk
        # it is about to collect.
        slide = (s["slide"] if s["strategy"] == "sliding_window"
                 else s["chunk_length"])
        self.emit("perf", {
            "infer_s": round(infer_s, 3),
            "window_s": round(decision.duration, 2),
            "xrt": round(infer_s / max(decision.duration, 0.001), 3),
            "slide_s": slide,
            "backend": getattr(self.backend, "active_name", "?"),
            "sustainable": infer_s <= slide,
            "pending_s": round(self.strategy.pending_seconds(), 2),
        })
        if infer_s <= slide:
            return
        now = time.monotonic()
        if now - self._last_perf_report <= PERF_REPORT_SEC:
            return
        self._last_perf_report = now
        # Ask which backend is actually serving. Under --backend auto that
        # changes mid-session, and naming the wrong one sends people off to
        # tune a server that is not even running.
        head = ("Inference {0:.1f}s per {1:.0f}s window, longer than the {2}s "
                "step - ".format(infer_s, decision.duration, slide))
        if getattr(self.backend, "active_name", "") == "server":
            self.log("warn", head + "the GPU is not holding real-time pace "
                                    "with this model. Restart the server with "
                                    "a smaller or more-quantized one, lower "
                                    "the audio context, or raise the slide. "
                                    "Run the benchmark to see what this "
                                    "machine can actually sustain.")
        else:
            self.log("warn", head + "this is the CPU fallback, which is "
                                    "expected to lag. Start the GPU server to "
                                    "get back to real time, or point the model "
                                    "path at a smaller faster-whisper model.")

    def _meter(self, decision):
        # The gate only runs when a window is complete, which at a 4 s buffer is
        # once every couple of seconds - but the meter ticks eight times a
        # second. Holding the last result means the speech bar shows the most
        # recent real measurement between windows instead of blanking to "-"
        # for nine ticks out of ten, which reads as "the VAD is not working".
        gate = getattr(decision, "gate", None) if decision else None
        if gate is not None:
            self._last_gate = gate
        now = time.monotonic()
        if now - self._last_meter < METER_INTERVAL_SEC:
            return
        self._last_meter = now
        gate = gate or self._last_gate
        payload = {
            "level": round(self._live_level, 5),
            "threshold": self.settings["silence_threshold"],
            "pending_s": round(self.strategy.pending_seconds(), 2)
                         if self.strategy else 0.0,
            "queue": self._audio.qsize(),
            "gated": getattr(decision, "reason", "") if decision else "",
            "speech_frames": getattr(gate, "frames", None),
            "min_frames": getattr(gate, "min_frames", None)
                          or (self.gate.min_frames if self.gate else None),
            "vad": self.gate is not None,
        }
        self.emit("meter", payload)

    # -- introspection ----------------------------------------------------
    def push_state(self):
        """Ask for a state event now. Used by the front end after a command."""
        self._push_state()

    def _push_state(self):
        self.emit("state", self.status())

    def status(self):
        backend = self.backend
        source = self.source
        stats = dict(self._stats)
        if stats["windows"]:
            stats["mean_infer_s"] = round(
                stats["infer_total"] / stats["windows"], 3)
            stats["xrt"] = round(
                stats["infer_total"] / max(stats["audio_total"], 0.001), 3)
        return {
            "running": self.alive(),
            "paused": self._paused.is_set(),
            "uptime_s": round(time.monotonic() - self._started_at, 1)
                        if self._started_at else 0.0,
            "backend": getattr(backend, "active_name", None),
            "backend_name": getattr(backend, "name", None),
            "source": getattr(source, "name", None),
            "source_alive": bool(source and source.alive()),
            "gate": bool(self.gate),
            "gate_model": getattr(self.gate, "model_path", None),
            "strategy": getattr(self.strategy, "name", None),
            "saving": self.writer.path if self.writer else None,
            "saved_lines": self.writer.count if self.writer else 0,
            "error": self._last_error,
            "consecutive_errors": self._consecutive_errors,
            "languages": dict(self._recent_languages.most_common(6)),
            "stats": stats,
        }

    def drained(self):
        """
        True once a finite source has ended and everything it produced is out.

        Only ever true for a source that finishes - a WAV file. A capture device
        is never "done", so this stays False and the caller waits for Ctrl+C, the
        way it always has.
        """
        return self._drained

    def _check_drained(self):
        """
        Notice the end of a file, flush the tail, and say so. Worker thread only.

        The tail matters: a sliding window always holds back buffer minus slide
        seconds as the overlap for the next pass, and a wait-for-silence chunk
        holds back everything until somebody stops talking. Live, that is
        invisible. At the end of a file it is the last few seconds of the
        transcript simply never appearing.
        """
        if self._drained or self.settings["capture"] != "file":
            return
        source = self.source
        if source is None or source.alive() or not self._audio.empty():
            return
        try:
            decision = self.strategy.flush() if self.strategy else None
            if decision is not None and decision.action == "transcribe":
                self._transcribe(decision)
        except Exception as e:
            self.log("warn", "Could not transcribe the tail of the file: "
                             "{0}".format(e))
        self._drained = True
        self.log("info", "Reached the end of the file - {0} captions.".format(
            self._stats["captions"]))
        self._push_state()

    def history(self, limit=200):
        items = list(self._history)
        return items[-limit:] if limit else items

    def clear_history(self):
        self._history.clear()
        self._recent_languages.clear()

    # -- benchmark --------------------------------------------------------
    def benchmark(self, buffers=(4, 8, 16, 24), wav=None, reps=2,
                  headroom=1.5, min_overlap=3, trusted=False):
        """
        Time this machine through the backend that is actually serving.

        Runs on the live backend on purpose: measuring a separately-constructed
        one would report a model that is not the one producing your captions,
        and under --backend auto would not even know which hardware it is on.
        Capture keeps running and the worker is paused for the duration, so the
        numbers are not competing with live inference.

        `wav` is the door REMOTE_LOCKED cannot reach: it arrives as a command
        argument rather than a settings patch, so nothing that filters patches
        ever sees it. `trusted=False` by default because the socket is the
        caller that matters; wpf_panel passes True.
        """
        import benchmark as bench

        if self.backend is None:
            self.emit("benchmark", {"status": "error",
                                    "msg": "No backend is available."})
            return None
        sizes = sorted(set(int(b) for b in buffers if int(b) >= 1))
        if not sizes:
            self.emit("benchmark", {"status": "error",
                                    "msg": "No buffer sizes to measure."})
            return None
        roots = self._audio_roots()
        if wav:
            # Refused here, before pause(True), so a rejected path does not
            # stop live captions for the length of a benchmark that is not
            # going to run. load_wav checks again at the sink; the two are the
            # same call and the second one is the one that would still be
            # there if this method were bypassed.
            try:
                wav = safe_paths.check_read_path(wav, trusted=trusted,
                                                 roots=roots,
                                                 what="benchmark WAV")
            except safe_paths.PathRefused as e:
                self.emit("benchmark", {"status": "error", "msg": str(e)})
                return None

        self.pause(True)
        try:
            if wav:
                source = bench.load_wav(wav, trusted=trusted, roots=roots)
                described = "{0} ({1:.1f}s of real audio)".format(
                    wav, len(source) / float(SAMPLERATE))
                synthetic = False
            else:
                source = bench.synth_audio(30.0)
                described = "synthetic tones - encoder floor only"
                synthetic = True
            self.emit("benchmark", {"status": "running", "audio": described,
                                    "synthetic": synthetic, "sizes": sizes,
                                    "rows": []})
            rows = []
            for secs in sizes:
                buf = bench.make_buffer(source, secs)
                times, chars = bench.measure(
                    self.backend, buf, self.settings["translate"], reps)
                row = {"buffer": secs, "median": bench.median(times),
                       "min": min(times), "max": max(times),
                       "chars": chars // max(reps, 1)}
                row["xrt"] = row["median"] / float(secs)
                row["slide"] = bench.slide_for(row, headroom, min_overlap)
                rows.append(row)
                self.emit("benchmark", {"status": "row", "row": row,
                                        "rows": rows})
            best, slide = bench.recommend(rows, headroom, min_overlap)
            payload = {
                "status": "done", "rows": rows, "synthetic": synthetic,
                "audio": described,
                "backend": getattr(self.backend, "active_name", "?"),
                "recommend": ({"buffer": best["buffer"], "slide": slide}
                              if best else None),
            }
            self.emit("benchmark", payload)
            return payload
        except Exception as e:
            self.emit("benchmark", {"status": "error", "msg": str(e)})
            return None
        finally:
            self.pause(False)


def _text_of(segment):
    """Text of a Segment, which is also a (text, language) 2-tuple."""
    return getattr(segment, "text", None) or (
        segment[0] if isinstance(segment, (tuple, list)) and segment else "")


def _attr(segment, name, default):
    value = getattr(segment, name, default)
    return default if value is None else value
