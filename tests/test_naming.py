"""Offline tests for `imperial_doc_download.naming`.

The nasty inputs here aren't hypothetical: the titles are real ones from
this account's exercises (plan §9), and the traversal cases are what a
`Content-Disposition` filename would have to be stopped from doing.
"""

from __future__ import annotations

import pytest

from imperial_doc_download.naming import (
    exercise_directory,
    filename_from_disposition,
    guess_extension,
    safe_component,
)


class TestSafeComponent:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Data representation", "Data representation"),
            # Real titles from this account: `:` is illegal on Windows and
            # breaks macOS, `+` is fine and should survive untouched. The
            # ": " becomes a single "-" rather than "- ", because the run
            # collapse runs after the substitution.
            ("Task 1: Framework and Labts warm-up", "Task 1-Framework and Labts warm-up"),
            ("C Project Final Report+Source", "C Project Final Report+Source"),
            ("CW: Theory Coursework", "CW-Theory Coursework"),
        ],
    )
    def test_keeps_real_titles_readable(self, raw: str, expected: str) -> None:
        assert safe_component(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["../../.bashrc", "/etc/passwd", r"..\..\evil", "a/b/c", "\\\\server\\share"],
    )
    def test_never_emits_a_path_separator(self, raw: str) -> None:
        result = safe_component(raw)
        assert "/" not in result
        assert "\\" not in result

    def test_dots_only_becomes_the_fallback(self) -> None:
        # `.` and `..` already exist as directory entries, so writing to
        # either would write somewhere else entirely.
        assert safe_component("..") == "unnamed"
        assert safe_component(".") == "unnamed"
        assert safe_component("...", fallback="x") == "x"

    def test_empty_and_whitespace_become_the_fallback(self) -> None:
        assert safe_component("") == "unnamed"
        assert safe_component("   ") == "unnamed"
        assert safe_component(" . - _ ", fallback="fb") == "fb"

    def test_strips_control_characters(self) -> None:
        assert "\x00" not in safe_component("spec\x00.pdf")
        assert "\n" not in safe_component("spec\nname")

    def test_strips_trailing_dots_and_spaces(self) -> None:
        # Windows silently drops these, so a file written as "name." would
        # come back as "name" and never match on a later run.
        assert safe_component("report...") == "report"
        assert safe_component("report   ") == "report"

    def test_caps_the_length(self) -> None:
        assert len(safe_component("x" * 500)) == 120
        assert len(safe_component("x" * 500, max_length=10)) == 10

    @pytest.mark.parametrize("reserved", ["CON", "con", "NUL.pdf", "COM1", "LPT9.txt"])
    def test_windows_reserved_names_are_defused(self, reserved: str) -> None:
        assert safe_component(reserved).endswith("-file")

    def test_collapses_runs_left_by_substitution(self) -> None:
        assert safe_component("a///b") == "a-b"
        assert safe_component("a   b") == "a b"


class TestExerciseDirectory:
    def test_number_and_title(self) -> None:
        assert exercise_directory(1, "Data representation") == "1-Data representation"

    def test_missing_title_falls_back_to_the_number(self) -> None:
        assert exercise_directory(7, None) == "7"
        assert exercise_directory(7, "   ") == "7"

    def test_title_that_normalises_away_still_gives_a_directory(self) -> None:
        # "3-..." strips to "3", which is fine; a title of only separators
        # must not leave us with an empty or dot-only component.
        assert exercise_directory(3, "...") == "3"
        assert exercise_directory(3, "/") == "3"


class TestFilenameFromDisposition:
    def test_the_form_the_api_actually_sends(self) -> None:
        # Every one of the five download endpoints sends exactly this
        # (plan §9.1) -- note `inline`, not `attachment`.
        header = 'inline; filename="40001_1_spec.pdf"'
        assert filename_from_disposition(header) == "40001_1_spec.pdf"

    def test_attachment_works_too(self) -> None:
        assert filename_from_disposition('attachment; filename="a.pdf"') == "a.pdf"

    def test_unquoted_value(self) -> None:
        assert filename_from_disposition("inline; filename=a.pdf") == "a.pdf"

    def test_rfc5987_extended_value(self) -> None:
        header = "attachment; filename*=UTF-8''na%C3%AFve%20r%C3%A9sum%C3%A9.pdf"
        assert filename_from_disposition(header) == "naïve résumé.pdf"

    def test_extended_value_wins_over_the_plain_one(self) -> None:
        header = "attachment; filename=\"fallback.pdf\"; filename*=UTF-8''real.pdf"
        assert filename_from_disposition(header) == "real.pdf"

    @pytest.mark.parametrize(
        "header",
        [
            'attachment; filename="../../../etc/passwd"',
            'attachment; filename="/etc/passwd"',
            'attachment; filename="..\\\\..\\\\evil.exe"',
        ],
    )
    def test_traversal_attempts_are_reduced_to_a_bare_name(self, header: str) -> None:
        result = filename_from_disposition(header)
        assert result is not None
        assert "/" not in result
        assert "\\" not in result
        assert result not in ("..", ".")

    @pytest.mark.parametrize("header", [None, "", "inline", "attachment; foo=bar"])
    def test_no_filename_means_none(self, header: str | None) -> None:
        assert filename_from_disposition(header) is None

    def test_a_filename_that_sanitises_to_nothing_means_none(self) -> None:
        # Better to fall back to the caller's stem than to invent "unnamed".
        assert filename_from_disposition('attachment; filename=".."') is None


class TestGuessExtension:
    def test_recognises_a_pdf(self) -> None:
        assert guess_extension(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n") == ".pdf"

    def test_unrecognisable_content_is_bin(self) -> None:
        assert guess_extension(b"\x01\x02\x03\x04nonsense") == ".bin"

    def test_empty_input_is_bin(self) -> None:
        assert guess_extension(b"") == ".bin"

    def test_missing_file_command_is_bin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise FileNotFoundError

        monkeypatch.setattr("subprocess.run", boom)
        assert guess_extension(b"%PDF-1.4") == ".bin"
