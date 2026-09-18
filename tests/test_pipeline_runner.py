"""Tests for the pipeline runner, including `continue_on_error`."""

from __future__ import annotations

import pytest

from imperial_doc_download.config import Settings
from imperial_doc_download.pipeline import Pipeline, PipelineContext, Step
from imperial_doc_download.pipeline.runner import StepFailed


class Recorder(Step):
    def __init__(self, name: str, *, fails: bool = False) -> None:
        self.name = name
        self._fails = fails
        self.ran = False

    def run(self, ctx: PipelineContext) -> None:
        self.ran = True
        if self._fails:
            raise RuntimeError(f"{self.name} went wrong")


def _context(tmp_path) -> PipelineContext:
    return PipelineContext(output_dir=tmp_path, settings=Settings())


class TestDefault:
    def test_runs_every_step_in_order(self, tmp_path) -> None:
        steps = [Recorder("one"), Recorder("two")]
        Pipeline(steps).run(_context(tmp_path))
        assert all(step.ran for step in steps)

    def test_a_failure_aborts_the_rest(self, tmp_path) -> None:
        first, second = Recorder("one", fails=True), Recorder("two")
        with pytest.raises(RuntimeError, match="one went wrong"):
            Pipeline([first, second]).run(_context(tmp_path))
        assert not second.ran


class TestContinueOnError:
    def test_later_steps_still_run(self, tmp_path) -> None:
        # The point of `all`: LabTS being down is no reason to skip
        # eMarking as well.
        first, second = Recorder("one", fails=True), Recorder("two")
        with pytest.raises(StepFailed):
            Pipeline([first, second], continue_on_error=True).run(_context(tmp_path))
        assert second.ran

    def test_every_failure_is_reported_not_just_the_first(self, tmp_path) -> None:
        steps = [Recorder("one", fails=True), Recorder("two"), Recorder("three", fails=True)]
        with pytest.raises(StepFailed) as excinfo:
            Pipeline(steps, continue_on_error=True).run(_context(tmp_path))

        assert [name for name, _ in excinfo.value.failures] == ["one", "three"]
        assert "one went wrong" in str(excinfo.value)
        assert "three went wrong" in str(excinfo.value)

    def test_nothing_is_raised_when_everything_works(self, tmp_path) -> None:
        steps = [Recorder("one"), Recorder("two")]
        Pipeline(steps, continue_on_error=True).run(_context(tmp_path))
        assert all(step.ran for step in steps)

    def test_dry_run_executes_nothing(self, tmp_path) -> None:
        ctx = _context(tmp_path)
        ctx.dry_run = True
        steps = [Recorder("one", fails=True)]
        Pipeline(steps, continue_on_error=True).run(ctx)
        assert not steps[0].ran
