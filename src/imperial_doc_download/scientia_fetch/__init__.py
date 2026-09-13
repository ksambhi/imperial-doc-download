"""Fetch data from Scientia — Imperial's timetabling/exams system.

Placeholder module: this pipeline stage isn't implemented yet. Once it is,
`ScientiaFetchStep` gets added to the `steps` list built in `cli.py`,
alongside the other `*_fetch` stages (`labts_fetch`, `gitlab_fetch`, ...).
"""

from __future__ import annotations

import logging

from imperial_doc_download.pipeline import PipelineContext, Step

logger = logging.getLogger(__name__)


class ScientiaFetchStep(Step):
    """Downloads personal timetable/exam data from Scientia."""

    name = "scientia-fetch"

    def run(self, ctx: PipelineContext) -> None:
        raise NotImplementedError("scientia-fetch is a placeholder and isn't implemented yet.")
