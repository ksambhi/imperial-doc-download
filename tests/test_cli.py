from pathlib import Path

from typer.testing import CliRunner

from imperial_doc_download.cli import app

runner = CliRunner()


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
