"""SSH plumbing for cloning DoC repositories, without touching `~/.ssh/config`.

There are two remotes behind a LabTS exercise, reached very differently:

- **GitLab** (`gitlab.doc.ic.ac.uk`) is reachable from anywhere with the
  GitLab key alone.
- **gitolite** (`gitolite.doc.ic.ac.uk`) lives behind the DoC firewall, so
  it can only be reached by proxy-jumping through a DoC shell server —
  which needs a *second* key (the shell-server one) and the DoC username.
  Git traffic then goes shell key → shell server → GitLab key → gitolite.

Rather than requiring the user to have all that in `~/.ssh/config` (and
never, ever editing it for them), this module writes a throwaway config
into a per-run temporary directory and hands it to git as
`GIT_SSH_COMMAND="ssh -F <that file>"`. OpenSSH propagates `-F` to the
`ProxyJump` subprocess, so the jump host is configured by the same file.
The directory — config, agent socket and all — is deleted when the run ends.

Passphrase-protected keys are handled once per run rather than once per
clone: if a locked key isn't already in the user's own agent, a private
`ssh-agent` is started for this run only, the key is unlocked into it, and
the generated config points at it via `IdentityAgent`. That keeps clones
non-interactive (`BatchMode yes`, so nothing can ever hang on a prompt)
and is what will let clones run in parallel later.
"""

from __future__ import annotations

import getpass
import logging
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

GITLAB_HOST = "gitlab.doc.ic.ac.uk"
GITOLITE_HOST = "gitolite.doc.ic.ac.uk"

#: The DoC shell servers that can be proxy-jumped through to reach gitolite.
#: One is picked at random per run to spread the load rather than always
#: hammering the same box. (`sftp.doc.ic.ac.uk` looks like the polite
#: choice, and is what people often alias to a shell server, but the real
#: host of that name only serves SFTP and refuses ssh key auth.)
SHELL_SERVERS = tuple(f"shell{n}.doc.ic.ac.uk" for n in range(1, 6))

#: Alias for the jump host inside the generated config. Only ever used by
#: `ProxyJump`, so it can't collide with anything the user has.
_JUMP_ALIAS = "imperial-doc-jump"

#: Env var the askpass helper reads the passphrase out of. It's passed to
#: `ssh-add` through the environment (never written to disk, never logged,
#: and on both Linux and macOS a process's environment is readable only by
#: its own user).
_ASKPASS_ENV_VAR = "IMPERIAL_DOC_DOWNLOAD_PASSPHRASE"

#: Env var naming a file the helper touches once it has answered.
_ASKPASS_SENTINEL_ENV_VAR = "IMPERIAL_DOC_DOWNLOAD_ASKED"

# Answers exactly once, on purpose. `ssh-add` re-asks indefinitely while
# the passphrase it gets is wrong, and only gives up when handed an empty
# one — so a helper that kept repeating itself would wedge the whole run
# on a typo'd passphrase instead of failing with a message.
_ASKPASS_SCRIPT = f"""#!/bin/sh
if [ -e "${_ASKPASS_SENTINEL_ENV_VAR}" ]; then
    exit 0
fi
: > "${_ASKPASS_SENTINEL_ENV_VAR}"
printf '%s' "${_ASKPASS_ENV_VAR}"
"""


class SshSetupError(RuntimeError):
    """Raised when the SSH environment can't be built (missing key, locked
    key with no passphrase available, agent refused the key, ...)."""


@dataclass(frozen=True)
class SshKey:
    """One private key, plus where to get its passphrase if it has one."""

    path: Path
    #: Human-readable source, used in error messages (e.g. the env var name).
    label: str
    passphrase: str | None = None


