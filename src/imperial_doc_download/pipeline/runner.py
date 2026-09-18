"""Sequential pipeline runner."""

from __future__ import annotations

import logging

from imperial_doc_download.pipeline.base import PipelineContext, Step

logger = logging.getLogger(__name__)


class StepFailed(RuntimeError):
    """One or more steps failed in a `continue_on_error` run.

    Carries every failure rather than just the first, because the whole
    point of that mode is to find out about all of them in one go.
    """

    def __init__(self, failures: list[tuple[str, BaseException]]) -> None:
        self.failures = failures
        summary = "; ".join(f"{name}: {error}" for name, error in failures)
        super().__init__(summary)


class Pipeline:
    """Runs a fixed, ordered list of steps against one shared context.

    By default a failing step aborts the run, which is what you want when
    the later steps consume the earlier ones' output. `continue_on_error`
    keeps going instead and raises `StepFailed` at the end — for the
    `all` command, where LabTS being down is no reason to skip eMarking
    as well.
    """

    def __init__(self, steps: list[Step], *, continue_on_error: bool = False) -> None:
        self.steps = steps
        self.continue_on_error = continue_on_error

    def run(self, ctx: PipelineContext) -> None:
        if not self.steps:
            logger.warning("Pipeline has no steps registered — nothing to do.")
            return

        failures: list[tuple[str, BaseException]] = []
        for step in self.steps:
            if ctx.dry_run:
                logger.info("[dry-run] would run step: %s", step.name)
                continue

            logger.info("Running step: %s", step.name)
            try:
                step.run(ctx)
            except Exception as exc:
                if not self.continue_on_error:
                    raise
                # Logged with a traceback here because the summary raised
                # at the end only carries the message, and a failure
                # twenty minutes into a long run deserves the detail.
                logger.exception("Step %s failed — continuing with the rest.", step.name)
                failures.append((step.name, exc))

        logger.info("Pipeline finished (%d step(s), %d failed).", len(self.steps), len(failures))
        if failures:
            raise StepFailed(failures)
