"""
overlay.py - the always-on-top caption strip, as an object you can own.

Why this exists
---------------
The overlay used to be fifty lines at the bottom of live_transcription.py: a
bare tk.Tk(), a label, two drag handlers and a poll loop, with every
appearance decision written into the source - Segoe UI 24 bold, white on
black, 0.8 alpha, full screen width by 100 px, 85% down, wrapped at 800 px.
Changing any of it meant editing the program, and the pipeline could not
create or destroy the window without also owning Tk.

What moved here, beyond the settings:

  * The cross-thread rule. Tk may only be touched from the thread running
    its event loop. show()/clear()/configure()/close() therefore queue a
    request and the root.after pump drains it. Calling into Tk from the
    transcription worker instead is the single most common way one of these
    apps dies, with the useless message "main thread is not in main loop"
    from somewhere unrelated to the actual call.

  * The transparency trap. -transparentcolor keys out exactly ONE colour and
    that colour is the window background, which is how the caption appears
    to float over the desktop. It was hardcoded black on a black window, so
    it could never collide with the text; now that both ends are settings, a
    text colour equal to the background makes the LETTERS transparent too
    and the overlay silently shows nothing. Detected below, and answered by
    dropping transparency rather than by rendering a hole.

  * The worker watchdog. A daemon transcription thread that dies leaves
    mainloop() running forever over a stale caption while capture keeps
    filling a queue nobody drains. set_watchdog() ports the guard: the pump
    asks each tick, and on a dead worker it puts the reason on screen and
    closes 2 s later.

start() raises OverlayUnavailable rather than dying when Tk cannot open a
window at all - some remote and service sessions have no display - so the
caller can carry on console-only instead of losing the whole run.
"""

import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

from settings import DEFAULTS

POLL_MS = 100               # the same 100 ms pump the inline overlay used
PAD_PX = 8
WATCHDOG_LINGER_MS = 2000   # ported: read the reason, then the window goes
# Requests handled per tick. Draining until empty means a caller that queues
# faster than Tk can lay out never gives the loop back: six threads calling
# show()/configure() in a tight loop left 32423 requests pending, the window
# frozen and the watchdog unable to run. 64 a tick is 640 a second, far more
# than captions arrive at, and the rest simply waits one more tick.
MAX_DRAIN = 64


class OverlayUnavailable(RuntimeError):
    """Tk is installed but cannot open a window on this session."""