class _Agent:
    """A private `ssh-agent` started for, and killed at the end of, one run."""

    def __init__(self, sock: str, pid: int | None) -> None:
        self.sock = sock
        self.pid = pid

    @classmethod
    def start(cls) -> _Agent:
        try:
            proc = subprocess.run(["ssh-agent", "-s"], capture_output=True, text=True, check=True)
        except FileNotFoundError as exc:  # pragma: no cover - needs a box without ssh
            raise SshSetupError(
                "ssh-agent isn't on PATH, so a passphrase-protected key can't be "
                "unlocked. Install OpenSSH, or load the key into your own agent first."
            ) from exc
        except subprocess.CalledProcessError as exc:  # pragma: no cover - rare
            raise SshSetupError(f"ssh-agent failed to start: {exc.stderr.strip()}") from exc

        sock = _search(r"SSH_AUTH_SOCK=([^;]+);", proc.stdout)
        pid = _search(r"SSH_AGENT_PID=(\d+);", proc.stdout)
        if sock is None:
            raise SshSetupError("Couldn't parse the socket path out of ssh-agent's output.")

        logger.debug("Started a private ssh-agent (pid %s) for this run.", pid)
        return cls(sock, int(pid) if pid else None)

    def add(self, key: SshKey, passphrase: str, askpass: Path, sentinel: Path) -> None:
        """Unlock `key` into this agent using `passphrase`."""
        sentinel.unlink(missing_ok=True)
        env = {
            **os.environ,
            "SSH_AUTH_SOCK": self.sock,
            "SSH_ASKPASS": str(askpass),
            _ASKPASS_SENTINEL_ENV_VAR: str(sentinel),
            # OpenSSH >= 8.4 honours this and uses the helper unconditionally.
            "SSH_ASKPASS_REQUIRE": "force",
            # Older OpenSSH only falls back to the helper when DISPLAY is set
            # and there's no controlling terminal (hence start_new_session).
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
            _ASKPASS_ENV_VAR: passphrase,
        }
        result = subprocess.run(
            ["ssh-add", str(key.path)],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            start_new_session=True,
        )
        if result.returncode != 0:
            raise SshSetupError(
                f"Couldn't unlock {key.path} (from {key.label}) — check its passphrase. "
                f"ssh-add said: {result.stderr.strip() or 'nothing'}"
            )
        logger.info("Unlocked %s into this run's private ssh-agent.", key.path)

    def stop(self) -> None:
        if self.pid is None:
            return
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:  # pragma: no cover - the agent already died
            logger.debug("Private ssh-agent (pid %s) was already gone.", self.pid)


