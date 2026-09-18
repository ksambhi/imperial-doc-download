"""Tests for `.env` parsing — the loader that must never touch a shell.

There is a real bug behind this file: `set -a; . ./.env; set +a` silently
truncated a password containing shell metacharacters (17 characters in
the file, 15 in the environment) and produced an auth failure that looked
exactly like an expired password.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from imperial_doc_download.config import Settings, load_env_file, parse_env_file


class TestParseEnvFile:
    def test_a_value_with_shell_metacharacters_survives_intact(self, tmp_path: Path) -> None:
        """The whole reason this function exists."""
        password = "a$b`c\\d!e&f(g)h"
        env = tmp_path / ".env"
        env.write_text(f"IMPERIAL_PASSWORD={password}\n")
        assert parse_env_file(env)["IMPERIAL_PASSWORD"] == password

    def test_a_value_containing_equals_keeps_all_of_it(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("KEY=a=b=c\n")
        assert parse_env_file(env)["KEY"] == "a=b=c"

    def test_comments_and_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("# a comment\n\n  \nKEY=value\n")
        assert parse_env_file(env) == {"KEY": "value"}

    def test_export_prefix_is_tolerated(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("export KEY=value\n")
        assert parse_env_file(env) == {"KEY": "value"}

    @pytest.mark.parametrize("quoted", ['"value"', "'value'"])
    def test_one_pair_of_surrounding_quotes_is_stripped(self, tmp_path: Path, quoted) -> None:
        env = tmp_path / ".env"
        env.write_text(f"KEY={quoted}\n")
        assert parse_env_file(env)["KEY"] == "value"

    def test_inner_quotes_are_left_alone(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text('KEY=va"lu"e\n')
        assert parse_env_file(env)["KEY"] == 'va"lu"e'

    def test_no_interpolation(self, tmp_path: Path) -> None:
        # A literal $HOME is a literal $HOME.
        env = tmp_path / ".env"
        env.write_text("KEY=$HOME/x\n")
        assert parse_env_file(env)["KEY"] == "$HOME/x"

    def test_a_line_without_an_equals_is_ignored(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("nonsense\nKEY=value\n")
        assert parse_env_file(env) == {"KEY": "value"}


class TestLoadEnvFile:
    def test_sets_variables_and_reports_their_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = tmp_path / ".env"
        env.write_text("IMPERIAL_USERNAME=jbloggs\nIMPERIAL_PASSWORD=hunter2\n")
        monkeypatch.delenv("IMPERIAL_USERNAME", raising=False)

        loaded = load_env_file(env)

        assert set(loaded) == {"IMPERIAL_USERNAME", "IMPERIAL_PASSWORD"}
        assert os.environ["IMPERIAL_USERNAME"] == "jbloggs"
        assert Settings.from_env().password == "hunter2"

    def test_the_real_environment_always_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An explicit export shouldn't be overridden by whatever file
        # happens to be in the working directory.
        monkeypatch.setenv("IMPERIAL_USERNAME", "from-the-shell")
        env = tmp_path / ".env"
        env.write_text("IMPERIAL_USERNAME=from-the-file\n")

        assert load_env_file(env) == []
        assert os.environ["IMPERIAL_USERNAME"] == "from-the-shell"

    def test_a_missing_file_is_fine(self, tmp_path: Path) -> None:
        assert load_env_file(tmp_path / "nope") == []

    def test_values_are_never_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        monkeypatch.delenv("IMPERIAL_PASSWORD", raising=False)
        env = tmp_path / ".env"
        env.write_text("IMPERIAL_PASSWORD=hunter2\n")

        with caplog.at_level("DEBUG"):
            load_env_file(env)

        assert "hunter2" not in caplog.text
        assert "IMPERIAL_PASSWORD" in caplog.text
