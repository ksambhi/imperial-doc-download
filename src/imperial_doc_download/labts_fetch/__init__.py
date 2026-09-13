"""Fetch data from LabTS — Imperial DoC's coursework submission/test system.

Placeholder module: this pipeline stage isn't implemented yet. Once it is,
`LabtsFetchStep` gets added to the `steps` list built in `cli.py`, alongside
the other `*_fetch` stages (`gitlab_fetch`, `scientia_fetch`, ...).
"""

from __future__ import annotations

import logging

from imperial_doc_download.pipeline import PipelineContext, Step

logger = logging.getLogger(__name__)


class LabtsFetchStep(Step):
    """Downloads submissions and results from LabTS."""

    name = "labts-fetch"

    def run(self, ctx: PipelineContext) -> None:
        raise NotImplementedError("labts-fetch is a placeholder and isn't implemented yet.")
