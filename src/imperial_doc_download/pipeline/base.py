"""Base types shared by every pipeline step."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from imperial_doc_download.config import Settings


@dataclass
class PipelineContext:
    """State threaded through every step of a pipeline run."""

    output_dir: Path
    settings: Settings
    dry_run: bool = False
    # Free-form scratch space for steps to hand data to later steps
    # (e.g. a login step stashing an authenticated session).
    state: dict[str, Any] = field(default_factory=dict)


class Step(ABC):
    """One unit of work in the download pipeline (e.g. "log in", "fetch labs")."""

    #: Short, human-readable name shown in logs and `--dry-run` output.
    name: str = "step"

    @abstractmethod
    def run(self, ctx: PipelineContext) -> None:
        """Execute the step. Raise on unrecoverable failure."""
        raise NotImplementedError
