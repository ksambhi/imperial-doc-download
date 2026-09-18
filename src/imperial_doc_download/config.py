"""Runtime configuration for the CLI.

This stays deliberately small for now. As pipeline steps for individual
Imperial DoC systems get implemented, whatever credentials/config each one
needs (session cookies, API tokens, base URLs, ...) should be added here
rather than scattered across the step modules.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ENV_FILE = Path(".env")


def parse_env_file(path: Path) -> dict[str, str]:
    """Read a `.env` file **literally** — never through a shell.

    This exists because the obvious alternative is wrong in a way that
    costs an afternoon:

        set -a; . ./.env; set +a      # WRONG

    A password containing shell metacharacters gets partly expanded away
    by that, silently: 17 characters in the file, 15 in the environment,
    and an authentication failure that looks exactly like an expired
    password. So: split on the first `=`, take the rest verbatim, and
    never let a shell near it.

    Values are taken as-is apart from one pair of surrounding quotes,
    which is the one convention common enough that not honouring it would
    surprise people. No escape sequences, no interpolation, no `export `
    prefix handling beyond stripping it.
    """
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()

        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(path: Path | None = None) -> list[str]:
    """Load a `.env` into `os.environ`, returning the names it set.

    A variable already set in the real environment always wins — an
    explicit `export` on the command line should never be overridden by a
    file that happens to be in the working directory.
    """
    path = path or DEFAULT_ENV_FILE
    if not path.is_file():
        return []

    loaded = []
    for key, value in parse_env_file(path).items():
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    # Names only. Never the values -- this runs before logging is even
    # configured to a file, and one of these is a password.
    logger.debug("Loaded %d variable(s) from %s: %s", len(loaded), path, ", ".join(loaded))
    return loaded


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
