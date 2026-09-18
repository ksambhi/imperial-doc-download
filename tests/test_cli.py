import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from imperial_doc_download.cli import app

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _output(result: Result) -> str:
    """A run's output with the styling stripped off.

    Worth doing rather than reading `result.output` directly: typer
    highlights option names a character at a time, so with colour on
    `--force` is emitted as `-` and `-force` in separate escape
    sequences and the plain substring is nowhere in the output. Whether
    colour is on depends on the environment (CI forces it on, an
    ordinary terminal doesn't), so an unstripped `in` check passes
    locally and fails in CI.
    """
    return _ANSI.sub("", result.output)


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # setup_logging() writes to ./logs/ relative to the cwd — run each test
    # from a scratch directory so we don't litter the repo with log files.
    monkeypatch.chdir(tmp_path)

    # Typer reads credentials and key paths straight from the environment,
    # so whatever the developer running the tests has exported must not
    # leak in and change what these assert.
    for name, _ in list(os.environ.items()):
        if name.startswith("IMPERIAL_"):
            monkeypatch.delenv(name)


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "imperial-doc-download" in _output(result)


@pytest.mark.parametrize(
    ("command", "step_name"),
    [
        ("labts", "labts-fetch"),
        ("gitlab", "gitlab-fetch"),
        ("scientia", "scientia-fetch"),
    ],
)
def test_each_pipeline_is_its_own_subcommand(tmp_path: Path, command: str, step_name: str) -> None:
    output_dir = tmp_path / "data"
    result = runner.invoke(app, [command, "--dry-run", "--output-dir", str(output_dir)])

    assert result.exit_code == 0
    assert output_dir.is_dir()
    # --dry-run must never touch the network: the runner skips every step
    # rather than calling its run(), so only the "would run" log line shows.
    assert f"would run step: {step_name}" in _output(result)


def test_labts_fetches_the_list_and_then_clones(tmp_path: Path) -> None:
    result = runner.invoke(app, ["labts", "--dry-run", "--output-dir", str(tmp_path / "data")])

    assert result.exit_code == 0
    output = _output(result)
    fetch = output.index("would run step: labts-fetch")
    clone = output.index("would run step: gitlab-fetch")
    assert fetch < clone, "the clone step must run after the list it clones from"


def test_the_clone_options_are_on_both_clone_commands() -> None:
    for command in ("labts", "gitlab"):
        output = _output(runner.invoke(app, [command, "--help"]))
        for option in ("--force", "--concurrency", "--clone-timeout", "--gitlab-ssh-key"):
            assert option in output, f"{option} missing from `{command} --help`"


def test_ssh_settings_can_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMPERIAL_GITLAB_SSH_KEY", "/keys/gitlab")
    output_dir = tmp_path / "data"
    output_dir.mkdir()

    result = runner.invoke(app, ["gitlab", "--output-dir", str(output_dir)])

    # Got past the "no key configured" check, and stopped at the next
    # thing that's missing — which proves the env var was picked up.
    assert result.exit_code == 1
    assert "Run the labts step first" in _output(result)


def test_a_missing_ssh_key_is_reported_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(app, ["gitlab", "--output-dir", str(tmp_path / "data")])

    assert result.exit_code == 1
    assert "IMPERIAL_GITLAB_SSH_KEY" in _output(result)
    assert "Traceback" not in _output(result)


def test_subcommands_are_listed_in_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    output = _output(result)
    assert "labts" in output
    assert "scientia" in output


def test_labts_takes_a_cache_dir_but_scientia_does_not() -> None:
    assert "--cache-dir" in _output(runner.invoke(app, ["labts", "--help"]))
    assert "--cache-dir" not in _output(runner.invoke(app, ["scientia", "--help"]))


def test_run_writes_a_log_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["labts", "--dry-run", "--output-dir", str(tmp_path / "data")])

    assert result.exit_code == 0
    log_files = list((tmp_path / "logs").glob("*.log"))
    assert len(log_files) == 1


def test_unimplemented_pipeline_reports_cleanly_without_a_traceback(tmp_path: Path) -> None:
    result = runner.invoke(app, ["scientia", "--output-dir", str(tmp_path / "data")])

    assert result.exit_code == 1
    output = _output(result)
    assert "Not implemented yet" in output
    assert "Traceback" not in output
