"""Typer entry point for imperial-doc-download.

One subcommand per Imperial system, each running its own pipeline:

    imperial-doc-download labts     [options]
    imperial-doc-download scientia  [options]

Eventually the root command will run all of them in parallel; that only
makes sense once each pipeline works on its own, so it isn't wired up yet.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from imperial_doc_download import __version__
from imperial_doc_download.config import Settings
from imperial_doc_download.emarking_fetch import EmarkingFetchStep
from imperial_doc_download.gitlab_fetch import GitlabFetchStep
from imperial_doc_download.gitlab_fetch.cloning import DEFAULT_CLONE_TIMEOUT
from imperial_doc_download.labts_fetch import LabtsFetchStep
from imperial_doc_download.logging import setup_logging
from imperial_doc_download.pipeline import Pipeline, PipelineContext, Step
from imperial_doc_download.scientia_fetch import ScientiaFetchStep

app = typer.Typer(
    name="imperial-doc-download",
    help="Back up personal data from Imperial College Department of Computing systems.",
    no_args_is_help=True,
)

# Options shared by every pipeline subcommand. Defined once so the
# subcommands stay consistent as more systems are added.
_OUTPUT_DIR_OPTION = typer.Option(
    Path("./imperial-data"),
    "--output-dir",
    "-o",
    help="Directory to download data into.",
)
_DRY_RUN_OPTION = typer.Option(
    False,
    "--dry-run",
    help="Print the steps that would run without downloading anything.",
)
_FORCE_OPTION = typer.Option(
    False,
    "--force",
    help=(
        "Ignore anything a previous run already produced and redo it. "
        "Without this, an existing repository list is reused and an "
        "already-cloned repository is left alone."
    ),
)

# Options for the clone stage. Every one of these is readable from the
# environment too (that's how the credentials are meant to be supplied) —
# except the key passphrases, which are environment-only on purpose so a
# secret can't leak into shell history or `ps` output. See `config.py`.
_USERNAME_OPTION = typer.Option(
    None,
    "--username",
    envvar="IMPERIAL_USERNAME",
    help="DoC username (e.g. abc123), for LabTS and the shell-server jump.",
)
_DOC_SSH_KEY_OPTION = typer.Option(
    None,
    "--doc-ssh-key",
    envvar="IMPERIAL_DOC_SSH_KEY",
    help="Private key for the DoC shell servers, used to reach gitolite.",
)
_GITLAB_SSH_KEY_OPTION = typer.Option(
    None,
    "--gitlab-ssh-key",
    envvar="IMPERIAL_GITLAB_SSH_KEY",
    help="Private key registered with DoC GitLab (and gitolite behind it).",
)
_JUMP_HOST_OPTION = typer.Option(
    None,
    "--jump-host",
    envvar="IMPERIAL_JUMP_HOST",
    help=(
        "Shell server to proxy-jump through for gitolite. "
        "Defaults to one of shell1-5.doc.ic.ac.uk, picked at random per run."
    ),
)
_CONCURRENCY_OPTION = typer.Option(
    1,
    "--concurrency",
    "-j",
    min=1,
    help="How many repositories to clone at once. 1 (the default) is sequential.",
)
_CLONE_TIMEOUT_OPTION = typer.Option(
    DEFAULT_CLONE_TIMEOUT,
    "--clone-timeout",
    min=1.0,
    help="Seconds to let a single git command run before giving up on it.",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"imperial-doc-download {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """imperial-doc-download: grab your data before your account gets nuked."""
    log_file = setup_logging(verbose=verbose)
    logging.getLogger(__name__).debug("Logging to %s", log_file)


def _run_pipeline(
    steps: list[Step],
    output_dir: Path,
    dry_run: bool,
    settings: Settings | None = None,
) -> None:
    """Run one system's pipeline against a freshly built context."""
    ctx = PipelineContext(
        output_dir=output_dir,
        settings=settings or Settings.from_env(),
        dry_run=dry_run,
    )
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        Pipeline(steps=steps).run(ctx)
    except NotImplementedError as exc:
        # A pipeline that's still a placeholder should say so plainly
        # rather than dumping a traceback at whoever ran it.
        typer.secho(f"Not implemented yet: {exc}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(1) from exc
    except RuntimeError as exc:
        # Steps raise RuntimeError for "you haven't given me what I need"
        # (missing credentials, missing key, no repository list yet). That's
        # a user-fixable problem, so say what's wrong, not where it raised.
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


_CACHE_DIR_OPTION = typer.Option(
    None,
    "--cache-dir",
    help=(
        "Cache fetched pages here and reuse them on later runs. "
        "Makes re-runs near-instant; delete the directory to refetch."
    ),
)


@app.command()
def labts(
    output_dir: Path = _OUTPUT_DIR_OPTION,
    dry_run: bool = _DRY_RUN_OPTION,
    force: bool = _FORCE_OPTION,
    cache_dir: Path | None = _CACHE_DIR_OPTION,
    username: str | None = _USERNAME_OPTION,
    doc_ssh_key: Path | None = _DOC_SSH_KEY_OPTION,
    gitlab_ssh_key: Path | None = _GITLAB_SSH_KEY_OPTION,
    jump_host: str | None = _JUMP_HOST_OPTION,
    concurrency: int = _CONCURRENCY_OPTION,
    clone_timeout: float = _CLONE_TIMEOUT_OPTION,
) -> None:
    """Fetch the LabTS repository list, then clone every repository.

    Two steps, run back to back. `labts-fetch` writes `labts-list.json`
    (the full record) and `labts-list.txt` (just the ssh clone URLs);
    `gitlab-fetch` then clones each repository into
    `<output-dir>/<year>/gitlab/<repo>/` and records what it did in
    `gitlab-clones.json`.

    Both steps reuse whatever the last run produced — an existing
    repository list, and any repository already cloned — so re-running
    picks up where it left off. `--force` redoes everything.

    Needs IMPERIAL_USERNAME and IMPERIAL_PASSWORD for LabTS, and
    IMPERIAL_GITLAB_SSH_KEY (plus IMPERIAL_DOC_SSH_KEY for repositories
    only gitolite still serves) for the clones.
    """
    _run_pipeline(
        [
            LabtsFetchStep(cache_dir=cache_dir, force=force),
            _gitlab_step(force, concurrency, clone_timeout, jump_host),
        ],
        output_dir,
        dry_run,
        _settings(username, doc_ssh_key, gitlab_ssh_key),
    )


@app.command()
def gitlab(
    output_dir: Path = _OUTPUT_DIR_OPTION,
    dry_run: bool = _DRY_RUN_OPTION,
    force: bool = _FORCE_OPTION,
    username: str | None = _USERNAME_OPTION,
    doc_ssh_key: Path | None = _DOC_SSH_KEY_OPTION,
    gitlab_ssh_key: Path | None = _GITLAB_SSH_KEY_OPTION,
    jump_host: str | None = _JUMP_HOST_OPTION,
    concurrency: int = _CONCURRENCY_OPTION,
    clone_timeout: float = _CLONE_TIMEOUT_OPTION,
) -> None:
    """Clone the GitLab repositories, without refetching the LabTS list.

    The clone half of `labts` on its own: reads
    `<output-dir>/labts-list.json` from an earlier run and clones what it
    lists. Handy for retrying failed clones without going near LabTS.
    """
    _run_pipeline(
        [_gitlab_step(force, concurrency, clone_timeout, jump_host)],
        output_dir,
        dry_run,
        _settings(username, doc_ssh_key, gitlab_ssh_key),
    )


def _gitlab_step(
    force: bool, concurrency: int, clone_timeout: float, jump_host: str | None
) -> GitlabFetchStep:
    return GitlabFetchStep(
        force=force,
        concurrency=concurrency,
        timeout=clone_timeout,
        jump_host=jump_host,
    )


def _settings(
    username: str | None, doc_ssh_key: Path | None, gitlab_ssh_key: Path | None
) -> Settings:
    """Settings from the environment, with the CLI options layered on top."""
    return Settings.from_env(
        username=username,
        doc_ssh_key=str(doc_ssh_key) if doc_ssh_key else None,
        gitlab_ssh_key=str(gitlab_ssh_key) if gitlab_ssh_key else None,
    )


_EMARKING_DELAY_OPTION = typer.Option(
    1.0,
    "--delay",
    min=0.0,
    help="Seconds to wait after each API request. eMarking is a shared teaching server.",
)
_EMARKING_YEAR_OPTION = typer.Option(
    None,
    "--year",
    help=(
        "Only fetch these academic years (e.g. --year 2324 --year 2425). "
        "By default every year the API advertises is checked."
    ),
)


@app.command()
def emarking(
    output_dir: Path = _OUTPUT_DIR_OPTION,
    dry_run: bool = _DRY_RUN_OPTION,
    force: bool = _FORCE_OPTION,
    username: str | None = _USERNAME_OPTION,
    doc_ssh_key: Path | None = _DOC_SSH_KEY_OPTION,
    jump_host: str | None = _JUMP_HOST_OPTION,
    concurrency: int = _CONCURRENCY_OPTION,
    delay: float = _EMARKING_DELAY_OPTION,
    years: list[str] | None = _EMARKING_YEAR_OPTION,
) -> None:
    """Download coursework specs, submissions and feedback from eMarking.

    One request per academic year lists the exercises that are yours,
    then each spec, submission, supplementary file and feedback file is
    downloaded into `<output-dir>/<year>/<module-code>/emarking/`, with a
    record of everything in `emarking-files.json`.

    Re-running is cheap: a file already on disk is left alone, and an
    answer the API has settled (a missing artefact, or a model answer you
    aren't allowed) is not asked about again. `--force` redoes everything.

    Read-only by construction — this client only ever issues GET, and
    only ever to your own data.

    Needs IMPERIAL_USERNAME, IMPERIAL_PASSWORD, and IMPERIAL_DOC_SSH_KEY
    for the SSH SOCKS proxy that reaches the API through the DoC
    firewall.
    """
    _run_pipeline(
        [
            EmarkingFetchStep(
                force=force,
                delay=delay,
                concurrency=concurrency,
                jump_host=jump_host,
                years=list(years) if years else None,
            )
        ],
        output_dir,
        dry_run,
        _settings(username, doc_ssh_key, None),
    )


@app.command()
def scientia(
    output_dir: Path = _OUTPUT_DIR_OPTION,
    dry_run: bool = _DRY_RUN_OPTION,
) -> None:
    """Fetch personal data from Scientia (not implemented yet)."""
    _run_pipeline([ScientiaFetchStep()], output_dir, dry_run)


if __name__ == "__main__":
    app()
