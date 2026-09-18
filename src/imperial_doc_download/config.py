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
    password: str | None = field(default_factory=lambda: os.environ.get("IMPERIAL_PASSWORD"))

    #: Private key for the DoC shell servers, used to proxy-jump to the
    #: firewalled gitolite server.
    doc_ssh_key: str | None = field(default_factory=lambda: os.environ.get("IMPERIAL_DOC_SSH_KEY"))
    #: Private key registered with DoC GitLab (and gitolite behind it).
    gitlab_ssh_key: str | None = field(
        default_factory=lambda: os.environ.get("IMPERIAL_GITLAB_SSH_KEY")
    )

    # Passphrases are deliberately environment-only — no CLI flag — so a
    # secret never ends up in shell history or a process list. Leave them
    # unset for unencrypted keys, or for keys already in your ssh-agent.
    doc_ssh_key_passphrase: str | None = field(
        default_factory=lambda: os.environ.get("IMPERIAL_DOC_SSH_KEY_PASSPHRASE")
    )
    gitlab_ssh_key_passphrase: str | None = field(
        default_factory=lambda: os.environ.get("IMPERIAL_GITLAB_SSH_KEY_PASSPHRASE")
    )

    @classmethod
    def from_env(cls, **overrides: str | None) -> Settings:
        """Build settings from the current environment (main entry point).

        Any keyword given a non-`None` value wins over the environment, so
        a CLI option can override the corresponding env var. (Typer reads
        the env vars itself for options declared with `envvar=`, so in
        practice these arrive already resolved.)
        """
        return cls(**{name: value for name, value in overrides.items() if value is not None})
