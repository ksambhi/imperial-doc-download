"""Extracting a zip that came off the network, carefully.

Everything else in this tool downloads bytes into a path *we* chose. A
zip is different: it names its own output paths, and those names were
written by another system. `extractall()` on untrusted input is the
classic way to end up writing files somewhere you didn't intend, so this
module plans the extraction first and refuses archives it doesn't like,
rather than discovering the problem halfway through writing one.

The checks, in the order they matter (`docs/materials-fetch-plan.md` §6):

1. every member's resolved destination stays inside the target directory
2. symlinks and other non-regular members are skipped, not written
3. total uncompressed size and member count are capped
4. every path component is made safe for the filesystem
5. extraction happens in a temp directory and is moved into place

CPython's `ZipFile.extract` does strip `..` and leading separators, so
(1) is belt-and-braces — but a security property that holds only because
of an implementation detail of the standard library is one that quietly
stops holding. It's checked here explicitly.
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from imperial_doc_download.naming import safe_component

logger = logging.getLogger(__name__)

#: The largest real archive is 189 MB; 2 GB leaves an order of magnitude
#: of headroom and still stops something pathological before it fills a
#: disk. Checked against the *declared* sizes, before anything is written.
MAX_UNCOMPRESSED_BYTES = 2 * 1024**3

#: Likewise: real archives hold a few hundred files.
MAX_MEMBERS = 50_000

#: `st_mode >> 16` of a zip entry, masked to the file-type bits.
_S_IFMT = 0o170000
_S_IFLNK = 0o120000
_S_IFREG = 0o100000
_S_IFDIR = 0o040000


class UnsafeArchiveError(RuntimeError):
    """The archive asked for something we won't do.

    Always names the member responsible: "this zip is bad" is not an
    actionable message when the zip is 189 MB of someone's lectures.
    """


@dataclass
class ExtractionPlan:
    """What extracting this archive would do, decided before it does it."""

    #: member name → path relative to the target directory.
    members: dict[str, PurePosixPath] = field(default_factory=dict)
    #: Members deliberately not written, and why.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    total_bytes: int = 0
    #: The single top-level directory shared by every member, if there is
    #: one — `50007.2/` for a materials zip. See `plan()`.
    common_prefix: str | None = None

    @property
    def file_count(self) -> int:
        return len(self.members)


def plan(archive: zipfile.ZipFile, *, strip_common_prefix: bool = True) -> ExtractionPlan:
    """Decide where every member would go, or raise if any of them can't.

    Nothing is written. Raising here — rather than partway through an
    extraction — is what keeps a bad archive from leaving a half-tree
    behind.

    Every member of a materials zip is prefixed with its module code
    (`50007.2/Written Notes/…`), which would nest the code twice under
    `<year>/<module>/materials/`. When *every* member shares one
    top-level directory that prefix is dropped; when they don't, the
    archive is extracted as-is. That rule can't merge two trees together,
    which is why it's safe to apply automatically.
    """
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise UnsafeArchiveError(
            f"Archive declares {len(infos)} members, more than the {MAX_MEMBERS} limit."
        )

    result = ExtractionPlan()
    prefix = _common_prefix(infos) if strip_common_prefix else None
    result.common_prefix = prefix

    for info in infos:
        kind = (info.external_attr >> 16) & _S_IFMT
        if info.is_dir():
            continue
        if kind == _S_IFLNK:
            # zipfile would write the link *target* as file content rather
            # than creating a link, which is safe by accident. Skipping
            # makes it safe on purpose.
            result.skipped.append((info.filename, "symlink"))
            continue
        if kind not in (0, _S_IFREG, _S_IFDIR):
            result.skipped.append((info.filename, f"not a regular file (mode {kind:o})"))
            continue

        relative = _safe_relative_path(info.filename, prefix)
        if relative is None:
            result.skipped.append((info.filename, "empty path after sanitising"))
            continue

        result.total_bytes += info.file_size
        if result.total_bytes > MAX_UNCOMPRESSED_BYTES:
            raise UnsafeArchiveError(
                f"Archive expands to more than {MAX_UNCOMPRESSED_BYTES} bytes "
                f"(reached {result.total_bytes} at {info.filename!r})."
            )
        result.members[info.filename] = relative

    return result


def extract(
    zip_path: Path,
    target: Path,
    *,
    strip_common_prefix: bool = True,
) -> ExtractionPlan:
    """Extract `zip_path` into `target`, atomically.

    Written into a sibling temp directory and moved into place, so an
    interrupted run never leaves a half-tree that a later run mistakes
    for a finished one. Any existing tree is replaced.
    """
    with zipfile.ZipFile(zip_path) as archive:
        extraction = plan(archive, strip_common_prefix=strip_common_prefix)

        staging = target.parent / f".{target.name}.extracting"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            for name, relative in extraction.members.items():
                destination = staging / relative
                _assert_inside(staging, destination, name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, destination.open("wb") as handle:
                    shutil.copyfileobj(source, handle)

            if target.exists():
                shutil.rmtree(target)
            staging.replace(target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    for name, reason in extraction.skipped:
        logger.warning("Skipped %s from %s: %s", name, zip_path.name, reason)
    return extraction


def _common_prefix(infos: list[zipfile.ZipInfo]) -> str | None:
    """The one top-level directory every member sits under, if there is one."""
    tops = set()
    for info in infos:
        parts = PurePosixPath(info.filename).parts
        if not parts:
            continue
        if len(parts) == 1 and not info.is_dir():
            return None  # a file at the root, so there's no single prefix
        tops.add(parts[0])
        if len(tops) > 1:
            return None
    return next(iter(tops)) if len(tops) == 1 else None


def _safe_relative_path(name: str, prefix: str | None) -> PurePosixPath | None:
    """A member name as a relative path that's safe on this filesystem.

    Sanitises **per component** — `safe_component` replaces `/`, so
    running it over the whole path would flatten the directory structure
    into one long filename.
    """
    # Windows-created archives can use backslashes as separators.
    parts = PurePosixPath(name.replace("\\", "/")).parts

    cleaned: list[str] = []
    for index, part in enumerate(parts):
        if part in ("", ".", "/"):
            continue
        if part == "..":
            # Never resolved relative to what came before: a member is not
            # allowed to climb, so the component is simply dropped and
            # `_assert_inside` catches anything this missed.
            continue
        if index == 0 and prefix is not None and part == prefix:
            continue
        cleaned.append(safe_component(part, fallback="unnamed"))

    return PurePosixPath(*cleaned) if cleaned else None


def _assert_inside(root: Path, candidate: Path, member: str) -> None:
    """Last line of defence: the resolved path must be under `root`.

    `_safe_relative_path` should already make this impossible. It is
    checked anyway because the cost is a syscall and the failure mode is
    writing over something outside the output directory.
    """
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise UnsafeArchiveError(
            f"Member {member!r} would be written to {resolved}, outside {resolved_root}."
        )
