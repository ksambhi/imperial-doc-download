"""Logging setup for the CLI."""

from __future__ import annotations

import logging


def setup_logging(verbose: bool = False) -> None:
    """Configure root logging.

    Call once, from the Typer app callback, before any pipeline code runs.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
