"""Sequential pipeline runner."""

from __future__ import annotations

import logging

from imperial_doc_download.pipeline.base import PipelineContext, Step

logger = logging.getLogger(__name__)


class Pipeline:
    """Runs a fixed, ordered list of steps against one shared context."""

    def __init__(self, steps: list[Step]) -> None:
        self.steps = steps

    def run(self, ctx: PipelineContext) -> None:
        if not self.steps:
            logger.warning("Pipeline has no steps registered — nothing to do.")
            return

        for step in self.steps:
            if ctx.dry_run:
                logger.info("[dry-run] would run step: %s", step.name)
                continue

            logger.info("Running step: %s", step.name)
            step.run(ctx)

        logger.info("Pipeline finished (%d step(s)).", len(self.steps))
