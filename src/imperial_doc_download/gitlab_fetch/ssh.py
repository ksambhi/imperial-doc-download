"""SSH setup for cloning DoC repositories, without touching `~/.ssh/config`.

There are two remotes behind a LabTS exercise, reached very differently:

- **GitLab** (`gitlab.doc.ic.ac.uk`) is reachable from anywhere with the
  GitLab key alone.
- **gitolite** (`gitolite.doc.ic.ac.uk`) lives behind the DoC firewall, so
  it can only be reached by proxy-jumping through a DoC shell server —
  which needs a *second* key (the shell-server one) and the DoC username.
  Git traffic then goes shell key → shell server → GitLab key → gitolite.

Rather than requiring the user to have all that in `~/.ssh/config` (and
never, ever editing it for them), this module describes those hosts as
`Host` blocks for the throwaway config that `imperial_doc_download.ssh`
writes into a per-run temporary directory, and hands it to git as
`GIT_SSH_COMMAND="ssh -F <that file>"`. OpenSSH propagates `-F` to the
`ProxyJump` subprocess, so the jump host is configured by the same file.

Key discovery, unlocking and the private-agent dance all live in
`imperial_doc_download.ssh` — `emarking_fetch`'s SOCKS proxy needs the
same plumbing for the same shell servers.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path

from imperial_doc_download.ssh import SHELL_SERVERS, SshKey, SshSession, SshSetupError

__all__ = ["GITLAB_HOST", "GITOLITE_HOST", "SHELL_SERVERS", "SshEnvironment", "SshSetupError"]

logger = logging.getLogger(__name__)

GITLAB_HOST = "gitlab.doc.ic.ac.uk"
GITOLITE_HOST = "gitolite.doc.ic.ac.uk"

#: Alias for the jump host inside the generated config. Only ever used by
#: `ProxyJump`, so it can't collide with anything the user has.
_JUMP_ALIAS = "imperial-doc-jump"


class SshEnvironment(SshSession):
    """A self-contained SSH setup for one clone run, as a context manager.

    ``with SshEnvironment(...) as ssh:`` yields an object whose `env` is
    ready to hand to `git` subprocesses. Everything it creates lives in one
    temporary directory that's removed on exit.
    """

    def __init__(
        self,
        *,
        gitlab_key: Path,
        doc_key: Path | None = None,
        username: str | None = None,
        gitlab_key_passphrase: str | None = None,
        doc_key_passphrase: str | None = None,
        jump_host: str | None = None,
        allow_prompt: bool = True,
    ) -> None:
        self._gitlab_key = SshKey(
            Path(gitlab_key).expanduser(), "IMPERIAL_GITLAB_SSH_KEY", gitlab_key_passphrase
        )
        self._doc_key = (
            SshKey(Path(doc_key).expanduser(), "IMPERIAL_DOC_SSH_KEY", doc_key_passphrase)
            if doc_key is not None
            else None
        )
        self._username = username
        self._jump_host = jump_host or random.choice(SHELL_SERVERS)

        super().__init__(
            [key for key in (self._gitlab_key, self._doc_key) if key is not None],
            allow_prompt=allow_prompt,
        )

    @property
    def supports_gitolite(self) -> bool:
        """Whether the gitolite fallback is usable at all.

        It needs both a DoC shell-server key and the username to jump with;
        without them we can still clone straight from GitLab.
        """
        return self._doc_key is not None and bool(self._username)

    @property
    def jump_host(self) -> str:
        return self._jump_host

    @property
    def git_ssh_command(self) -> str:
        return self.ssh_command

    @property
    def env(self) -> dict[str, str]:
        """Environment for `git` subprocesses: our ssh config, no prompts."""
        return {
            **super().env,
            "GIT_SSH_COMMAND": self.git_ssh_command,
            # Belt and braces with BatchMode: never block on a credential
            # prompt, whatever the remote asks for.
            "GIT_TERMINAL_PROMPT": "0",
        }

    def _host_blocks(self) -> list[str]:
        blocks = [
            f"Host {GITLAB_HOST}",
            f"    HostName {GITLAB_HOST}",
            "    User git",
            f"    IdentityFile {self._gitlab_key.path}",
            "    IdentitiesOnly yes",
            "",
        ]

        if self.supports_gitolite:
            assert self._doc_key is not None
            blocks += [
                f"Host {GITOLITE_HOST}",
                f"    HostName {GITOLITE_HOST}",
                "    User gitolite",
                f"    IdentityFile {self._gitlab_key.path}",
                "    IdentitiesOnly yes",
                f"    ProxyJump {_JUMP_ALIAS}",
                "",
                # The jump host: a DoC shell server reached with the *other*
                # key, which is what gets us through the departmental firewall.
                f"Host {_JUMP_ALIAS}",
                f"    HostName {self._jump_host}",
                f"    User {self._username}",
                f"    IdentityFile {self._doc_key.path}",
                "    IdentitiesOnly yes",
                "",
            ]

        return blocks
