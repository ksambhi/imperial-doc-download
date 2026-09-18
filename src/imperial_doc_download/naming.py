"""Pure helpers for turning remote strings into safe local filenames.

No I/O except the optional `file --extension` probe, so this is the part
of the download pipelines that can be tested exhaustively offline.

Everything here treats its input as hostile, because all of it is remote.
Exercise titles are free text written by lecturers (`Task 1: Framework
and Labts warm-up`, `C Project Final Report+Source`); zip members name
their own paths; and `Content-Disposition` filenames come straight off
the wire — a `filename="../../.bashrc"` must land inside the directory it
was meant for, not wherever it fancied.
"""

from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger(__name__)

#: Characters no filesystem we care about will take. `/` and `\` are path
#: separators; the rest are illegal on Windows (and `:` also breaks macOS).
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Collapse whatever the substitution left behind into single separators.
_RUNS = re.compile(r"[-_ ]{2,}")

#: Windows refuses these, with or without an extension, in any case.
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)

#: Long enough for any real title, short enough to leave room for the rest
#: of the path on a filesystem with a 255-byte component limit.
MAX_COMPONENT = 120

#: `Content-Disposition: inline; filename="40001_1_spec.pdf"`. The eMarking
#: API only ever sends this simple quoted form (plan §9.1), but RFC 6266's
#: `filename*=` is handled too rather than silently producing a wrong name.
_FILENAME_STAR = re.compile(r"filename\*\s*=\s*([^;]+)", re.I)
_FILENAME_QUOTED = re.compile(r'filename\s*=\s*"([^"]*)"', re.I)
_FILENAME_BARE = re.compile(r"filename\s*=\s*([^;\s]+)", re.I)


def safe_component(name: str, *, fallback: str = "unnamed", max_length: int = MAX_COMPONENT) -> str:
    """One path component, safe on Linux, macOS and Windows.

    Replaces separators and control characters, collapses the runs that
    leaves, strips the trailing dots and spaces Windows quietly eats, caps
    the length, and never returns an empty string — a title that
    normalises away to nothing would otherwise silently become the parent
    directory.
    """
    cleaned = _UNSAFE.sub("-", name)
    cleaned = _RUNS.sub(lambda m: m.group(0)[0], cleaned).strip(" .-_")
    cleaned = cleaned[:max_length].strip(" .-_")

    # `.` and `..` are directory entries that already exist, and writing to
    # either means writing somewhere else entirely.
    if not cleaned or set(cleaned) <= {"."}:
        return fallback

    # Windows reserves these by stem, so `CON.pdf` is refused too.
    stem, _, _ = cleaned.partition(".")
    if stem.upper() in _RESERVED:
        return f"{cleaned}-file"
    return cleaned


def exercise_directory(number: int | str, title: str | None) -> str:
    """`<number>-<title>`, the per-exercise output directory (plan §4)."""
    if not title or not title.strip():
        return safe_component(str(number), fallback=f"exercise-{number}")
    return safe_component(f"{number}-{title}", fallback=f"exercise-{number}")


def filename_from_disposition(header: str | None) -> str | None:
    """The filename out of a `Content-Disposition` header, or None.

    Accepts `inline` as well as `attachment` — the API sends `inline` for
    all five download endpoints (plan §9.1). The result is sanitised here
    rather than by the caller, because forgetting to would be a directory
    traversal, and this is the only place the header is ever parsed.
    """
    if not header:
        return None

    raw = _extended_value(header)
    if raw is None:
        match = _FILENAME_QUOTED.search(header) or _FILENAME_BARE.search(header)
        raw = match.group(1) if match else None
    if raw is None:
        return None

    # Some servers send a full path; only the last segment is a filename,
    # and on either separator, since the sender's OS is not ours.
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = safe_component(raw.strip(), fallback="")
    return cleaned or None


def _extended_value(header: str) -> str | None:
    """RFC 5987 `filename*=UTF-8''nam%C3%A9.pdf`, percent-decoding included."""
    match = _FILENAME_STAR.search(header)
    if not match:
        return None

    value = match.group(1).strip().strip('"')
    charset, _, remainder = value.partition("'")
    _language, _, encoded = remainder.partition("'")
    if not encoded:
        return None

    from urllib.parse import unquote

    return unquote(encoded, encoding=charset or "utf-8", errors="replace")


def guess_extension(path_or_bytes: bytes) -> str:
    """An extension for content whose name we never learned.

    Only reached when `Content-Disposition` is missing, which the live API
    never does (plan §9.1) — it exists so that a future response without
    one produces `feedback.pdf` rather than `feedback.bin`. Shelling out to
    `file` keeps this dependency-free; if it isn't installed or says
    nothing useful, `.bin` is an honest answer.
    """
    try:
        result = subprocess.run(
            ["file", "--brief", "--extension", "-"],
            input=path_or_bytes,
            capture_output=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ".bin"

    if result.returncode != 0:
        return ".bin"

    # `file` prints alternatives slash-separated ("jpeg/jpg/jpe"), and a
    # literal "???" when it has no idea.
    guess = result.stdout.decode("utf-8", "replace").strip().split("/")[0]
    if not guess or "?" in guess or not guess.isalnum():
        return ".bin"
    return f".{guess.lower()}"
