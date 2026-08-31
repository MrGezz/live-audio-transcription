"""
model_fetch.py - putting a model in _models\\ without leaving the panel.

Why this exists
---------------
Both model settings are pickers over what is already in _models\\, which is
honest but circular: the list cannot offer a size that was never fetched, and
fetching one meant leaving the app, finding the right Hugging Face repo,
knowing that a GGML model is one file while a faster-whisper model is a
folder of five, and putting the result in the right place under the right
name. The picker then says "large-v3" only if you already guessed correctly.

So the catalog below is the other half of those two pickers: the names are
the ones upstream actually publishes, and `download()` is what turns a name
into an entry in the list the pickers read.

What a name is allowed to be
----------------------------
A name, and nothing else. Never a URL, never a path, never a repo id.

This is reachable from the browser panel, which can be bound off loopback,
and "fetch this URL and write it into the program's folder" is the whole of a
remote-write primitive if the caller picks the URL. So the caller picks an
ENTRY from a fixed catalog and the repo id, the filename and the destination
are all derived here from that entry - the same shape as _engine_start, which
takes a basename and checks it against the real listing rather than trusting
it. `resolve()` is the single gate; nothing else in this module accepts a
name from outside.

The destination is derived too, never passed in: everything lands under
_models\\ by construction, so a name cannot escape it however it is spelled.

Where the files come from
-------------------------
  GGML (whisper.cpp, the GPU server)
      ggerganov/whisper.cpp on Hugging Face - the mirror whisper.cpp's own
      models/download-ggml-model.sh pulls from, so the file this fetches is
      byte-for-byte the one that script would leave behind.

  faster-whisper (the CPU/CUDA fallback)
      the Systran/faster-whisper-* CTranslate2 conversions, which are what
      faster_whisper.WhisperModel("large-v3") resolves to on its own. Fetched
      here explicitly, into _models\\, so the model is a folder this project
      owns and can list rather than something in a user-level cache that the
      picker cannot see.

huggingface_hub does the transfer. It arrives with faster-whisper, so this
adds no dependency; it resumes a part-finished file, verifies what it wrote,
and is the same client faster-whisper would have used anyway.
"""

import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(HERE, "_models")

GGML_REPO = "ggerganov/whisper.cpp"

# GGML files, by the name they are published and stored under. size_mb is the
# published size, used for the label and for the "will this fit" question the
# perf warning sends people here to ask - it is not checked against what
# arrives, which huggingface_hub verifies itself.
#
# Quantised variants only where upstream publishes them. q5_1 and q8_0 are the
# two that matter on a live pipeline: q5_1 is the smallest that stays usable
# and q8_0 is near-lossless, and between them they cover "the GPU cannot hold
# real time" in both directions.
GGML_MODELS = (
    ("ggml-tiny.en-q5_1.bin", 32, "tiny.en, 5-bit - the floor, English only"),
    ("ggml-tiny-q5_1.bin", 33, "tiny, 5-bit - the floor, all languages"),
    ("ggml-base.en-q5_1.bin", 58, "base.en, 5-bit - English only"),
    ("ggml-base-q5_1.bin", 60, "base, 5-bit - the shipped default"),
    ("ggml-small.en-q5_1.bin", 182, "small.en, 5-bit - English only"),
    ("ggml-small-q5_1.bin", 190, "small, 5-bit"),
    ("ggml-small-q8_0.bin", 264, "small, 8-bit - near-lossless small"),
    ("ggml-medium-q5_0.bin", 539, "medium, 5-bit"),
    ("ggml-large-v3-turbo-q5_0.bin", 574, "large-v3-turbo, 5-bit"),
    ("ggml-large-v3-turbo-q8_0.bin", 874, "large-v3-turbo, 8-bit"),
    ("ggml-large-v3-turbo.bin", 1624, "large-v3-turbo, full"),
    ("ggml-large-v3-q5_0.bin", 1080, "large-v3, 5-bit"),
    ("ggml-large-v3.bin", 3095, "large-v3, full - the most accurate"),
)

# faster-whisper models. Each is a FOLDER: model.bin plus config.json,
# tokenizer.json, vocabulary and preprocessor - which is why these are fetched
# with snapshot_download and why _list_models identifies one by looking for
# model.bin or config.json inside a directory rather than by extension.
#
# The local folder name is the repo name with Systran's prefix dropped, so
# what lands in _models\\ reads as faster-whisper-large-v3 - the spelling the
# `model` setting has always used for the one that shipped.
FASTER_MODELS = (
    ("tiny", "Systran/faster-whisper-tiny", 75),
    ("tiny.en", "Systran/faster-whisper-tiny.en", 75),
    ("base", "Systran/faster-whisper-base", 145),
    ("base.en", "Systran/faster-whisper-base.en", 145),
    ("small", "Systran/faster-whisper-small", 484),
    ("small.en", "Systran/faster-whisper-small.en", 484),
    ("medium", "Systran/faster-whisper-medium", 1530),
    ("medium.en", "Systran/faster-whisper-medium.en", 1530),
    ("large-v2", "Systran/faster-whisper-large-v2", 3090),
    ("large-v3", "Systran/faster-whisper-large-v3", 3090),
    ("distil-large-v3", "Systran/faster-distil-whisper-large-v3", 1510),
)

KINDS = ("ggml", "faster_whisper")


class FetchError(Exception):
    """A download that cannot be started, or did not finish."""


def _faster_dir(size):
    """Local folder name for a faster-whisper size. Derived, never passed."""
    return "faster-whisper-" + size


