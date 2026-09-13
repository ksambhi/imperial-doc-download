"""Typer entry point for imperial-doc-download."""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from imperial_doc_download import __version__
from imperial_doc_download.config import Settings
from imperial_doc_download.labts_fetch import LabtsFetchStep
from imperial_doc_download.logging import setup_logging
from imperial_doc_download.pipeline import Pipeline, PipelineContext

app = typer.Typer(
    name="imperial-doc-download",
    help="Back up personal data from Imperial College Department of Computing systems.",
    no_args_is_help=True,
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


@app.command()
def run(
    output_dir: Path = typer.Option(
        Path("./imperial-data"),
        "--output-dir",
        "-o",
        help="Directory to download data into.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the steps that would run without downloading anything.",
    ),
    cache_dir: Path | None = typer.Option(
        None,
        "--cache-dir",
        help=(
            "Cache fetched pages here and reuse them on later runs. "
            "Makes re-runs near-instant; delete the directory to refetch."
        ),
    ),
) -> None:
    """Run the full download pipeline."""
    settings = Settings.from_env()
    ctx = PipelineContext(output_dir=output_dir, settings=settings, dry_run=dry_run)
    ctx.output_dir.mkdir(parents=True, exist_ok=True)

    # More steps get registered here as each `*_fetch` module is implemented,
    # e.g. imperial_doc_download.gitlab_fetch.GitlabFetchStep().
    pipeline = Pipeline(steps=[LabtsFetchStep(cache_dir=cache_dir)])
    pipeline.run(ctx)


if __name__ == "__main__":
    app()
