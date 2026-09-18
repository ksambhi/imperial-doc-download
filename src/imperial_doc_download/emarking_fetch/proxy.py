"""An SSH SOCKS proxy into the DoC network, as a context manager.

`emarking-api.doc.ic.ac.uk` and `abc-api.doc.ic.ac.uk` are both inside the
departmental firewall, so every request has to go through a shell server:

    ssh -N -D 127.0.0.1:<port> <username>@shell<n>.doc.ic.ac.uk

The ssh setup itself — throwaway config, key unlocking, private agent,
`BatchMode yes` — is `imperial_doc_download.ssh`'s job (see `SshSession`);
all this module adds is the `-D` tunnel and its lifecycle.

Two details that are worth the code they cost:

- **The port is a free ephemeral one chosen at runtime**, not a hardcoded
  1080. The user may already have a proxy on the usual port, and two
  concurrent runs must not collide.
- **Entering waits for the port to actually accept a connection.** `ssh -N`
  forks and returns long before the tunnel is usable, and an ssh that
  fails *after* forking would otherwise surface as a baffling connection
  error on the first HTTP request instead of a clear one here.
"""

from __future__ import annotations

import logging
import random
import socket
import subprocess
import time
from pathlib import Path

from imperial_doc_download.ssh import SHELL_SERVERS, SshKey, SshSession, SshSetupError

logger = logging.getLogger(__name__)

#: Alias for the tunnel host inside the generated config, so it can't
#: collide with anything in the user's own setup.
_PROXY_ALIAS = "imperial-doc-socks"

#: How long to wait for `ssh -D` to start accepting connections.
_STARTUP_TIMEOUT = 45.0
_POLL_INTERVAL = 0.25


class SocksProxy(SshSession):
    """`ssh -N -D` through a DoC shell server, for the length of a `with`.

    ``with SocksProxy(key=..., username=...) as proxy:`` yields an object
    whose `url` is ready to hand to httpx as `proxy=`. The tunnel and its
    temporary directory are torn down on the way out, including when the
    body raises.
    """

    def __init__(
        self,
        *,
        key: Path,
        username: str,
        key_passphrase: str | None = None,
        jump_host: str | None = None,
        allow_prompt: bool = True,
    ) -> None:
        self._key = SshKey(Path(key).expanduser(), "IMPERIAL_DOC_SSH_KEY", key_passphrase)
        self._username = username
        self._host = jump_host or random.choice(SHELL_SERVERS)
        self._port: int | None = None
        self._process: subprocess.Popen[str] | None = None

        super().__init__([self._key], allow_prompt=allow_prompt)

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("SocksProxy must be entered before it can be used.")
        return self._port

    @property
    def url(self) -> str:
        """The `socks5://` URL to point httpx at."""
        return f"socks5://127.0.0.1:{self.port}"

    def _host_blocks(self) -> list[str]:
        return [
            f"Host {_PROXY_ALIAS}",
            f"    HostName {self._host}",
            f"    User {self._username}",
            f"    IdentityFile {self._key.path}",
            "    IdentitiesOnly yes",
            # No remote command and no tty: this connection exists only to
            # carry the tunnel, and must never land on a shell.
            "    RequestTTY no",
            "",
        ]

    def __enter__(self) -> SocksProxy:
        super().__enter__()
        try:
            self._port = _free_port()
            self._start()
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        self._stop()
        self._port = None
        super().close()

    def _start(self) -> None:
        command = [
            "ssh",
            "-F",
            str(self.config_path),
            "-N",
            "-D",
            f"127.0.0.1:{self._port}",
            _PROXY_ALIAS,
        ]
        logger.info("Opening a SOCKS proxy on 127.0.0.1:%d via %s", self._port, self._host)
        self._process = subprocess.Popen(
            command,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._wait_until_listening()
        logger.info("SOCKS proxy is up.")

    def _wait_until_listening(self) -> None:
        assert self._process is not None
        deadline = time.monotonic() + _STARTUP_TIMEOUT

        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise SshSetupError(
                    f"Couldn't open a SOCKS proxy through {self._host}: "
                    f"{self._ssh_complaint() or 'ssh exited without saying why'}"
                )
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                time.sleep(_POLL_INTERVAL)

        raise SshSetupError(
            f"A SOCKS proxy through {self._host} didn't start accepting connections "
            f"within {_STARTUP_TIMEOUT:g}s. {self._ssh_complaint()}".strip()
        )

    def _ssh_complaint(self) -> str:
        """Whatever ssh said on stderr, for the error message.

        Only read once the process has exited or is being killed — stderr
        is a pipe, so reading it while ssh is alive would block forever.
        """
        if self._process is None or self._process.stderr is None:
            return ""
        try:
            return self._process.stderr.read().strip()
        except (OSError, ValueError):  # pragma: no cover - pipe already closed
            return ""

    def _stop(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - a wedged ssh
                logger.warning("ssh didn't stop when asked; killing it.")
                self._process.kill()
                self._process.wait(timeout=5)
        for pipe in (self._process.stdout, self._process.stderr):
            if pipe is not None:
                pipe.close()
        logger.debug("SOCKS proxy through %s closed.", self._host)
        self._process = None


def _free_port() -> int:
    """An ephemeral port the OS says is free right now.

    Inherently a small race — something else could take it between the
    close here and ssh's bind — but ssh fails loudly and immediately if
    that happens, which `_wait_until_listening` turns into a clear error.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
