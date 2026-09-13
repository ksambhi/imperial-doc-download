"""Fetch data from Imperial DoC's self-hosted GitLab (gitlab.doc.ic.ac.uk).

Placeholder module: this pipeline stage isn't implemented yet. Once it is,
`GitlabFetchStep` gets added to the `steps` list built in `cli.py`, alongside
the other `*_fetch` stages (`labts_fetch`, `scientia_fetch`, ...).
"""

from __future__ import annotations

import logging

from imperial_doc_download.pipeline import PipelineContext, Step

logger = logging.getLogger(__name__)


class GitlabFetchStep(Step):
    """Clones/downloads repos (and any other data worth keeping) from DoC GitLab."""

    name = "gitlab-fetch"

    def run(self, ctx: PipelineContext) -> None:
        raise NotImplementedError("gitlab-fetch is a placeholder and isn't implemented yet.")
