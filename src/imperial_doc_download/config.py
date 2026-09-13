"""Runtime configuration for the CLI.

This stays deliberately small for now. As pipeline steps for individual
Imperial DoC systems get implemented, whatever credentials/config each one
needs (session cookies, API tokens, base URLs, ...) should be added here
rather than scattered across the step modules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    """Configuration loaded from environment variables.

    All fields are optional at this layer — a given pipeline step is
    responsible for validating that the settings it needs are actually
    present before it runs.
    """

    username: str | None = field(default_factory=lambda: os.environ.get("IMPERIAL_USERNAME"))

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the current environment (main entry point)."""
        return cls()
