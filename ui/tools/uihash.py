r"""
One digest over the sources that compile into ui/runtime.

ui/runtime is committed deliberately - it is how a clone runs the panel with no
.NET SDK installed - and that is exactly what makes a stale one invisible. Edit
a .cs, forget build_ui.cmd, and the whole suite still passes: every panel test
loads the shipped assembly, so it is testing the previous build and has no way
to tell. The build is deterministic and path-independent, so a hash of its
inputs is enough to notice.

build_ui.cmd writes the digest here after a successful publish, and
tests/test_ui_stamp.py fails when it disagrees with the working tree.

What is hashed is the compile input and nothing else: .cs, .xaml and .csproj
under ui/, minus runtime/, bin/, obj/ and .vs/. obj/ is the one that matters -
WPF regenerates .g.cs and GeneratedInternalTypeHelper.cs in there on every
build, so hashing it would tie the digest to whether the tree had been built
rather than to what is in it. ui/tools/*.py falls out by suffix: those drive
the panel, they do not compile into it.

global.json is out of scope on purpose. An SDK-band change does rewrite the
committed deps.json and runtimeconfig.json - but those are committed too, so
it lands in a diff and is not the invisible case this guards.

Content is newline-normalized and keyed by repo-relative POSIX path, so the
digest is the same on a fresh clone whatever core.autocrlf did, and wherever
the folder was put.

    .venv\Scripts\python.exe ui\tools\uihash.py            report, 1 if stale
    .venv\Scripts\python.exe ui\tools\uihash.py --write    stamp this tree
"""
import hashlib
import os

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(UI_DIR)
RUNTIME_DIR = os.path.join(UI_DIR, "runtime")
STAMP_FILE = os.path.join(RUNTIME_DIR, "sources.sha256")

#: What the csproj hands the compiler. Widening this without widening the
#: project file would put files in the digest that no rebuild can settle.
SOURCE_SUFFIXES = (".cs", ".xaml", ".csproj")

#: Pruned whole, see the module docstring on obj/.
SKIP_DIRS = frozenset(("runtime", "bin", "obj", ".vs"))

_HEADER = (
    "# sha256 over ui/ *.cs, *.xaml and *.csproj - written by build_ui.cmd\n"
    "# after a successful publish. tests/test_ui_stamp.py fails when this\n"
    "# disagrees with the working tree, which means ui/runtime is stale.\n"
)


def source_files():
    """The compile inputs, as repo-relative POSIX paths, sorted.

    Sorted and POSIX because two machines have to agree: os.walk order is
    filesystem order, and a backslash would pin the digest to one OS.
    """
    found = []
    for root, dirs, names in os.walk(UI_DIR):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in names:
            if name.endswith(SOURCE_SUFFIXES):
                rel = os.path.relpath(os.path.join(root, name), REPO)
                found.append(rel.replace(os.sep, "/"))
    return sorted(found)


def source_hash():
    """One sha256 over every compile input - path and content both."""
    digest = hashlib.sha256()
    for rel in source_files():
        with open(os.path.join(REPO, rel), "rb") as fh:
            body = fh.read()
        # CRLF -> LF before hashing. .gitattributes normalizes to LF in the
        # repo and checks CRLF back out here, so a clone with core.autocrlf
        # off holds byte-different files that compile to the same assembly.
        # Hashing them raw would fail this gate on a fresh clone, for a
        # mistake nobody made.
        body = body.replace(b"\r\n", b"\n")
        # Length-prefixed so a rename cannot be spelled as a content edit:
        # without it, path "a" with body "b" and path "ab" with an empty body
        # produce the same digest.
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(body)).encode("ascii"))
        digest.update(b"\0")
        digest.update(body)
    return digest.hexdigest()


def read_stamp():
    """The digest build_ui.cmd last wrote, or None if there is no stamp."""
    try:
        with open(STAMP_FILE, "r") as fh:
            lines = fh.read().splitlines()
    except (IOError, OSError):
        return None
    for line in reversed(lines):
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def write_stamp():
    """Record the current sources next to the assembly they just built."""
    value = source_hash()
    with open(STAMP_FILE, "w") as fh:
        fh.write(_HEADER)
        fh.write(value + "\n")
    return value


def is_current():
    """Does the committed ui/runtime correspond to the sources sitting here?"""
    return read_stamp() == source_hash()


def _main(argv):
    if "--write" in argv:
        print("ui sources: {0}".format(write_stamp()))
        return 0
    current = source_hash()
    stamp = read_stamp()
    print("sources : {0}  ({1} files)".format(current, len(source_files())))
    print("stamp   : {0}".format(stamp or "(none - run build_ui.cmd)"))
    if stamp == current:
        return 0
    print("")
    print("ui/runtime is STALE - the committed assembly was built from")
    print("different sources. Run build_ui.cmd to rebuild and restamp.")
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
