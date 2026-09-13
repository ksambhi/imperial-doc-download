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


def _run_pipeline(steps: list[Step], output_dir: Path, dry_run: bool) -> None:
    """Run one system's pipeline against a freshly built context."""
    ctx = PipelineContext(
        output_dir=output_dir,
        settings=Settings.from_env(),
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


@app.command()
def labts(
    output_dir: Path = _OUTPUT_DIR_OPTION,
    dry_run: bool = _DRY_RUN_OPTION,
    cache_dir: Path | None = typer.Option(
        None,
        "--cache-dir",
        help=(
            "Cache fetched pages here and reuse them on later runs. "
            "Makes re-runs near-instant; delete the directory to refetch."
        ),
    ),
) -> None:
    """Fetch the LabTS exercise/repository list.

    Writes `labts-list.json` (the full record) and `labts-list.txt` (just
    the ssh clone URLs, grouped by academic year) into the output
    directory. Requires IMPERIAL_USERNAME and IMPERIAL_PASSWORD.
    """
    _run_pipeline([LabtsFetchStep(cache_dir=cache_dir)], output_dir, dry_run)


@app.command()
def scientia(
    output_dir: Path = _OUTPUT_DIR_OPTION,
    dry_run: bool = _DRY_RUN_OPTION,
) -> None:
    """Fetch personal data from Scientia (not implemented yet)."""
    _run_pipeline([ScientiaFetchStep()], output_dir, dry_run)


if __name__ == "__main__":
    app()
