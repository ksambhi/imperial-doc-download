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
    # (IMPERIAL_* is cleared for every test in conftest.py.)
    monkeypatch.chdir(tmp_path)


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


class TestAllCommand:
    """The one-invocation command that runs every pipeline."""

    def test_dry_run_lists_all_four_steps_in_order(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["all", "--output-dir", str(tmp_path), "--dry-run"])
        assert result.exit_code == 0, result.output
        output = _output(result)
        for name in ("labts-fetch", "gitlab-fetch", "emarking-fetch", "emarking-marks"):
            assert name in output
        assert output.index("labts-fetch") < output.index("gitlab-fetch")
        assert output.index("emarking-fetch") < output.index("emarking-marks")

    def test_it_is_listed_in_the_help(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "all" in _output(result)

    def test_a_failing_step_does_not_stop_the_others(self, tmp_path: Path) -> None:
        """Without credentials every step fails — but all four are tried."""
        result = runner.invoke(app, ["all", "--output-dir", str(tmp_path)])

        output = _output(result)
        assert result.exit_code == 1
        assert "step(s) failed" in output
        # All four reported, not just the first.
        for name in ("labts-fetch", "gitlab-fetch", "emarking-fetch", "emarking-marks"):
            assert name in output
        assert "Re-run to retry just the failures." in output

    def test_reads_credentials_from_an_env_file(self, tmp_path: Path) -> None:
        env = tmp_path / "creds.env"
        # A password full of shell metacharacters, which is the case that
        # sourcing the file would mangle.
        env.write_text("IMPERIAL_USERNAME=jbloggs\nIMPERIAL_PASSWORD=a$b`c!d&e\n")

        result = runner.invoke(
            app,
            ["--env-file", str(env), "all", "--output-dir", str(tmp_path), "--dry-run"],
        )
        assert result.exit_code == 0, result.output

    def test_a_missing_env_file_is_not_an_error(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "--env-file",
                str(tmp_path / "nope"),
                "all",
                "--output-dir",
                str(tmp_path),
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
