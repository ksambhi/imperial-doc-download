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


def test_run_dry_run_creates_output_dir(tmp_path: Path) -> None:
    output_dir = tmp_path / "data"
    result = runner.invoke(app, ["run", "--dry-run", "--output-dir", str(output_dir)])

    assert result.exit_code == 0
    assert output_dir.is_dir()
    assert "No pipeline steps registered yet" in result.stdout


def test_run_writes_a_log_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "--dry-run", "--output-dir", str(tmp_path / "data")])

    assert result.exit_code == 0
    log_files = list((tmp_path / "logs").glob("*.log"))
    assert len(log_files) == 1
