"""
live_transcription.py - the console entry point.

Why this file is now short
--------------------------
It used to be the whole program: argument parsing, device selection, the audio
plumbing, the worker loop, the buffering policy, the duplicate filter, the file
writing and the Tk overlay, all as module-level code that ran on import. That
shape works for exactly one caller with every decision fixed at startup, which
is why the guided launcher could only offer a handful of the flags and why
nothing could be changed without stopping and retyping.

Those parts now live in modules that a browser can also drive:

    settings.py        every option, declared once - this file's flags included
    audio_sources.py   loopback / recording device / browser / WAV
    speech_gate.py     Silero: is there speech, and where did it stop
    buffering.py       sliding window, or wait-for-silence
    whisper_backends.py  GPU server / CPU fallback, with word confidence
    pipeline.py        the worker loop, reconfigurable while it runs
    overlay.py         the caption strip
    app.py             wires those together, plus the optional web panel

What is left here is the console-specific part: the interactive device picker,
which is the one piece a web UI must not have and a terminal should keep.

Every flag this script accepted before still works, spelled the same way. Add
--web to get the control panel on top of it.
"""

import sys

import audio_sources
import settings as settings_mod
from app import App


def choose_device(settings):
    """
    Ask which device to capture, the way this script always has.

    Skipped when --device was given, when the capture mode does not have a
    device to pick (a WAV, or audio streamed from a browser), or when stdin is
    not a terminal - a scheduled task or a piped run would otherwise block
    forever on a prompt nobody can see. The web panel picks from a dropdown
    instead, which is the whole reason this stayed here rather than moving into
    audio_sources.
    """
    mode = settings["capture"]
    if mode not in ("loopback", "input") or settings["device"]:
        return
    if not sys.stdin or not sys.stdin.isatty():
        return

    devices = audio_sources.list_devices()
    for problem in devices.get("errors", []):
        print("[audio] {0}".format(problem))
    listing = devices["loopback"] if mode == "loopback" else devices["input"]
    if not listing:
        print("[audio] No {0} devices found - falling back to the system "
              "default.".format(mode))
        return

    if mode == "loopback":
        print("Output devices (loopback capture - pick the one you LISTEN on):")
    else:
        print("Recording devices:")
    default = ""
    for index, device in enumerate(listing):
        marker = ""
        if device.get("default"):
            marker = "  <- Windows default"
            default = device["id"]
        extra = ""
        if mode == "input":
            extra = "  [{0}, {1} Hz]".format(device.get("hostapi", "?"),
                                             device.get("samplerate", "?"))
        print("{0} {1}{2}{3}".format(index, device["name"], extra, marker))

    prompt = ("Select device [Enter = default]: " if default
              else "Select device number: ")
    while True:
        try:
            choice = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice == "" and default:
            settings["device"] = default
            return
        try:
            index = int(choice)
        except ValueError:
            print("Invalid choice - enter a number from the list"
                  + (", or Enter for the default." if default else "."))
            continue
        # Explicit range check: listing[-1] would silently pick the last device.
        if 0 <= index < len(listing):
            settings["device"] = listing[index]["id"]
            print("Capturing: {0}".format(listing[index]["name"]))
            return
        print("Invalid choice - enter 0-{0}{1}".format(
            len(listing) - 1, ", or Enter for the default." if default else "."))


def main(argv=None):
    parser = settings_mod.build_parser(
        "Live system audio transcription with a draggable overlay, optional "
        "translation, and an optional browser control panel (--web).")
    settings, args = settings_mod.from_args(parser, argv)

    if args.list_devices:
        audio_sources.print_devices()
        return 0

    # The panels have their own pickers, and a prompt would block the process
    # before it ever gets far enough to open the page or window that could
    # answer it.
    if not settings["web"] and not settings["wpf"]:
        choose_device(settings)

    App(settings, start_server=args.start_server).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