class SshEnvironment:
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
        self._allow_prompt = allow_prompt

        self._tempdir: Path | None = None
        self._agent: _Agent | None = None
        self._config_path: Path | None = None

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
    def config_path(self) -> Path:
        if self._config_path is None:
            raise RuntimeError("SshEnvironment must be entered before it can be used.")
        return self._config_path

    @property
    def git_ssh_command(self) -> str:
        return f"ssh -F {shlex.quote(str(self.config_path))}"

    @property
    def env(self) -> dict[str, str]:
        """Environment for `git` subprocesses: our ssh config, no prompts."""
        env = {
            **os.environ,
            "GIT_SSH_COMMAND": self.git_ssh_command,
            # Belt and braces with BatchMode: never block on a credential
            # prompt, whatever the remote asks for.
            "GIT_TERMINAL_PROMPT": "0",
        }
        if self._agent is not None:
            env["SSH_AUTH_SOCK"] = self._agent.sock
        return env

    def __enter__(self) -> SshEnvironment:
        self._tempdir = Path(tempfile.mkdtemp(prefix="imperial-doc-download-ssh-"))
        self._tempdir.chmod(0o700)
        try:
            self._check_keys_exist()
            self._unlock_keys()
            self._config_path = self._write_config()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._agent is not None:
            self._agent.stop()
            self._agent = None
        if self._tempdir is not None:
            shutil.rmtree(self._tempdir, ignore_errors=True)
            self._tempdir = None
        self._config_path = None

    def _keys(self) -> list[SshKey]:
        return [k for k in (self._gitlab_key, self._doc_key) if k is not None]

    def _check_keys_exist(self) -> None:
        for key in self._keys():
            if not key.path.is_file():
                raise SshSetupError(f"No such SSH key: {key.path} (from {key.label}).")

    def _unlock_keys(self) -> None:
        """Make sure every passphrase-protected key is usable non-interactively.

        Keys the user has already loaded into their own agent are left
        alone; anything else goes into a private agent for this run.
        """
        locked = [key for key in self._keys() if _key_is_encrypted(key.path)]
        if not locked:
            return

        in_user_agent = _agent_fingerprints()
        needed = [key for key in locked if _fingerprint(key.path) not in in_user_agent]
        if not needed:
            logger.info("Passphrase-protected key(s) are already in your ssh-agent.")
            return

        assert self._tempdir is not None
        askpass = self._tempdir / "askpass.sh"
        askpass.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
        askpass.chmod(0o700)

        self._agent = _Agent.start()
        for index, key in enumerate(needed):
            self._agent.add(
                key,
                self._passphrase_for(key),
                askpass,
                self._tempdir / f"asked-{index}",
            )

    def _passphrase_for(self, key: SshKey) -> str:
        if key.passphrase:
            return key.passphrase
        if not (self._allow_prompt and sys.stdin.isatty()):
            raise SshSetupError(
                f"{key.path} (from {key.label}) is passphrase-protected, but there's no "
                f"passphrase to unlock it with and no terminal to ask at. Either set "
                f"{key.label}_PASSPHRASE, or run `ssh-add {key.path}` first so it's in "
                f"your own agent."
            )
        # Asked once per run, not once per clone: the agent holds it after this.
        return getpass.getpass(f"Passphrase for {key.path} ({key.label}): ")

    def _write_config(self) -> Path:
        assert self._tempdir is not None
        path = self._tempdir / "ssh_config"

        blocks = [
            "# Written by imperial-doc-download for a single run, and deleted",
            "# afterwards. Your own ~/.ssh/config is neither read nor modified.",
            "",
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

        blocks += [
            "Host *",
            # accept-new records first-seen hosts in the user's known_hosts
            # but still aborts loudly if a known host's key ever changes.
            "    StrictHostKeyChecking accept-new",
            # Nothing may block waiting for input: an unattended run must
            # fail with a message rather than hang forever on a prompt.
            "    BatchMode yes",
            "    ConnectTimeout 30",
            "    ServerAliveInterval 30",
        ]
        if self._agent is not None:
            blocks.append(f"    IdentityAgent {self._agent.sock}")
        blocks.append("")

        path.write_text("\n".join(blocks), encoding="utf-8")
        path.chmod(0o600)
        logger.debug("Wrote a throwaway ssh config to %s", path)
        return path


def _search(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _last_line(text: str) -> str:
    """The last non-blank line — where ssh tools put the actual complaint,
    after any multi-line warning banner."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _key_is_encrypted(path: Path) -> bool:
    """True if `path` needs a passphrase before it can be used.

    Asking ssh-keygen for the public half with an explicitly empty
    passphrase succeeds for an unlocked key and fails for a locked one.
    A file that isn't a private key at all also fails, so that case is
    picked out and reported for what it is rather than as a passphrase
    problem the user can't possibly fix.
    """
    result = subprocess.run(
        ["ssh-keygen", "-y", "-P", "", "-f", str(path)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        return False

    # "incorrect passphrase supplied to decrypt private key" is the one
    # failure that means "locked". Anything else is a key ssh itself would
    # refuse too (wrong file, world-readable, corrupt), and saying so beats
    # sending the user hunting for a passphrase that doesn't exist.
    complaint = _last_line(result.stderr)
    if "passphrase" in complaint.lower():
        return True
    if "bad permissions" in complaint.lower():
        raise SshSetupError(
            f"{path} is readable by other users, so ssh will refuse it. Fix it with "
            f"`chmod 600 {path}`."
        )
    raise SshSetupError(
        f"{path} isn't a usable private key ({complaint}). Point at the private key "
        "itself — not the .pub file, and not a certificate."
    )


def _fingerprint(path: Path) -> str | None:
    """SHA256 fingerprint of a key, in the form `ssh-add -l` prints.

    Tries the `.pub` file first: the public half of an encrypted PEM-format
    key can't be read out of the private file, but the `.pub` beside it can.
    """
    for candidate in (Path(f"{path}.pub"), path):
        if not candidate.is_file():
            continue
        result = subprocess.run(
            ["ssh-keygen", "-lf", str(candidate)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return _search(r"(SHA256:\S+)", result.stdout)
    return None


def _agent_fingerprints() -> set[str]:
    """Fingerprints of the keys in the user's own agent (empty if none)."""
    if not os.environ.get("SSH_AUTH_SOCK"):
        return set()
    result = subprocess.run(
        ["ssh-add", "-l"], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if result.returncode != 0:
        return set()
    return {fp for line in result.stdout.splitlines() if (fp := _search(r"(SHA256:\S+)", line))}