def _dir_bytes(path):
    """Bytes on disk under `path`. 0 for a path that is not there yet."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def installed(kind, name):
    """Is this entry already in _models\\?"""
    if kind == "ggml":
        return os.path.exists(os.path.join(MODEL_DIR, name))
    target = os.path.join(MODEL_DIR, _faster_dir(name))
    return (os.path.exists(os.path.join(target, "model.bin"))
            or os.path.exists(os.path.join(target, "config.json")))


def catalog():
    """
    Everything that can be fetched, and whether it is here already.

    Shaped for a picker: the panels render this straight, so the label is
    written for a human choosing a model rather than for a log line.
    """
    ggml = [{"name": name, "size_mb": mb, "note": note,
             "installed": installed("ggml", name)}
            for name, mb, note in GGML_MODELS]
    faster = [{"name": size, "repo": repo, "size_mb": mb,
               "dir": _faster_dir(size),
               "installed": installed("faster_whisper", size)}
              for size, repo, mb in FASTER_MODELS]
    return {"ggml": ggml, "faster_whisper": faster,
            "dir": os.path.relpath(MODEL_DIR, HERE)}


def resolve(kind, name):
    """
    (repo_id, filename_or_None, destination) for a catalog entry.

    The one gate. Every value that reaches huggingface_hub or the filesystem
    is produced HERE from a matched catalog row - `name` is only ever compared,
    never joined onto a path - so a caller that sends "../../x" or an https://
    URL gets FetchError rather than a clever destination.
    """
    if kind == "ggml":
        for entry, _mb, _note in GGML_MODELS:
            if entry == name:
                return GGML_REPO, entry, MODEL_DIR
        raise FetchError("'{0}' is not a GGML model this app knows how to "
                         "fetch.".format(name))
    if kind == "faster_whisper":
        for size, repo, _mb in FASTER_MODELS:
            if size == name:
                return repo, None, os.path.join(MODEL_DIR, _faster_dir(size))
        raise FetchError("'{0}' is not a faster-whisper model this app knows "
                         "how to fetch.".format(name))
    raise FetchError("'{0}' is not a kind of model; expected one of {1}."
                     .format(kind, ", ".join(KINDS)))


def expected_mb(kind, name):
    """Published size, or 0 when the entry has none. For progress only."""
    if kind == "ggml":
        for entry, mb, _note in GGML_MODELS:
            if entry == name:
                return mb
    elif kind == "faster_whisper":
        for size, _repo, mb in FASTER_MODELS:
            if size == name:
                return mb
    return 0


def _progress_thread(target, total_mb, report, stop):
    """
    Say how far along it is, from what is on disk.

    Deliberately not huggingface_hub's own progress bar: that writes a
    carriage-returned tqdm bar to stderr, and this output goes to a log pane
    and a transcript file where a bar becomes thousands of lines. Polling the
    destination gives the one number worth reporting at a rate a log can hold.
    """
    last = -1
    while not stop.wait(2.0):
        mb = _dir_bytes(target) / 1e6 if os.path.isdir(target) else (
            os.path.getsize(target) / 1e6 if os.path.exists(target) else 0)
        pct = int(100 * mb / total_mb) if total_mb else 0
        # Only on a whole-percent change, so a slow link does not narrate.
        if total_mb and pct != last and pct <= 100:
            last = pct
            report("{0:.0f} of about {1} MB ({2}%)".format(mb, total_mb, pct))
        elif not total_mb and int(mb) != last:
            last = int(mb)
            report("{0:.0f} MB".format(mb))


def download(kind, name, report=None):
    """
    Fetch one catalog entry into _models\\. Blocking - never on the loop.

    Returns the path that now exists. Raises FetchError with a sentence that
    says what to do, because every caller of this puts the message in front of
    a person: there is no retry that helps when huggingface_hub cannot resolve
    the host.
    """
    report = report or (lambda _msg: None)
    repo, filename, target = resolve(kind, name)

    try:
        from huggingface_hub import hf_hub_download, snapshot_download
    except ImportError:
        raise FetchError(
            "huggingface_hub is not installed, so nothing can be downloaded. "
            "It normally arrives with faster-whisper: pip install "
            "huggingface_hub")

    try:
        os.makedirs(MODEL_DIR, exist_ok=True)
    except OSError as e:
        raise FetchError("Could not create {0}: {1}".format(MODEL_DIR, e))

    total_mb = expected_mb(kind, name)
    watched = target if filename is None else os.path.join(target, filename)
    stop = threading.Event()
    ticker = threading.Thread(
        target=_progress_thread, args=(watched, total_mb, report, stop),
        name="fetch-progress", daemon=True)
    ticker.start()
    try:
        if filename is None:
            # allow_patterns, not the whole repo: several of these carry an
            # original-weights .pt or a flax checkpoint alongside the
            # CTranslate2 conversion, and pulling those doubles or triples the
            # transfer for files faster-whisper will never open.
            out = snapshot_download(
                repo_id=repo, local_dir=target,
                allow_patterns=["*.bin", "*.json", "*.txt", "*.model"])
        else:
            out = hf_hub_download(repo_id=repo, filename=filename,
                                  local_dir=target)
    except Exception as e:
        raise FetchError("Could not download {0} from {1}: {2}".format(
            name, repo, e))
    finally:
        stop.set()

    if not os.path.exists(out):
        raise FetchError("{0} reported success but left nothing at {1}."
                         .format(repo, out))
    return out
