"""Offline tests for `materials_fetch.archive`.

Every archive here is built for the test, because the point is the ones a
server could send us and a real zip wouldn't: a member escaping the
target directory, a symlink, a bomb. These are the cases that decide
whether this tool can only write inside its own output directory.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from imperial_doc_download.materials_fetch.archive import (
    MAX_MEMBERS,
    UnsafeArchiveError,
    extract,
    plan,
)


def _zip(path: Path, members: dict[str, bytes], *, compress: bool = False) -> Path:
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(path, "w", compression=mode) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _zip_with_symlink(path: Path, link_name: str, target: str) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo(link_name)
        # 0o120000 is S_IFLNK; the low bits are the permissions.
        info.external_attr = (0o120777 << 16) | 0o600
        archive.writestr(info, target)
        archive.writestr("real.txt", b"fine")
    return path


#: What a real materials zip looks like (verified against 2324/50007.2).
REALISTIC = {
    "50007.2/Written Notes/(0) Prolog Course Notes.pdf": b"%PDF-1.4 notes",
    "50007.2/links.md": b"# links\n",
}


class TestRealArchives:
    def test_extracts_a_realistic_zip(self, tmp_path: Path) -> None:
        archive = _zip(tmp_path / "m.zip", REALISTIC)
        result = extract(archive, tmp_path / "materials")

        assert result.file_count == 2
        assert (tmp_path / "materials/Written Notes/(0) Prolog Course Notes.pdf").is_file()
        assert (tmp_path / "materials/links.md").read_bytes() == b"# links\n"

    def test_the_module_code_prefix_is_stripped(self, tmp_path: Path) -> None:
        # Every member is prefixed with the module code, which would
        # otherwise nest it twice under <year>/<module>/materials/.
        archive = _zip(tmp_path / "m.zip", REALISTIC)
        result = extract(archive, tmp_path / "materials")

        assert result.common_prefix == "50007.2"
        assert not (tmp_path / "materials/50007.2").exists()

    def test_the_prefix_is_kept_when_members_do_not_share_one(self, tmp_path: Path) -> None:
        # Stripping here would merge two trees, so it must not happen.
        archive = _zip(tmp_path / "m.zip", {"a/one.txt": b"1", "b/two.txt": b"2"})
        result = extract(archive, tmp_path / "materials")

        assert result.common_prefix is None
        assert (tmp_path / "materials/a/one.txt").is_file()
        assert (tmp_path / "materials/b/two.txt").is_file()

    def test_a_root_level_file_prevents_stripping(self, tmp_path: Path) -> None:
        archive = _zip(tmp_path / "m.zip", {"README": b"x", "50007.2/a.pdf": b"y"})
        result = extract(archive, tmp_path / "materials")

        assert result.common_prefix is None
        assert (tmp_path / "materials/README").is_file()

    def test_a_links_only_zip_is_ordinary(self, tmp_path: Path) -> None:
        # Five of the real modules publish nothing but a links.md. That's
        # a module whose materials are all external, not an error.
        archive = _zip(tmp_path / "m.zip", {"40007/links.md": b"# links\n"})
        result = extract(archive, tmp_path / "materials")

        assert result.file_count == 1
        assert (tmp_path / "materials/links.md").is_file()

    def test_disabling_the_strip_keeps_the_literal_layout(self, tmp_path: Path) -> None:
        archive = _zip(tmp_path / "m.zip", REALISTIC)
        extract(archive, tmp_path / "materials", strip_common_prefix=False)
        assert (tmp_path / "materials/50007.2/links.md").is_file()


class TestPathEscapes:
    """Nothing may be written outside the target directory."""

    @pytest.mark.parametrize(
        "name",
        [
            "../escaped.txt",
            "../../escaped.txt",
            "a/../../escaped.txt",
            "50007.2/../../escaped.txt",
        ],
    )
    def test_parent_traversal_never_escapes(self, tmp_path: Path, name: str) -> None:
        target = tmp_path / "out" / "materials"
        archive = _zip(tmp_path / "m.zip", {name: b"pwned", "ok/fine.txt": b"fine"})

        extract(archive, target)

        assert not (tmp_path / "escaped.txt").exists()
        assert not (tmp_path / "out/escaped.txt").exists()
        # Whatever survived is inside the target.
        for path in target.rglob("*"):
            assert target.resolve() in path.resolve().parents

    def test_an_absolute_path_is_made_relative(self, tmp_path: Path) -> None:
        target = tmp_path / "materials"
        _zip(tmp_path / "m.zip", {"/etc/passwd": b"root:x:0:0"})

        extract(tmp_path / "m.zip", target)

        assert (target / "etc/passwd").is_file()
        for path in target.rglob("*"):
            assert target.resolve() in path.resolve().parents

    def test_a_windows_separator_is_treated_as_a_separator(self, tmp_path: Path) -> None:
        target = tmp_path / "materials"
        _zip(tmp_path / "m.zip", {"notes\\week1\\slides.pdf": b"x"})

        extract(tmp_path / "m.zip", target)

        assert (target / "notes/week1/slides.pdf").is_file()

    def test_directory_structure_is_preserved_not_flattened(self, tmp_path: Path) -> None:
        # safe_component replaces "/", so sanitising the whole path
        # instead of each component would collapse the tree. Two
        # top-level entries, so the prefix strip stays out of it.
        target = tmp_path / "materials"
        _zip(tmp_path / "m.zip", {"x/a/b/c/deep.pdf": b"x", "y/other.txt": b"y"})

        extract(tmp_path / "m.zip", target)

        assert (target / "x/a/b/c/deep.pdf").is_file()


class TestSymlinks:
    def test_a_symlink_member_is_skipped_not_written(self, tmp_path: Path) -> None:
        archive = _zip_with_symlink(tmp_path / "m.zip", "evil-link", "/etc/passwd")
        target = tmp_path / "materials"

        result = extract(archive, target)

        assert not (target / "evil-link").exists()
        assert [name for name, _ in result.skipped] == ["evil-link"]
        assert result.skipped[0][1] == "symlink"
        # The rest of the archive still extracts.
        assert (target / "real.txt").is_file()


class TestLimits:
    def test_too_many_members_is_refused(self, tmp_path: Path) -> None:
        archive = tmp_path / "m.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            for index in range(3):
                handle.writestr(f"f{index}", b"x")

        with zipfile.ZipFile(archive) as handle:
            # Cheaper than writing 50,001 real members.
            handle.infolist().extend(handle.infolist() * MAX_MEMBERS)
            with pytest.raises(UnsafeArchiveError, match="more than"):
                plan(handle)

    def test_a_declared_expansion_over_the_cap_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "imperial_doc_download.materials_fetch.archive.MAX_UNCOMPRESSED_BYTES", 10
        )
        archive = _zip(tmp_path / "m.zip", {"big.bin": b"x" * 100})
        target = tmp_path / "materials"

        with pytest.raises(UnsafeArchiveError, match="expands to more than"):
            extract(archive, target)

        # Refused before writing, so nothing is left behind.
        assert not target.exists()

    def test_the_limit_is_checked_from_declared_sizes_before_writing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A zip bomb is small on disk and huge when expanded; the check
        # has to read file_size, not the compressed size.
        monkeypatch.setattr(
            "imperial_doc_download.materials_fetch.archive.MAX_UNCOMPRESSED_BYTES", 1000
        )
        archive = _zip(tmp_path / "m.zip", {"bomb": b"\0" * 100_000}, compress=True)
        assert archive.stat().st_size < 1000  # compresses to almost nothing

        with pytest.raises(UnsafeArchiveError):
            extract(archive, tmp_path / "materials")


class TestAtomicity:
    def test_an_existing_tree_is_replaced_not_merged(self, tmp_path: Path) -> None:
        target = tmp_path / "materials"
        target.mkdir()
        (target / "stale.txt").write_text("from an older run")

        extract(_zip(tmp_path / "m.zip", REALISTIC), target)

        assert not (target / "stale.txt").exists()
        assert (target / "links.md").is_file()

    def test_a_refused_archive_leaves_no_staging_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "materials"
        _zip(tmp_path / "m.zip", {"a.txt": b"x"})

        with pytest.raises(UnsafeArchiveError):
            with zipfile.ZipFile(tmp_path / "m.zip") as handle:
                handle.infolist().extend(handle.infolist() * MAX_MEMBERS)
                plan(handle)

        assert list(tmp_path.glob(".*extracting")) == []
        assert not target.exists()

    def test_a_failure_midway_leaves_the_previous_tree_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "materials"
        target.mkdir()
        (target / "previous.txt").write_text("still here")

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        archive = _zip(tmp_path / "m.zip", REALISTIC)
        monkeypatch.setattr("shutil.copyfileobj", boom)

        with pytest.raises(OSError, match="disk full"):
            extract(archive, target)

        assert (target / "previous.txt").read_text() == "still here"
        assert list(tmp_path.glob(".*extracting")) == []


class TestPlan:
    def test_reports_totals_without_writing_anything(self, tmp_path: Path) -> None:
        archive = _zip(tmp_path / "m.zip", REALISTIC)
        with zipfile.ZipFile(archive) as handle:
            result = plan(handle)

        assert result.file_count == 2
        assert result.total_bytes == sum(len(v) for v in REALISTIC.values())
        assert list(tmp_path.iterdir()) == [archive]

    def test_directory_entries_are_not_counted_as_files(self, tmp_path: Path) -> None:
        archive = tmp_path / "m.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("dir/", b"")
            handle.writestr("dir/file.txt", b"x")

        with zipfile.ZipFile(archive) as handle:
            assert plan(handle).file_count == 1
