r"""
safe_paths.py - the one answer to "may THIS caller name THAT file?".

Why this exists
---------------
settings.REMOTE_LOCKED settles the write side by naming keys a browser may not
set. It cannot settle the read side, and the difference is worth stating once
because it is not obvious: a lock on a settings key is not a lock on a
capability. A wav path reaches wave.open by two doors - `file_path` in a
settings patch, and the `wav` argument of the benchmark COMMAND, which is not a
patch and never passes through Pipeline.apply at all. Locking the key would
have shut one door, removed a working browser feature on the way past, and
left the other door standing open.

So the rule lives at the sink instead, and it is a shape rather than a name.
Every untrusted read path in the tree comes through check_read_path().

What is actually dangerous, worst first
---------------------------------------
1. UNC. `\\attacker\share\x.wav` is not a file, it is a network
   authentication. Windows resolves it by connecting to that host and offering
   the current user's credentials, and it happens during RESOLUTION - inside
   os.path.exists, before anything is opened or a byte is read. This is the
   only one that hands the attacker something they did not already have, and
   it is why the check has to run before any code that stats the value.
2. Device names. `\\.\pipe\x`, and the DOS reserved words - CON, NUL, COM1 -
   which are still devices WITH an extension on, so `CON.wav` is the console
   rather than a file. Reading one can block forever.
3. Alternate data streams. `x.wav:hidden` reads a different stream of the same
   file. A colon outside the drive letter has no other use here.
4. Reading a file that is simply none of the caller's business. This is the
   only one that costs a real capability to prevent, so it is the only one
   that is conditional - see `roots`.

Trust is the parameter, not the path
------------------------------------
There is no such thing as a safe path, only a path that is safe for a given
caller. Someone at the keyboard is allowed to transcribe a WAV on a NAS; a
browser client naming a path on the host machine is not. So callers declare
which they are, and `trusted=False` is the default on purpose: a call site
added later by someone who did not think about this gets the careful answer
rather than the convenient one.
"""

import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))

#: Extra folders an untrusted caller may read audio from, os.pathsep-separated.
#: Deliberately an environment variable and not a Field: policy that limits the
#: browser must not itself be reachable from the browser, and every settings
#: key is one patch away from being.
ROOTS_ENV = "LAT_AUDIO_ROOTS"

# Still devices in every directory, and still devices with a suffix attached.
_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM{0}".format(i) for i in range(1, 10)]
    + ["LPT{0}".format(i) for i in range(1, 10)])


class PathRefused(ValueError):
    """
    An untrusted caller named a path it may not have.

    The message is user-facing and says what to do instead: the person reading
    it in the browser panel is usually the owner of the machine, who has hit a
    rule meant for somebody else and needs to know which one and how to widen
    it.
    """


def audio_roots():
    """Folders an untrusted caller may read audio from. The checkout, plus $LAT_AUDIO_ROOTS."""
    roots = [APP_DIR]
    for part in (os.environ.get(ROOTS_ENV) or "").split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(os.path.abspath(part))
    return roots


def _is_network(path):
    r"""True for UNC (\\host\share), device (\\.\x) and extended (\\?\x) paths."""
    return path.replace("\\", "/").startswith("//")


def _within(path, root):
    """True when `path` is `root` or sits underneath it. Case-insensitive on Windows."""
    try:
        low, base = os.path.normcase(path), os.path.normcase(root)
        return os.path.commonpath([low, base]) == base
    except ValueError:
        # Different drives, or one of them relative. commonpath raises rather
        # than returning "" for that, and it means "not underneath".
        return False


def check_read_path(path, trusted=False, roots=None, what="WAV file"):
    r"""
    Return `path` as an absolute path this caller is allowed to open, or raise.

    `trusted` callers - the CLI, and the desktop panel, which is local by
    construction - get only the null-byte guard, because a rule that stopped
    the person at the keyboard reading their own NAS would be obeying a threat
    model that does not describe them.

    `roots`, when given, additionally confines the path to those folders. It is
    None for a listener on loopback: see Pipeline._audio_roots for why that
    judgement is made there and not here.

    Order matters as much as the rules do. Nothing here touches the filesystem
    until the network shape has been ruled out, because on Windows touching a
    UNC path IS the attack - a check that resolved the path first and judged it
    afterwards would have already done the thing it then refused.
    """
    text = "" if path is None else str(path).strip()
    if not text:
        raise PathRefused("No {0} was given.".format(what))
    if "\x00" in text:
        # Would otherwise surface as ValueError from deep inside open().
        raise PathRefused("That {0} name contains a null byte.".format(what))

    if trusted:
        return os.path.abspath(text)

    if _is_network(text):
        raise PathRefused(
            "'{0}' is a network or device path. Resolving one is a connection "
            "to another machine rather than a file read, so a {1} chosen "
            "remotely has to be a plain local path.".format(text, what))

    if ":" in os.path.splitdrive(text)[1]:
        # An alternate data stream, x.wav:hidden. Checked on all platforms
        # rather than under a Windows branch, so that the test proving it
        # proves it everywhere the suite runs.
        raise PathRefused(
            "'{0}' names an alternate data stream. Give a plain {1} path."
            .format(text, what))

    stem, ext = os.path.splitext(os.path.basename(text.replace("\\", "/")))
    if ext.lower() != ".wav":
        raise PathRefused(
            "'{0}' is not a .wav file. Both readers here want uncompressed "
            "16-bit PCM WAV, so this is the extension as well as the format."
            .format(text))
    if stem.strip(" .").upper() in _RESERVED:
        # Windows strips trailing dots and spaces before matching, so "CON ."
        # is CON. Match the way the filesystem matches, not the way it looks.
        raise PathRefused(
            "'{0}' is a reserved device name, not a file.".format(text))

    full = os.path.abspath(text)
    if _is_network(full):
        # A relative path resolved against a UNC working directory. Rare, and
        # exactly the case a check on the raw string alone would miss.
        raise PathRefused(
            "'{0}' resolves onto a network path ({1}).".format(text, full))

    # Only now, with a local .wav established, is it safe to ask the disk
    # anything - realpath opens the file to follow links.
    real = os.path.realpath(full)
    if _is_network(real):
        raise PathRefused(
            "'{0}' is a link to a network path ({1}).".format(text, real))

    if roots:
        # realpath the roots too, and for a reason that is invisible until it
        # bites: Windows still hands out 8.3 short names - %TEMP% is commonly
        # C:\Users\ICECRE~1\... and C:\PROGRA~1 is Program Files - while
        # realpath above has already expanded the FILE to its long form.
        # Comparing one against the other refuses every path under such a
        # root, which reads as "containment works" and is actually
        # containment failing shut on the owner of the machine.
        allowed = [os.path.realpath(r) for r in roots]
        if not any(_within(real, r) for r in allowed):
            raise PathRefused(
                "'{0}' is outside the folders this server may read audio "
                "from. Move the file under {1}, or name more folders in the "
                "{2} environment variable before starting."
                .format(real, allowed[0], ROOTS_ENV))
    return real
