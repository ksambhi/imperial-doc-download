"""Tests for the throwaway SSH environment.

Real keys are generated with `ssh-keygen` (fast, and the alternative —
faking the key format — would test nothing useful), but nothing here ever
connects to anything. The point is to pin the two properties that matter:
the generated config says what it should, and the user's own
`~/.ssh/config` is never involved.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from imperial_doc_download.gitlab_fetch.ssh import (
    SHELL_SERVERS,
    SshEnvironment,
    SshSetupError,
)

pytestmark = pytest.mark.skipif(
    not (shutil.which("ssh-keygen") and shutil.which("ssh-agent") and shutil.which("ssh-add")),
    reason="needs OpenSSH's ssh-keygen/ssh-agent/ssh-add",
)


def _keygen(path: Path, passphrase: str = "") -> Path:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-C", "test", "-f", str(path)],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    return path


@pytest.fixture
def keys(tmp_path: Path) -> tuple[Path, Path]:
    return _keygen(tmp_path / "gitlab_key"), _keygen(tmp_path / "doc_key")


def test_config_routes_gitolite_through_the_jump_host(keys: tuple[Path, Path]) -> None:
    gitlab_key, doc_key = keys
    with SshEnvironment(
        gitlab_key=gitlab_key, doc_key=doc_key, username="abc123", jump_host="shell9.example"
    ) as ssh:
        config = ssh.config_path.read_text(encoding="utf-8")

    assert "Host gitlab.doc.ic.ac.uk" in config
    assert "Host gitolite.doc.ic.ac.uk" in config
    assert "ProxyJump imperial-doc-jump" in config
    # The jump host is reached as the user, with the *shell server* key...
    assert "HostName shell9.example" in config
    assert "User abc123" in config
    assert f"IdentityFile {doc_key}" in config
    # ...while gitolite itself is authenticated with the GitLab key.
    assert config.count(f"IdentityFile {gitlab_key}") == 2


def test_config_never_hangs_and_never_silently_trusts_a_changed_host(
    keys: tuple[Path, Path],
) -> None:
    gitlab_key, doc_key = keys
    with SshEnvironment(gitlab_key=gitlab_key, doc_key=doc_key, username="abc123") as ssh:
        config = ssh.config_path.read_text(encoding="utf-8")

    assert "BatchMode yes" in config
    assert "StrictHostKeyChecking accept-new" in config


def test_the_users_own_ssh_config_is_not_read_or_written(keys: tuple[Path, Path]) -> None:
    gitlab_key, doc_key = keys
    with SshEnvironment(gitlab_key=gitlab_key, doc_key=doc_key, username="abc123") as ssh:
        # -F tells ssh to use this file *instead of* ~/.ssh/config, and
        # nothing pulls the user's config back in behind our backs.
        assert ssh.git_ssh_command.startswith("ssh -F ")
        assert str(ssh.config_path) in ssh.git_ssh_command
        assert "Include" not in ssh.config_path.read_text(encoding="utf-8")
        assert ssh.env["GIT_SSH_COMMAND"] == ssh.git_ssh_command
        assert ssh.env["GIT_TERMINAL_PROMPT"] == "0"


def test_a_random_shell_server_is_picked_when_none_is_given(keys: tuple[Path, Path]) -> None:
    gitlab_key, doc_key = keys
    with SshEnvironment(gitlab_key=gitlab_key, doc_key=doc_key, username="abc123") as ssh:
        assert ssh.jump_host in SHELL_SERVERS


def test_everything_it_created_is_cleaned_up(keys: tuple[Path, Path]) -> None:
    gitlab_key, doc_key = keys
    with SshEnvironment(gitlab_key=gitlab_key, doc_key=doc_key, username="abc123") as ssh:
        tempdir = ssh.config_path.parent
        assert tempdir.is_dir()
    assert not tempdir.exists()


def test_gitolite_is_unsupported_without_a_doc_key_or_username(keys: tuple[Path, Path]) -> None:
    gitlab_key, doc_key = keys

    with SshEnvironment(gitlab_key=gitlab_key) as ssh:
        assert not ssh.supports_gitolite
        config = ssh.config_path.read_text(encoding="utf-8")
        assert "Host gitolite.doc.ic.ac.uk" not in config
        assert "ProxyJump" not in config

    with SshEnvironment(gitlab_key=gitlab_key, doc_key=doc_key, username=None) as ssh:
        assert not ssh.supports_gitolite


def test_a_missing_key_is_reported_with_the_env_var_that_named_it(tmp_path: Path) -> None:
    with pytest.raises(SshSetupError, match="IMPERIAL_GITLAB_SSH_KEY"):
        with SshEnvironment(gitlab_key=tmp_path / "nope"):
            pass


def test_pointing_at_a_public_key_by_mistake_says_so(tmp_path: Path) -> None:
    key = _keygen(tmp_path / "key")
    public = Path(f"{key}.pub")
    public.chmod(0o600)  # else ssh complains about the permissions first

    with pytest.raises(SshSetupError, match="not the .pub file"):
        with SshEnvironment(gitlab_key=public):
            pass


def test_a_world_readable_key_is_reported_as_such(tmp_path: Path) -> None:
    # ssh refuses these outright, so catching it here (with the fix) beats
    # every clone failing with a permissions warning buried in git output.
    key = _keygen(tmp_path / "key", passphrase="secret")
    key.chmod(0o644)

    with pytest.raises(SshSetupError, match="chmod 600"):
        with SshEnvironment(gitlab_key=key):
            pass


def test_a_passphrase_protected_key_is_unlocked_into_a_private_agent(tmp_path: Path) -> None:
    key = _keygen(tmp_path / "locked_key", passphrase="correct horse battery staple")

    with SshEnvironment(
        gitlab_key=key, gitlab_key_passphrase="correct horse battery staple"
    ) as ssh:
        config = ssh.config_path.read_text(encoding="utf-8")
        env = ssh.env
        sock = env["SSH_AUTH_SOCK"]
        # The config points at our agent, not whatever the user has running.
        assert f"IdentityAgent {sock}" in config

        listed = subprocess.run(
            ["ssh-add", "-l"],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        assert listed.returncode == 0, listed.stderr
        assert "test" in listed.stdout  # the key's comment

    # The passphrase itself must never reach the environment we hand git.
    assert "correct horse battery staple" not in str(env)


def test_a_wrong_passphrase_fails_loudly_rather_than_prompting(tmp_path: Path) -> None:
    key = _keygen(tmp_path / "locked_key", passphrase="the right one")

    with pytest.raises(SshSetupError, match="passphrase"):
        with SshEnvironment(gitlab_key=key, gitlab_key_passphrase="the wrong one"):
            pass


def test_a_locked_key_with_no_passphrase_anywhere_explains_the_options(tmp_path: Path) -> None:
    key = _keygen(tmp_path / "locked_key", passphrase="secret")

    # allow_prompt=False stands in for "running unattended": there's no
    # terminal to ask at, so it must fail with advice instead of hanging.
    with pytest.raises(SshSetupError, match="ssh-add"):
        with SshEnvironment(gitlab_key=key, allow_prompt=False):
            pass


def test_an_unencrypted_key_needs_no_agent_at_all(keys: tuple[Path, Path]) -> None:
    gitlab_key, _ = keys
    with SshEnvironment(gitlab_key=gitlab_key) as ssh:
        assert "IdentityAgent" not in ssh.config_path.read_text(encoding="utf-8")
