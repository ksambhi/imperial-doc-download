from pathlib import Path

import pytest
from typer.testing import CliRunner

from imperial_doc_download.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # setup_logging() writes to ./logs/ relative to the cwd — run each test
    # from a scratch directory so we don't litter the repo with log files.
    monkeypatch.chdir(tmp_path)


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "imperial-doc-download" in result.stdout


@pytest.mark.parametrize(
    ("command", "step_name"),
    [("labts", "labts-fetch"), ("scientia", "scientia-fetch")],
)
def test_each_pipeline_is_its_own_subcommand(tmp_path: Path, command: str, step_name: str) -> None:
    output_dir = tmp_path / "data"
    result = runner.invoke(app, [command, "--dry-run", "--output-dir", str(output_dir)])

    assert result.exit_code == 0
    assert output_dir.is_dir()
    # --dry-run must never touch the network: the runner skips every step
    # rather than calling its run(), so only the "would run" log line shows.
    assert f"would run step: {step_name}" in result.output


def test_subcommands_are_listed_in_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "labts" in result.output
    assert "scientia" in result.output


def test_labts_takes_a_cache_dir_but_scientia_does_not() -> None:
    assert "--cache-dir" in runner.invoke(app, ["labts", "--help"]).output
    assert "--cache-dir" not in runner.invoke(app, ["scientia", "--help"]).output


def test_run_writes_a_log_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["labts", "--dry-run", "--output-dir", str(tmp_path / "data")])

    assert result.exit_code == 0
    log_files = list((tmp_path / "logs").glob("*.log"))
    assert len(log_files) == 1


def test_unimplemented_pipeline_reports_cleanly_without_a_traceback(tmp_path: Path) -> None:
    result = runner.invoke(app, ["scientia", "--output-dir", str(tmp_path / "data")])

    assert result.exit_code == 1
    assert "Not implemented yet" in result.output
    assert "Traceback" not in result.output