class Overlay(object):
    """
    A caption window driven from other threads.

    start() and run() belong to the main thread; everything else may be
    called from anywhere. on_move(x_pixels, y_pct) fires after a drag so the
    caller can write the new position back into its settings, which is what
    makes dragging the overlay update the UI's Vertical position field.
    """

    def __init__(self, settings, on_move=None, on_close=None):
        # Filled out from DEFAULTS so a partial dict - one key from a web UI
        # patch, say - is a legal argument to both __init__ and configure().
        self._settings = dict(DEFAULTS)
        self._settings.update(settings or {})
        self._on_move = on_move
        self._on_close = on_close

        # The only channel a non-Tk thread may use. See the module docstring.
        self._requests = queue.Queue()

        self._root = None
        self._label = None
        self._text_var = None
        self._font = None
        self._lines = []            # captions still on screen, oldest first
        self._last_text_at = 0.0
        self._drag_from = None
        self._pressed_at = None     # window position when the button went down
        self._x = None              # set by a drag; None means "centred"
        # None rather than True: not decided yet, so the first fg == bg
        # logs exactly once and a later restyle to the same pair does not.
        self._transparent = None
        self._alive = False
        self._closing = False
        self._closed_notified = False
        self._watchdog = None
        self._watchdog_fired = False
        # Faults are reported once each: the pump ticks ten times a second, so
        # a setting Tk keeps rejecting would print six hundred lines a minute.
        self._warned = set()

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        """Create the window. Main thread only; may raise OverlayUnavailable"""
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(
                "Overlay.start() must be called on the MAIN thread: Tk keeps "
                "its event loop there, and a root created anywhere else "
                "fails later, from an unrelated line, with 'main thread is "
                "not in main loop'. Start and run the overlay on the main "
                "thread and drive it with show()/configure(), which are "
                "thread-safe.")
        if self._root is not None:
            return

        # Cleared here as well as in __init__. close() latches _closing so a
        # show() already in flight cannot resurrect a window that is going
        # away; without this reset a start() after a close() came back with
        # alive() reporting True and every show(), clear() and configure()
        # silently dropped, because _request() was still refusing everything.
        self._closing = False
        self._closed_notified = False
        self._watchdog_fired = False
        self._warned = set()
        # A fresh window gets a fresh mailbox. The close() that ended the last
        # one leaves its request behind - the pump answers the _closing latch
        # without reading the queue - and the new window's first tick would
        # otherwise drain that stale close and shut down on the spot.
        while True:
            try:
                self._requests.get_nowait()
            except queue.Empty:
                break

        try:
            root = tk.Tk()
        except Exception as e:
            # No display at all: a service session, or an RDP console that
            # has been disconnected. The caller is expected to catch this and
            # keep transcribing to the console.
            raise OverlayUnavailable(
                "Tk could not open a window ({0}). This session has no "
                "display - run without the overlay.".format(e))

        self._root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)

        self._text_var = tk.StringVar(master=root, value="")
        self._label = tk.Label(root, textvariable=self._text_var,
                               justify="center", anchor="center")
        self._label.bind("<Button-1>", self._press)
        self._label.bind("<B1-Motion>", self._drag)
        self._label.bind("<ButtonRelease-1>", self._release)
        self._label.pack(fill="both", expand=True)

        self._alive = True
        try:
            self._apply(self._settings)
        except Exception as e:
            # One unusable value in the starting dict must not cost the caller
            # the whole overlay - it is the only thing that would then be
            # console-only for no visible reason.
            self._warn("could not apply the starting settings ({0}) - "
                       "falling back to the defaults.".format(e))
            self._settings = dict(DEFAULTS)
            self._apply({})

    def run(self):
        """Run the Tk event loop until close(). Blocks the calling thread."""
        if self._root is None:
            raise RuntimeError("Overlay.run() called before a successful "
                               "start()")
        self._root.after(0, self._pump)
        try:
            self._root.mainloop()
        finally:
            # mainloop() also returns if the window is torn down by something
            # other than close() - a Tk error, a session logoff - and the
            # caller still has to hear about it exactly once.
            self._destroy()

    def alive(self):
        """True between start() and the window actually going away."""
        return self._alive

    def set_watchdog(self, is_alive, message):
        """
        Have the pump check `is_alive()` every tick.

        The first False puts `message` on screen and closes the overlay 2 s
        later. This is the ported "transcription thread stopped" guard: a
        dead worker otherwise leaves a stale caption up forever.
        """
        self._watchdog = (is_alive, message)
        self._watchdog_fired = False

    # -- thread-safe requests ----------------------------------------------
    def show(self, text):
        """Add a caption. Safe from any thread."""
        self._request("show", text)

    def clear(self):
        """Blank the overlay. Safe from any thread."""
        self._request("clear", None)

    def configure(self, settings):
        """Restyle live from a full or partial settings dict. Any thread."""
        self._request("configure", dict(settings or {}))

    def close(self):
        """Destroy the window and end run(). Any thread, idempotent."""
        self._closing = True
        self._request("close", None)

    def _request(self, kind, payload):
        # NOTHING here may touch Tk: this runs on the transcription worker,
        # the web server thread, or a signal handler. The pump owns the
        # widgets; this only ever hands it a message.
        if self._closing and kind != "close":
            return
        self._requests.put((kind, payload))

    # -- the pump ----------------------------------------------------------
    def _pump(self):
        if self._root is None:
            return
        try:
            if self._closing:
                # close() latches synchronously, so it is answered ahead of
                # the queue. Behind it, a burst measured 32423 pending
                # requests, and a close() sitting at the back of that line
                # left the window up and run() blocked for as long as the
                # drain took.
                self._destroy()
                return
            if not self._drain():
                return                      # a close request landed
            if self._check_watchdog():
                return                      # closing in WATCHDOG_LINGER_MS
            self._expire()
        except tk.TclError as e:
            # A destroyed window and a value Tk will not take both arrive as
            # TclError, and only the first one means stop. Returning on either
            # stranded the pump: overlay_fg="notacolour" left the strip frozen
            # on a stale caption, close() ignored, run() never returning and
            # so App.shutdown() never running - the exact hang the watchdog
            # below exists to prevent, arriving with no message at all.
            if not self._window_exists():
                return
            self._warn("Tk would not take a setting ({0}) - keeping the "
                       "previous look.".format(e))
        except Exception as e:
            # Same rule as the transcription worker: nothing gets to kill the
            # only loop that drains the request queue and runs the watchdog.
            self._warn("overlay tick failed ({0}) - carrying on.".format(e))
        if self._root is None:
            return
        try:
            self._root.after(POLL_MS, self._pump)
        except tk.TclError:
            pass

    def _window_exists(self):
        try:
            return self._root is not None and bool(self._root.winfo_exists())
        except tk.TclError:
            return False

    def _warn(self, message):
        if message in self._warned:
            return
        if len(self._warned) > 32:
            self._warned.clear()
        self._warned.add(message)
        print("[overlay] {0}".format(message))

    def _drain(self):
        """Apply every queued request. False once the overlay is closing."""
        # One redraw for the whole batch. Each one re-wraps the label and
        # calls update_idletasks(), so redrawing per request made a backlog
        # take longer to clear than it took to arrive.
        dirty = False
        try:
            for _ in range(MAX_DRAIN):
                try:
                    kind, payload = self._requests.get_nowait()
                except queue.Empty:
                    break

                if kind == "close":
                    self._destroy()
                    return False
                if kind == "show":
                    # str(), not the bare payload: a caption that is not a
                    # string is a caller's bug, and .strip() on it used to
                    # raise AttributeError straight into the pump.
                    text = str(payload if payload is not None else "").strip()
                    if not text:
                        continue
                    self._lines.append(text)
                    del self._lines[:-max(1, self._settings["overlay_lines"])]
                    self._last_text_at = time.monotonic()
                    dirty = True
                elif kind == "clear":
                    self._lines = []
                    dirty = True
                elif kind == "configure":
                    self._apply(payload)
                    dirty = False           # _apply() redraws for itself
            return True
        finally:
            # In a finally so a request that throws still leaves the label
            # showing the captions the ones before it added. Whatever is left
            # in the queue stays there for the next tick.
            if dirty:
                self._redraw()

    def _check_watchdog(self):
        """True once the "worker died" path has taken over the window."""
        if self._watchdog is None or self._watchdog_fired:
            return False
        is_alive, message = self._watchdog
        try:
            still_running = bool(is_alive())
        except Exception as e:
            # The probe itself is broken; treat that as gone rather than
            # raising out of the pump and killing the event loop.
            print("[overlay] watchdog check failed ({0}) - treating the "
                  "worker as stopped.".format(e))
            still_running = False
        if still_running:
            return False

        self._watchdog_fired = True
        print("[overlay] {0}".format(message))
        self._lines = [message]
        self._redraw()
        self._root.after(WATCHDOG_LINGER_MS, self._destroy)
        return True

    def _expire(self):
        """Honour overlay_clear_after. 0 leaves the last caption up."""
        after = self._settings["overlay_clear_after"]
        if after > 0 and self._lines \
                and time.monotonic() - self._last_text_at > after:
            self._lines = []
            self._redraw()

    # -- appearance --------------------------------------------------------
    # _place() and _expire() re-read these on every caption and every tick, so
    # a value that is not a number is turned away here rather than stored: one
    # configure({"overlay_font_size": "huge"}) otherwise raised for the rest
    # of the session, on requests that had nothing to do with it.
    _INT_KEYS = ("overlay_font_size", "overlay_lines", "overlay_width_pct")
    _FLOAT_KEYS = ("overlay_opacity", "overlay_y_pct", "overlay_clear_after")

    def _apply(self, settings):
        """Re-style and re-place the window. Tk thread only."""
        s = dict(self._settings)
        s.update(settings or {})
        for key in self._INT_KEYS:
            s[key] = int(round(float(s[key])))
        for key in self._FLOAT_KEYS:
            s[key] = float(s[key])
        if self._root is None:
            self._settings = s
            return
        root = self._root
        fg = str(s["overlay_fg"]).strip().lower()
        bg = str(s["overlay_bg"]).strip().lower()

        # Held on the instance: a tkfont.Font is a handle to a named font
        # inside the interpreter, and letting the last reference go deletes
        # that font out from under the label still using it.
        font = tkfont.Font(
            root=root, family=s["overlay_font_family"],
            size=s["overlay_font_size"],
            weight="bold" if s["overlay_bold"] else "normal")

        root.configure(bg=bg)
        self._label.configure(font=font, fg=fg, bg=bg)

        # Committed only now. Everything above it can be refused by Tk - an
        # unknown colour name, a size that is not a number - and the refused
        # value must not survive in self._settings, or the window keeps its
        # old look while every later tick re-reads the value that broke it.
        self._settings = s
        self._font = font

        # -transparentcolor keys out one colour, and that colour has to be
        # the background for the strip to float over the desktop. Text in
        # that same colour is keyed out with it, so the caption becomes an
        # invisible hole in the screen instead of words. A solid box is the
        # lesser failure.
        transparent = fg != bg
        if transparent != self._transparent:
            self._transparent = transparent
            if not transparent:
                print("[overlay] text colour {0} is the same as the "
                      "background, which is the colour keyed out for "
                      "transparency - the letters would be invisible. "
                      "Showing a solid window instead.".format(fg))
        try:
            root.attributes("-transparentcolor", bg if transparent else "")
        except tk.TclError as e:
            # Tk only implements -transparentcolor on Windows.
            print("[overlay] transparency unavailable ({0}) - the caption "
                  "strip will be a solid box.".format(e))
        try:
            root.attributes("-alpha", s["overlay_opacity"])
        except tk.TclError as e:
            self._warn("opacity not supported here ({0}) - the strip stays "
                       "fully opaque.".format(e))

        keep = max(1, s["overlay_lines"])
        del self._lines[:-keep]

        if s["overlay"]:
            root.deiconify()
            root.attributes("-topmost", True)
        else:
            root.withdraw()
        self._redraw()

    def _redraw(self):
        """Newest caption last, one per line, then re-fit the window."""
        if self._root is None:
            return
        self._text_var.set("\n".join(self._lines))
        self._place()

    def _place(self):
        # Nothing re-places the window while the mouse is holding it. Captions
        # land every couple of seconds, and one arriving mid-drag used to
        # teleport the strip back to where the drag started: the release then
        # recorded that same old spot, so the move the user just made was
        # silently thrown away. The inline original never re-placed at all.
        if self._drag_from is not None:
            return
        s = self._settings
        root = self._root
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()

        width = max(160, int(screen_w * s["overlay_width_pct"] / 100.0))
        # The inline version wrapped at a hardcoded 800 px inside a
        # full-width window, so on a 2560 px screen a long caption broke in
        # the middle with a third of the strip empty on either side. Wrap at
        # whatever width the window actually has.
        self._label.configure(wraplength=max(80, width - 2 * PAD_PX))

        # A fixed 100 px was right for 24 pt and two lines and nothing else;
        # with the font size now a setting it clipped the descenders at 32 pt
        # and hid the second caption at 40 pt. Measure instead, and never
        # shrink below the nominal line count or the strip would jump about
        # every time a one-line caption followed a two-line one.
        lines = max(1, s["overlay_lines"])
        base = lines * self._font.metrics("linespace") + 2 * PAD_PX
        root.update_idletasks()
        height = max(base, self._label.winfo_reqheight() + 2 * PAD_PX)
        height = min(height, screen_h)

        x = (screen_w - width) // 2 if self._x is None else int(self._x)
        x = max(0, min(x, max(0, screen_w - width)))
        y = int(screen_h * s["overlay_y_pct"])
        y = max(0, min(y, max(0, screen_h - height)))
        root.geometry("{0}x{1}+{2}+{3}".format(width, height, x, y))

    # -- dragging ----------------------------------------------------------
    def _press(self, event):
        if self._settings["overlay_locked"] or self._root is None:
            return
        self._drag_from = (event.x, event.y)
        self._pressed_at = (self._root.winfo_x(), self._root.winfo_y())

    def _drag(self, event):
        if self._drag_from is None or self._root is None:
            return
        root = self._root
        root.geometry("+{0}+{1}".format(
            root.winfo_x() + event.x - self._drag_from[0],
            root.winfo_y() + event.y - self._drag_from[1]))

    def _release(self, event):
        if self._drag_from is None or self._root is None:
            return
        self._drag_from = None
        root = self._root
        if (root.winfo_x(), root.winfo_y()) == self._pressed_at:
            # A click, not a drag - and recording one is worse than ignoring
            # it. winfo_y() is the position AFTER _place() pulled the window
            # up to keep a tall caption on screen, so a stray click while a
            # long caption had the strip two lines deep wrote 0.833 back over
            # the 0.85 the user chose, and the next click walked it up again.
            # Seen unprompted in a file-capture run, not theorised.
            return
        self._x = root.winfo_x()
        y_pct = root.winfo_y() / float(max(1, root.winfo_screenheight()))
        y_pct = round(min(1.0, max(0.0, y_pct)), 3)
        # Kept locally too, so the next configure() places the window where
        # the user left it instead of snapping back to the old y_pct.
        self._settings["overlay_y_pct"] = y_pct
        if self._on_move is not None:
            try:
                self._on_move(self._x, y_pct)
            except Exception as e:
                # The callback persists settings and may talk to a web UI. A
                # failure there must not take the event loop down with it.
                print("[overlay] position callback raised: {0}".format(e))

    # -- teardown ----------------------------------------------------------
    def _destroy(self):
        root, self._root = self._root, None
        self._alive = False
        self._closing = True
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        if self._on_close is not None and not self._closed_notified:
            self._closed_notified = True
            try:
                self._on_close()
            except Exception as e:
                print("[overlay] close callback raised: {0}".format(e))
