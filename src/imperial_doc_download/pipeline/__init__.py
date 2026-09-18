"""The download pipeline: an ordered list of `Step`s run against a shared context."""

from imperial_doc_download.pipeline.base import PipelineContext, Step
from imperial_doc_download.pipeline.runner import Pipeline

__all__ = ["Pipeline", "PipelineContext", "Step"]
