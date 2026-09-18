"""Shared SSH plumbing: throwaway configs, key unlocking, private agents.

Several DoC systems sit behind the departmental firewall and are only
reachable by way of a shell server — `gitlab_fetch` proxy-jumps through
one to reach gitolite, `emarking_fetch` runs a SOCKS proxy over one to
reach the eMarking API. Both need the same awkward plumbing, and neither
may touch the user's `~/.ssh/config`.

So `SshSession` owns the parts that have nothing to do with any
particular host: a per-run temporary directory, making sure every
passphrase-protected key is usable non-interactively, and writing a
throwaway `ssh_config` that gets handed to ssh as `-F <file>`. Subclasses
supply only the `Host` blocks they need, via `_host_blocks()`.

Passphrase-protected keys are handled once per run rather than once per
connection: if a locked key isn't already in the user's own agent, a
private `ssh-agent` is started for this run only, the key is unlocked
into it, and the generated config points at it via `IdentityAgent`. That
keeps everything non-interactive (`BatchMode yes`, so nothing can ever
hang on a prompt). The directory — config, agent socket and all — is
deleted when the run ends.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: The DoC shell servers reachable from outside the firewall. One is
#: picked at random per run to spread the load rather than always
#: hammering the same box. (`sftp.doc.ic.ac.uk` looks like the polite
#: choice, and is what people often alias to a shell server, but the real
#: host of that name only serves SFTP and refuses ssh key auth.)
SHELL_SERVERS = tuple(f"shell{n}.doc.ic.ac.uk" for n in range(1, 6))

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


class SshSession:
    """A self-contained SSH setup for one run, as a context manager.

    ``with SshSession([key]) as session:`` yields an object whose
    `config_path` points at a throwaway `ssh_config` and whose `env` is
    ready to hand to subprocesses. Everything it creates lives in one
    temporary directory that's removed on exit.

    Subclasses override `_host_blocks()` to add the `Host` stanzas they
    need; the shared `Host *` defaults are appended afterwards so they
    apply to everything.
    """

    def __init__(self, keys: Sequence[SshKey], *, allow_prompt: bool = True) -> None:
        self._keys = list(keys)
        self._allow_prompt = allow_prompt

        self._tempdir: Path | None = None
        self._agent: _Agent | None = None
        self._config_path: Path | None = None

    def _host_blocks(self) -> list[str]:
        """`Host` stanzas for this session, as config lines. Override me."""
        return []

    @property
    def tempdir(self) -> Path:
        if self._tempdir is None:
            raise RuntimeError(f"{type(self).__name__} must be entered before it can be used.")
        return self._tempdir

    @property
    def config_path(self) -> Path:
        if self._config_path is None:
            raise RuntimeError(f"{type(self).__name__} must be entered before it can be used.")
        return self._config_path

    @property
    def ssh_command(self) -> str:
        return f"ssh -F {shlex.quote(str(self.config_path))}"

    @property
    def env(self) -> dict[str, str]:
        """Environment for subprocesses: this run's agent, if there is one."""
        env = {**os.environ}
        if self._agent is not None:
            env["SSH_AUTH_SOCK"] = self._agent.sock
        return env

    def __enter__(self) -> SshSession:
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

    def _check_keys_exist(self) -> None:
        for key in self._keys:
            if not key.path.is_file():
                raise SshSetupError(f"No such SSH key: {key.path} (from {key.label}).")

    def _unlock_keys(self) -> None:
        """Make sure every passphrase-protected key is usable non-interactively.

        Keys the user has already loaded into their own agent are left
        alone; anything else goes into a private agent for this run.
        """
        locked = [key for key in self._keys if _key_is_encrypted(key.path)]
        if not locked:
            return

        in_user_agent = _agent_fingerprints()
        needed = [key for key in locked if _fingerprint(key.path) not in in_user_agent]
        if not needed:
            logger.info("Passphrase-protected key(s) are already in your ssh-agent.")
            return

        askpass = self.tempdir / "askpass.sh"
        askpass.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
        askpass.chmod(0o700)

        self._agent = _Agent.start()
        for index, key in enumerate(needed):
            self._agent.add(
                key,
                self._passphrase_for(key),
                askpass,
                self.tempdir / f"asked-{index}",
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
        # Asked once per run, not once per connection: the agent holds it after this.
        return getpass.getpass(f"Passphrase for {key.path} ({key.label}): ")

    def _write_config(self) -> Path:
        path = self.tempdir / "ssh_config"

        blocks = [
            "# Written by imperial-doc-download for a single run, and deleted",
            "# afterwards. Your own ~/.ssh/config is neither read nor modified.",
            "",
            *self._host_blocks(),
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
