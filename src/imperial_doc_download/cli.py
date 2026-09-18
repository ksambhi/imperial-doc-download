"""Typer entry point for imperial-doc-download.

    imperial-doc-download all       [options]   <- everything, in one go
    imperial-doc-download labts     [options]
    imperial-doc-download emarking  [options]
    imperial-doc-download scientia  [options]

`all` runs every implemented pipeline against one shared context, so the
steps that can hand data to each other still do. It keeps going when a
step fails — LabTS being down is no reason to skip eMarking as well —
and reports what failed at the end.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import typer

from imperial_doc_download import __version__
from imperial_doc_download.config import Settings, load_env_file
from imperial_doc_download.emarking_fetch import EmarkingFetchStep, EmarkingMarksStep
from imperial_doc_download.gitlab_fetch import GitlabFetchStep
from imperial_doc_download.gitlab_fetch.cloning import DEFAULT_CLONE_TIMEOUT
from imperial_doc_download.labts_fetch import LabtsFetchStep
from imperial_doc_download.logging import setup_logging
from imperial_doc_download.pipeline import Pipeline, PipelineContext, Step
from imperial_doc_download.pipeline.runner import StepFailed
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
#: Everything here is I/O-bound — clones waiting on a remote, downloads
#: waiting on a socket — so the core count isn't a hard ceiling so much as
#: a familiar, machine-appropriate number to fan out to. Note that the
#: HTTP pipelines still sleep `--delay` after each request *inside* the
#: semaphore, so this raises the request rate rather than removing the
#: throttle: on a many-core machine, consider lowering `-j` or raising
#: `--delay` against a teaching server that looks strained.
DEFAULT_CONCURRENCY = os.cpu_count() or 1

_CONCURRENCY_OPTION = typer.Option(
    DEFAULT_CONCURRENCY,
    "--concurrency",
    "-j",
    min=1,
    help=(
        "How many clones/downloads to run at once. Defaults to this "
        "machine's CPU count; pass 1 for strictly sequential."
    ),
)
_CLONE_TIMEOUT_OPTION = typer.Option(
    DEFAULT_CLONE_TIMEOUT,
    "--clone-timeout",
    min=1.0,
    help="Seconds to let a single git command run before giving up on it.",
)


_CACHE_DIR_OPTION = typer.Option(
    None,
    "--cache-dir",
    help=(
        "Cache fetched pages here and reuse them on later runs. "
        "Makes re-runs near-instant; delete the directory to refetch."
    ),
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

_EMARKING_MODEL_ANSWERS_OPTION = typer.Option(
    False,
    "--model-answers",
    help=(
        "Also try to download model answers. Doesn't work, and probably "
        "shouldn't: every one comes back 403, and model answers escaping to "
        "students would compromise future years' coursework. Off by default "
        "— the requests would all be refused."
    ),
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"imperial-doc-download {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
    env_file: Path = typer.Option(
        Path(".env"),
        "--env-file",
        help=(
            "Read credentials from this file if it exists. Parsed literally, "
            "never through a shell. Anything already exported wins."
        ),
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """imperial-doc-download: grab your data before your account gets nuked."""
    # Before logging is set up, so the debug line naming the variables
    # lands in the file too -- and before any subcommand resolves its
    # `envvar=` options, which is what makes this work at all.
    loaded = load_env_file(env_file)
    log_file = setup_logging(verbose=verbose)
    logger = logging.getLogger(__name__)
    logger.debug("Logging to %s", log_file)
    if loaded:
        logger.info("Read %d setting(s) from %s.", len(loaded), env_file)


def _run_pipeline(
    steps: list[Step],
    output_dir: Path,
    dry_run: bool,
    settings: Settings | None = None,
    *,
    continue_on_error: bool = False,
) -> None:
    """Run one system's pipeline against a freshly built context."""
    ctx = PipelineContext(
        output_dir=output_dir,
        settings=settings or Settings.from_env(),
        dry_run=dry_run,
    )
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        Pipeline(steps=steps, continue_on_error=continue_on_error).run(ctx)
    except StepFailed as exc:
        # Everything that could run has run; say what didn't and exit
        # non-zero so a script notices.
        typer.secho(
            f"\n{len(exc.failures)} step(s) failed:", fg=typer.colors.RED, err=True, bold=True
        )
        for name, error in exc.failures:
            typer.secho(f"  ✗ {name}: {error}", fg=typer.colors.RED, err=True)
        typer.secho(
            "Everything else finished. Re-run to retry just the failures.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(1) from exc
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


@app.command("all")
def download_all(
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
    delay: float = _EMARKING_DELAY_OPTION,
    model_answers: bool = _EMARKING_MODEL_ANSWERS_OPTION,
) -> None:
    """Download everything: LabTS, the GitLab repositories, and eMarking.

    Runs all four implemented steps against one shared context, in the
    order they depend on each other:

    \b
      1. labts-fetch     the repository list             → labts-list.json
      2. gitlab-fetch    clone every repository          → <year>/gitlab/
      3. emarking-fetch  specs, submissions, feedback    → <year>/<module>/emarking/
      4. emarking-marks  the marks record and web page   → emarking-results.html

    A failing step doesn't stop the rest — LabTS being down is no reason
    to lose the eMarking half — and what failed is listed at the end.
    Re-running retries only what's missing, so it's safe to just run it
    again.

    `-j/--concurrency` applies to both the clones and the eMarking
    downloads. Be modest with it: the clones go through a shared shell
    server and eMarking is a shared teaching server.

    Needs IMPERIAL_USERNAME and IMPERIAL_PASSWORD, plus
    IMPERIAL_GITLAB_SSH_KEY and IMPERIAL_DOC_SSH_KEY. A `.env` in the
    working directory is read automatically.
    """
    _run_pipeline(
        [
            LabtsFetchStep(cache_dir=cache_dir, force=force),
            _gitlab_step(force, concurrency, clone_timeout, jump_host),
            EmarkingFetchStep(
                force=force,
                delay=delay,
                concurrency=concurrency,
                jump_host=jump_host,
                model_answers=model_answers,
            ),
            EmarkingMarksStep(force=force, jump_host=jump_host),
        ],
        output_dir,
        dry_run,
        _settings(username, doc_ssh_key, gitlab_ssh_key),
        # The whole point of this command is one invocation that gets as
        # much as it can; aborting on the first problem defeats it.
        continue_on_error=True,
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
    model_answers: bool = _EMARKING_MODEL_ANSWERS_OPTION,
) -> None:
    """Download coursework files from eMarking, then write your marks record.

    Two steps. `emarking-fetch` lists the exercises in every module you
    were enrolled in and downloads each spec, submission, supplementary
    file and feedback file into
    `<output-dir>/<year>/<module-code>/emarking/`, recording what it did
    in `emarking-files.json`. `emarking-marks` then writes
    `emarking-marks.json` — every marked exercise with its deadline,
    mark, percentage, grade and status colour — and costs no requests.

    Re-running is cheap: a file already on disk is left alone, and an
    answer the API has settled (a missing artefact, a year you weren't
    enrolled in) is not asked about again. `--force` redoes everything.

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
                model_answers=model_answers,
            ),
            EmarkingMarksStep(force=force, jump_host=jump_host),
        ],
        output_dir,
        dry_run,
        _settings(username, doc_ssh_key, None),
    )


@app.command("emarking-marks")
def emarking_marks(
    output_dir: Path = _OUTPUT_DIR_OPTION,
    dry_run: bool = _DRY_RUN_OPTION,
    username: str | None = _USERNAME_OPTION,
    doc_ssh_key: Path | None = _DOC_SSH_KEY_OPTION,
    jump_host: str | None = _JUMP_HOST_OPTION,
) -> None:
    """Rebuild `emarking-marks.json` from an earlier run, without downloading.

    The marks record is a pure reshaping of what `emarking` already
    wrote, so this normally makes no requests at all — handy after
    changing how the record is built.
    """
    _run_pipeline(
        [EmarkingMarksStep(jump_host=jump_host)],
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
