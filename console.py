"""
console.py - the console window, for the runs where it is not the front end.

Why this exists
---------------
Started from `Start Transcription.vbs`, this program used to put three
windows on screen: run_pipeline.cmd's own console, which asks the setup
questions and then hosts Python; a second console holding whisper-server;
and the panel. Only the third one is the user interface. The other two are
a log with a title bar.

So the server's console goes away entirely - app.py starts it windowless
now and pumps its output into the panel's Log tab, which is where someone
looking for it would look - and this module deals with the one that is
left. It cannot simply not exist: run_pipeline.cmd needs it to ask its
questions, and by the time Python is running the console is already open
and already owns the process. What it can do is stop being *visible*, once
something else is on screen to replace it.

Two things follow from hiding it, and both are handled here rather than
left to the caller:

  * Whatever would have been printed has to go somewhere. capture() tees
    stdout and stderr into a per-session file under logs/, so a traceback
    that lands after the window is gone is still readable afterwards. The
    tee is installed BEFORE anything is hidden and stays installed for the
    life of the process, so there is no window in which output can be lost.

  * The window has to come back. cmd is still sitting behind Python and
    will run its "Finished. It is safe to close this window." and its
    pause when Python exits - into a window nobody can see, waiting for a
    keypress nobody can give it. restore() is registered with atexit and
    called on every exit path, including the crashing ones.

Nothing here decides WHETHER to hide. app.py does, and only once a front
end has actually opened: hiding unconditionally would turn "the .NET
runtime is missing" from a printed sentence into a process with no window
and no panel, which is the one outcome worse than three windows.

Not Windows: hide() and restore() are no-ops, and capture() still writes
the file. There is nothing to hide, and nothing that needs to know that.
"""

import atexit
import glob
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")

# How many session logs to keep. Rotation is per RUN rather than per byte:
# the console emits a line or two per caption, so a session log is a few KB
# and the useful question is "what happened the last few times I ran it",
# not "what happened in the last megabyte". logs/ is already gitignored.
KEEP_LOGS = 10

SW_HIDE = 0
SW_SHOW = 5

_state = {"file": None, "path": None, "stdout": None, "stderr": None,
          "hidden": False}


class _Tee(object):
    """
    One stream written to two places, with the log file line-stamped.

    Timestamps are added at line starts only, tracked across write() calls,
    because write() is handed whatever chunk the caller produced - print()
    alone makes two of them, the text and the newline - and stamping every
    chunk would put a time in the middle of a sentence.
    """

    def __init__(self, stream, handle, tag):
        self._stream = stream
        self._handle = handle
        self._tag = tag
        self._at_line_start = True

    def write(self, text):
        if self._stream is not None:
            try:
                self._stream.write(text)
            except Exception:
                # The console can be closed out from under us. Losing the
                # visible copy is not a reason to lose the logged one.
                pass
        self._log(text)
        return len(text)

    def _log(self, text):
        handle = self._handle
        if handle is None or not text:
            return
        try:
            for piece in text.splitlines(True):
                if self._at_line_start:
                    handle.write("{0:%H:%M:%S} {1} ".format(
                        datetime.now(), self._tag))
                handle.write(piece)
                self._at_line_start = piece.endswith(("\n", "\r"))
            # Flushed per write, not per buffer. The output is a few lines a
            # second at worst, and the run this file exists for is the one
            # that ends in a traceback - a buffered tail is exactly the part
            # that would be missing.
            handle.flush()
        except Exception:
            pass

    def flush(self):
        for target in (self._stream, self._handle):
            if target is not None:
                try:
                    target.flush()
                except Exception:
                    pass

    def isatty(self):
        # Asked by anything deciding whether to colour its output. The
        # honest answer is the console's, not the log file's.
        try:
            return bool(self._stream is not None and self._stream.isatty())
        except Exception:
            return False

    def __getattr__(self, name):
        # encoding, errors, fileno, buffer - whatever a caller reaches for
        # on a real stream, answered by the real stream.
        return getattr(self._stream, name)


def _rotate(directory, keep):
    """Delete all but the newest `keep` session logs."""
    try:
        existing = sorted(glob.glob(os.path.join(directory, "session_*.log")))
    except OSError:
        return
    for path in existing[:max(0, len(existing) - keep + 1)]:
        try:
            os.remove(path)
        except OSError:
            # A log being read in an editor is not a reason to refuse to
            # start, and there is nowhere to report it yet anyway.
            pass


def capture(directory=None, keep=KEEP_LOGS):
    """
    Start teeing stdout and stderr into a fresh session log. Idempotent.

    Returns the path, or None if the file could not be opened - in which
    case nothing is installed and printing carries on exactly as before.
    A run that cannot write a log is still a run that can transcribe.
    """
    if _state["file"] is not None:
        return _state["path"]
    directory = directory or LOG_DIR
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory)
        _rotate(directory, keep)
        path = os.path.join(directory, "session_{0:%Y%m%d_%H%M%S}.log".format(
            datetime.now()))
        handle = open(path, "w", encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None

    _state.update({"file": handle, "path": path,
                   "stdout": sys.stdout, "stderr": sys.stderr})
    sys.stdout = _Tee(sys.stdout, handle, "   ")
    sys.stderr = _Tee(sys.stderr, handle, "ERR")
    atexit.register(_close)
    return path


def path():
    """The session log's path, or None if capture() never ran."""
    return _state["path"]


def record(tag, text):
    """
    Write straight into the session log, without printing.

    For output that belongs in the file but not on anybody's screen. The
    case it exists for is whisper-server's, which prints four lines for
    every request it serves: that is the console window this program used
    to give it, and losing it entirely would be a worse trade than the
    window was. It goes here in full, while app.py forwards only the
    startup and the surprises to the panel's Log tab.

    A no-op when capture() never ran, so a plain console session - where
    the server keeps its own window anyway - writes no file.
    """
    handle = _state["file"]
    if handle is None or not text:
        return
    try:
        for line in str(text).splitlines():
            handle.write("{0:%H:%M:%S} {1} {2}\n".format(
                datetime.now(), tag, line))
        handle.flush()
    except Exception:
        pass


def _close():
    restore()
    handle, _state["file"] = _state["file"], None
    # Cleared with the handle, not left behind: path() answers "where is this
    # run being logged", and a closed file is nowhere. record() tests the
    # handle, but a caller that trusted a stale path would write to a file
    # nothing is flushing.
    _state["path"] = None
    if _state["stdout"] is not None:
        sys.stdout, _state["stdout"] = _state["stdout"], None
    if _state["stderr"] is not None:
        sys.stderr, _state["stderr"] = _state["stderr"], None
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass


def _window():
    """The console window this process owns, or None."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    except Exception:
        return None
    return hwnd or None


def hide():
    """
    Take the console off the screen. True if it went.

    Refuses unless capture() is holding the output, because a hidden
    console with nothing teeing it is output thrown away rather than moved.
    """
    if _state["hidden"] or _state["file"] is None:
        return False
    hwnd = _window()
    if hwnd is None:
        return False
    import ctypes

    ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
    _state["hidden"] = True
    # Registered after the hide rather than at import, so a process that
    # never hides never installs a handler that would un-hide somebody
    # else's window if these functions are ever called out of order.
    atexit.register(restore)
    return True


def restore():
    """Put it back. Safe to call when it was never hidden."""
    if not _state["hidden"]:
        return
    _state["hidden"] = False
    hwnd = _window()
    if hwnd is None:
        return
    try:
        import ctypes

        ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW)
    except Exception:
        pass
