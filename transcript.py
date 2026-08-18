"""
transcript.py - where captions go once they exist.

Why this exists
---------------
Saving used to be three lines in the worker: open a file in append mode, write
text + "\n", flush. That is the right amount of machinery for one format, and
the wrong amount as soon as the word timings arrive.

Both backends can now report per-word start/end/probability (the whisper.cpp
server has always returned them under segments[].words[]; we simply discarded
them). Once a caption knows when each of its words was said, a live session is
already carrying everything a subtitle file needs - so "save the transcript"
can mean .srt or .vtt, not only .txt, without asking the user to run anything
afterwards.

The formats differ in one way that matters to the caller: txt and jsonl are
append-only and survive being killed with Ctrl+C mid-sentence, while srt and
vtt need a header and sequential numbering. All four are written incrementally
and flushed per caption anyway, because the normal way this program ends is
Ctrl+C, and a transcript that only exists after a clean shutdown is a
transcript you will eventually lose.
"""

import json
import os
from datetime import datetime

EXTENSIONS = {"txt": ".txt", "jsonl": ".jsonl", "srt": ".srt", "vtt": ".vtt"}


def default_path(fmt):
    """transcript_YYYYmmdd_HHMMSS with the extension the format implies."""
    return "transcript_{0:%Y%m%d_%H%M%S}{1}".format(
        datetime.now(), EXTENSIONS.get(fmt, ".txt"))


def resolve_path(settings):
    """
    The file a given settings dict wants to write to.

    An explicit --output wins, but its extension is corrected to match the
    format: asking for srt and getting a file called notes.txt full of subtitle
    timecodes helps nobody, and the mismatch is silent otherwise.
    """
    fmt = settings.get("transcript_format", "txt")
    path = (settings.get("output") or "").strip()
    if not path:
        return default_path(fmt)
    want = EXTENSIONS.get(fmt, ".txt")
    stem, ext = os.path.splitext(path)
    if ext.lower() != want:
        return stem + want
    return path


def _clock(seconds, comma=True):
    """Seconds as HH:MM:SS,mmm (SubRip) or HH:MM:SS.mmm (WebVTT)."""
    if seconds is None or seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000.0))
    ms = total_ms % 1000
    total = total_ms // 1000
    return "{0:02d}:{1:02d}:{2:02d}{3}{4:03d}".format(
        total // 3600, (total % 3600) // 60, total % 60,
        "," if comma else ".", ms)


class TranscriptWriter(object):
    """
    One open transcript file. Create it with open(); it never raises at write
    time.

    Write failures disable saving and report once, rather than propagating.
    The original worker learned this the hard way: the write sat outside the
    try that guarded transcribe(), so a full disk or an unplugged USB drive
    killed the worker thread outright and the overlay carried on showing a
    frozen caption with nothing in the log.
    """

    def __init__(self, path, fmt, handle, on_log=None):
        self.path = path
        self.fmt = fmt
        self._fh = handle
        self._log = on_log or (lambda level, msg: None)
        self.count = 0
        self._cue = 0
        self._failed = False

    @classmethod
    def open(cls, settings, on_log=None):
        """A writer, or None if saving is off or the file cannot be opened."""
        log = on_log or (lambda level, msg: None)
        if not settings.get("save"):
            return None
        fmt = settings.get("transcript_format", "txt")
        path = resolve_path(settings)
        directory = os.path.dirname(os.path.abspath(path))
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            # Append for the line formats so an interrupted session can be
            # resumed into the same file. srt and vtt are numbered and headed,
            # so appending to an existing one would produce a file no player
            # will read - those get truncated, and we say so.
            mode = "a" if fmt in ("txt", "jsonl") else "w"
            if mode == "w" and os.path.exists(path):
                log("warn", "Overwriting {0}: {1} files are numbered from 1, "
                            "so appending would produce a file no player "
                            "accepts.".format(path, fmt))
            handle = open(path, mode, encoding="utf-8")
        except OSError as e:
            log("error", "Could not open transcript '{0}': {1}. Saving is "
                         "off.".format(path, e))
            return None

        writer = cls(path, fmt, handle, on_log)
        if fmt == "vtt":
            writer._raw("WEBVTT\n\n")
        log("info", "Saving transcript to {0}".format(path))
        return writer

    # -- writing ----------------------------------------------------------
    def _raw(self, text):
        if self._failed or self._fh is None:
            return
        try:
            self._fh.write(text)
            self._fh.flush()
        except OSError as e:
            # Disk full, or --output pointed at a removable drive that got
            # unplugged. Report once and go quiet; the alternative is one line
            # of the same error per caption for the rest of the session.
            self._failed = True
            self._log("error", "Transcript write failed, saving disabled: "
                               "{0}".format(e))

    def write(self, entry):
        """
        Append one caption.

        `entry` is the transcript event dict the pipeline emits: text,
        language, translated, t_start (seconds since the session began),
        duration, and words (each with start/end relative to the window).
        """
        text = (entry.get("text") or "").strip()
        if not text:
            return
        if self.fmt == "txt":
            self._raw(text + "\n")
        elif self.fmt == "jsonl":
            self._raw(json.dumps(entry, ensure_ascii=False) + "\n")
        else:
            self._raw(self._cue_text(entry, comma=(self.fmt == "srt")))
        self.count += 1

    def _cue_text(self, entry, comma):
        """One SubRip / WebVTT cue."""
        start, end = self._span(entry)
        self._cue += 1
        head = "{0}\n".format(self._cue) if comma else ""
        return "{0}{1} --> {2}\n{3}\n\n".format(
            head, _clock(start, comma), _clock(end, comma),
            entry.get("text", "").strip())

    @staticmethod
    def _span(entry):
        """
        Absolute (start, end) for a caption, in seconds since session start.

        Word times are relative to the window they were decoded in, so the
        window's own offset has to be added back or every cue in an hour-long
        session would claim to happen in the first few seconds. Without word
        timings the whole window is the cue, which is coarse but still lines up.
        """
        base = float(entry.get("t_start") or 0.0)
        words = entry.get("words") or []
        if words:
            first = words[0].get("start")
            last = words[-1].get("end")
            if first is not None and last is not None and last > first:
                return base + float(first), base + float(last)
        duration = float(entry.get("duration") or 0.0)
        return base, base + (duration or 2.0)

    def close(self):
        if self._fh is None:
            return
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None
