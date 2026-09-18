"""Working out what to clone, where to, and actually running `git clone`.

Two remotes are tried per repository, in order:

1. the GitLab ssh URL LabTS gave us, straight from `gitlab.doc.ic.ac.uk`;
2. failing that, the same path on `gitolite.doc.ic.ac.uk` behind the DoC
   firewall — older years' repositories are no longer served by GitLab but
   the gitolite copy is still there.

Both go through the throwaway ssh config built by `ssh.py`, which is what
makes the second one (proxy-jumped through a shell server, with a
different key) work without the user having any of it in `~/.ssh/config`.

These are archival copies of repositories that are about to be deleted, so
a plain `git clone` isn't enough. After cloning, each repository is
finished off in three steps:

1. **Mirror every ref the server has.** A clone fetches `refs/heads/*` and
   `refs/tags/*` and nothing else, but DoC's GitLab also carries hundreds
   of `refs/keep-around/*` (commits it pins deliberately — force-pushed
   work, commits referenced from merge requests) and `refs/merge-requests/*`.
   Those can point at objects no branch or tag reaches, so they'd be gone
   for good. They're fetched into `refs/imperial-mirror/*`.
2. **Give every remote branch a local branch**, so the repository still
   looks complete once `origin` no longer exists.
3. **Check out the submitted revision**, on a branch where one points at
   it and detached where none does.

Cloning is **sequential by default** (`concurrency=1`), but the runner is
written as async subprocesses behind a semaphore, so turning it parallel
later is a matter of raising that number — the work is I/O-bound on the
network, which is exactly what asyncio is for.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from imperial_doc_download.gitlab_fetch.ssh import GITLAB_HOST, GITOLITE_HOST
from imperial_doc_download.labts_fetch.models import Exercise

logger = logging.getLogger(__name__)

_GITLAB_SSH_PREFIX = f"git@{GITLAB_HOST}:"
_GITOLITE_SSH_PREFIX = f"gitolite@{GITOLITE_HOST}:"

#: Subdirectory each year's GitLab repositories are cloned into, i.e.
#: `<output_dir>/<year>/gitlab/<repo>` — leaving room for the other
#: systems' data to sit alongside it under the same year.
_SYSTEM_DIRNAME = "gitlab"

#: Generous, but bounded: a big repository over a proxy-jumped link is
#: slow, while a hung connection must not stall the whole run forever.
DEFAULT_CLONE_TIMEOUT = 600.0

#: Where refs a normal clone ignores are parked. Deliberately outside
#: `refs/heads` and `refs/tags` so they can't be mistaken for the
#: repository's own branches, while still being ordinary refs that keep
#: their objects alive and show up under `git log --all`.
MIRROR_REF_NAMESPACE = "refs/imperial-mirror"


def to_gitolite_url(ssh_url: str) -> str | None:
    """Rewrite a GitLab ssh clone URL to its gitolite equivalent.

    `git@gitlab.doc.ic.ac.uk:lab2324_autumn/pintos_17.git`
    -> `gitolite@gitolite.doc.ic.ac.uk:lab2324_autumn/pintos_17.git`

    Returns `None` for anything that isn't a DoC GitLab ssh URL, since
    there's then no telling what the gitolite path would be.
    """
    if not ssh_url.startswith(_GITLAB_SSH_PREFIX):
        return None
    return _GITOLITE_SSH_PREFIX + ssh_url[len(_GITLAB_SSH_PREFIX) :]


def repo_path(ssh_url: str) -> str:
    """The `namespace/project` part of an ssh clone URL, without `.git`."""
    _, _, path = ssh_url.partition(":")
    return path.removesuffix(".git")


@dataclass(frozen=True)
class Submission:
    """A revision LabTS recorded as submitted for one milestone."""

    milestone: str
    revision: str


@dataclass(frozen=True)
class CloneTarget:
    """One repository to clone, and where it should end up."""

    academic_year: str
    ssh_url: str
    destination: Path
    #: Every exercise LabTS listed against this repository. Usually one,
    #: but a multi-milestone exercise is still a single repository and two
    #: exercises occasionally share one.
    exercise_names: tuple[str, ...] = ()
    #: Submitted revisions, in milestone order. A multi-milestone exercise
    #: has several, all in this one repository.
    submissions: tuple[Submission, ...] = ()

    @property
    def gitolite_url(self) -> str | None:
        return to_gitolite_url(self.ssh_url)

    @property
    def label(self) -> str:
        return f"{self.academic_year}/{self.destination.name}"


@dataclass
class CloneAttempt:
    """What happened when one remote was tried."""

    remote: str
    ok: bool
    returncode: int | None
    output: str

    def to_dict(self) -> dict[str, object]:
        return {
            "remote": self.remote,
            "ok": self.ok,
            "returncode": self.returncode,
            "output": self.output,
        }


@dataclass
class CloneResult:
    """The outcome for one repository, and how it got there."""

    target: CloneTarget
    status: str  # "cloned" | "reused" | "failed"
    remote: str | None = None
    head: str | None = None
    attempts: list[CloneAttempt] = field(default_factory=list)
    #: Branch HEAD was left on, or `None` when the submitted revision had
    #: no branch at it and HEAD is detached there (see `_checkout_submission`).
    branch: str | None = None
    #: The milestone whose revision is checked out, or `None` if the
    #: repository was left exactly as it cloned.
    checked_out_milestone: str | None = None
    #: How many local branches were created from remote-tracking ones.
    local_branches: int = 0
    #: How many refs a plain clone would have missed were mirrored, or
    #: `None` if mirroring them failed (the clone itself is still good).
    mirrored_refs: int | None = None

    @property
    def via(self) -> str:
        """Which route worked: `gitlab`, `gitolite`, or nothing yet."""
        if self.remote is None:
            return "—"
        return "gitolite" if self.remote.startswith(_GITOLITE_SSH_PREFIX) else "gitlab"

    @property
    def checkout_summary(self) -> str:
        """Where the working tree was left, in words."""
        if self.branch:
            return self.branch
        if self.checked_out_milestone:
            return f"{(self.head or '')[:8] or 'the submitted revision'} (detached)"
        return "the default branch"

    @property
    def error(self) -> str | None:
        """Last remote's error output, for reporting a failure."""
        if self.status != "failed" or not self.attempts:
            return None
        return self.attempts[-1].output

    def to_dict(self) -> dict[str, object]:
        return {
            "academic_year": self.target.academic_year,
            "exercise_names": list(self.target.exercise_names),
            "ssh_url": self.target.ssh_url,
            "path": str(self.target.destination),
            "status": self.status,
            "remote": self.remote,
            "via": self.via,
            "head": self.head,
            "branch": self.branch,
            "checked_out_milestone": self.checked_out_milestone,
            "local_branches": self.local_branches,
            "mirrored_refs": self.mirrored_refs,
            "attempts": [a.to_dict() for a in self.attempts],
        }


def build_clone_targets(
    exercises_by_year: Mapping[str, list[Exercise]], output_dir: Path
) -> list[CloneTarget]:
    """Turn the LabTS output into one `CloneTarget` per repository.

    A repository is cloned exactly once no matter how many milestones (or
    exercises) LabTS lists against it — `pintos` with its three tasks is
    one repository, so one clone.
    """
    targets: list[CloneTarget] = []

    for year, exercises in exercises_by_year.items():
        # Group by clone URL: that, not the exercise, is what a clone is.
        by_url: dict[str, list[Exercise]] = {}
        for exercise in exercises:
            ssh_url = exercise.clone_urls.get("ssh")
            if not ssh_url:
                logger.warning(
                    "%s: exercise %r has no ssh clone URL — skipping it.",
                    year,
                    exercise.exercise_name,
                )
                continue
            by_url.setdefault(ssh_url, []).append(exercise)

        year_dir = output_dir / year / _SYSTEM_DIRNAME
        used: dict[str, str] = {}  # directory name -> the URL that claimed it

        for ssh_url, group in by_url.items():
            path = repo_path(ssh_url)
            name = path.rpartition("/")[2] or path
            if used.get(name, ssh_url) != ssh_url:
                # Two different repositories in one year with the same
                # project name (e.g. the same exercise run in both terms):
                # qualify with the GitLab namespace so neither is lost.
                name = path.replace("/", "__")
            used[name] = ssh_url

            targets.append(
                CloneTarget(
                    academic_year=year,
                    ssh_url=ssh_url,
                    destination=year_dir / name,
                    exercise_names=tuple(e.exercise_name for e in group),
                    submissions=_submissions_of(group),
                )
            )

    return targets


def _submissions_of(exercises: list[Exercise]) -> tuple[Submission, ...]:
    """Every submitted revision in this repository, in milestone order.

    Deduplicated by revision: two milestones (or two exercises sharing the
    repository) submitted at the same commit need only one branch.
    """
    submissions: dict[str, Submission] = {}
    for exercise in exercises:
        for milestone in exercise.milestones:
            revision = milestone.submitted_revision
            if revision and revision not in submissions:
                submissions[revision] = Submission(milestone.name, revision)
    return tuple(submissions.values())


class GitCloner:
    """Clones repositories with `git`, trying GitLab then gitolite.

    `concurrency` is 1 for now (clone one repo at a time, in order). The
    plumbing is async so that raising it is the only change needed to fan
    out — see the module docstring.
    """

    def __init__(
        self,
        env: Mapping[str, str],
        *,
        use_gitolite: bool = True,
        concurrency: int = 1,
        timeout: float = DEFAULT_CLONE_TIMEOUT,
    ) -> None:
        self._env = dict(env)
        self._use_gitolite = use_gitolite
        self._concurrency = max(1, concurrency)
        self._timeout = timeout

    def clone_all(self, targets: list[CloneTarget]) -> list[CloneResult]:
        """Clone every target, blocking until they're all done."""
        return asyncio.run(self._clone_all(targets))

    async def _clone_all(self, targets: list[CloneTarget]) -> list[CloneResult]:
        semaphore = asyncio.Semaphore(self._concurrency)

        async def one(index: int, target: CloneTarget) -> CloneResult:
            async with semaphore:
                logger.info("[%d/%d] Cloning %s", index, len(targets), target.label)
                return await self._clone_one(target)

        return await asyncio.gather(*(one(i, t) for i, t in enumerate(targets, start=1)))

    async def _clone_one(self, target: CloneTarget) -> CloneResult:
        result = CloneResult(target=target, status="failed")

        for remote in self._remotes_for(target):
            # A failed attempt can leave a half-written directory behind;
            # git also refuses to clone into a non-empty one.
            _clear(target.destination)
            target.destination.parent.mkdir(parents=True, exist_ok=True)

            returncode, output = await self._run_git("clone", remote, str(target.destination))
            attempt = CloneAttempt(
                remote=remote, ok=returncode == 0, returncode=returncode, output=output
            )
            result.attempts.append(attempt)

            if attempt.ok:
                result.status = "cloned"
                result.remote = remote
                await self._archive(target, result)
                result.head = await self._head_of(target.destination)
                logger.info(
                    "Cloned %s via %s — %d branch(es), %s extra ref(s), HEAD on %s",
                    target.label,
                    result.via,
                    result.local_branches,
                    "?" if result.mirrored_refs is None else result.mirrored_refs,
                    result.checkout_summary,
                )
                return result

            logger.warning(
                "Clone of %s from %s failed (exit %s): %s",
                target.label,
                remote,
                returncode,
                _first_useful_line(output),
            )

        _clear(target.destination)
        logger.error("Could not clone %s from any remote.", target.label)
        return result

    async def _archive(self, target: CloneTarget, result: CloneResult) -> None:
        """Turn a fresh clone into a standalone archive of the repository.

        Everything here is best-effort: a repository that clones but can't
        be tidied up is still worth keeping, so failures are logged and
        recorded rather than thrown.
        """
        repo = target.destination
        result.mirrored_refs = await self._mirror_extra_refs(repo, target.label)
        result.local_branches = await self._materialise_branches(repo, target.label)
        await self._checkout_submission(repo, target, result)

    async def _mirror_extra_refs(self, repo: Path, label: str) -> int | None:
        """Fetch every ref the server has, including the ones clone skips.

        GitLab's `refs/keep-around/*` can be the only thing keeping a
        force-pushed commit alive; once the server is deleted, whatever
        wasn't fetched is gone. So mirror the lot into a namespace of our
        own and let git's reachability keep the objects.
        """
        returncode, output = await self._run_git(
            "-C",
            str(repo),
            "fetch",
            "--quiet",
            "origin",
            f"+refs/*:{MIRROR_REF_NAMESPACE}/*",
        )
        if returncode != 0:
            logger.warning(
                "Could not mirror the extra refs of %s: %s", label, _first_useful_line(output)
            )
            return None

        returncode, output = await self._run_git(
            "-C", str(repo), "for-each-ref", "--format=%(refname)", MIRROR_REF_NAMESPACE
        )
        return len(output.splitlines()) if returncode == 0 else None

    async def _materialise_branches(self, repo: Path, label: str) -> int:
        """Give every remote branch a local branch of the same name.

        A clone leaves them as `refs/remotes/origin/*`, which is fine while
        the remote exists. These are archives of repositories that are
        about to be deleted, so the branches should stand on their own.

        Written in one `update-ref` batch rather than N `git branch` calls:
        `pintos` alone has 35 branches.
        """
        remote_branches = await self._refs(repo, "refs/remotes/origin")
        local_branches = await self._refs(repo, "refs/heads")

        creates = [
            f"create refs/heads/{name} {sha}"
            for name, sha in (
                (ref.removeprefix("refs/remotes/origin/"), sha)
                for ref, sha in remote_branches.items()
            )
            # refs/remotes/origin/HEAD is a symbolic ref to the default
            # branch, not a branch of its own. A local branch that already
            # exists (the one clone checked out) wins.
            if name != "HEAD" and f"refs/heads/{name}" not in local_branches
        ]
        if not creates:
            return 0

        returncode, output = await self._run_git(
            "-C", str(repo), "update-ref", "--stdin", stdin_text="\n".join(creates) + "\n"
        )
        if returncode != 0:
            logger.warning(
                "Could not create local branches in %s: %s", label, _first_useful_line(output)
            )
            return 0
        return len(creates)

    async def _checkout_submission(
        self, repo: Path, target: CloneTarget, result: CloneResult
    ) -> None:
        """Leave the working tree at the revision LabTS recorded as submitted.

        Where a branch already points at that revision — the usual case,
        since the submission is normally the tip of `master` — that branch
        is checked out, so the repository sits on a branch rather than a
        bare commit. Where no branch is at that revision (an earlier
        milestone of a multi-milestone exercise has later work on top),
        the revision is checked out directly and HEAD is detached, which
        is the honest representation: nothing branched there.

        With several milestones HEAD ends up at the last one's revision —
        the most complete piece of work. The earlier ones stay reachable
        through their LabTS tags and the full history.
        """
        for submission in reversed(target.submissions):
            if not await self._has_commit(repo, submission, target.label):
                continue

            branch = await self._branch_at(repo, submission.revision)
            returncode, output = await self._run_git(
                "-C", str(repo), "checkout", "--quiet", branch or submission.revision
            )
            if returncode != 0:
                logger.warning(
                    "%s: could not check out %s: %s",
                    target.label,
                    branch or submission.revision[:12],
                    _first_useful_line(output),
                )
                return

            result.branch = branch
            result.checked_out_milestone = submission.milestone
            return

    async def _has_commit(self, repo: Path, submission: Submission, label: str) -> bool:
        returncode, _ = await self._run_git(
            "-C", str(repo), "cat-file", "-e", f"{submission.revision}^{{commit}}"
        )
        if returncode != 0:
            # The revision LabTS recorded isn't in what the remote served:
            # a rewritten history, or a gitolite copy predating it.
            logger.warning(
                "%s: submitted revision %s (%s) isn't in the clone.",
                label,
                submission.revision[:12],
                submission.milestone,
            )
            return False
        return True

    async def _branch_at(self, repo: Path, revision: str) -> str | None:
        """A local branch whose tip is exactly `revision`, if there is one."""
        for ref, sha in (await self._refs(repo, "refs/heads")).items():
            if sha == revision:
                return ref.removeprefix("refs/heads/")
        return None

    async def _refs(self, repo: Path, namespace: str) -> dict[str, str]:
        """`{full ref name: object id}` for everything under `namespace`.

        Deliberately not `%(refname:short)`: git shortens names to whatever
        is unambiguous *right now*, so `refs/remotes/origin/HEAD` comes back
        as plain `origin` — which, taken as a branch name, produces a junk
        `origin` branch in every repository. Full names don't move.
        """
        returncode, output = await self._run_git(
            "-C", str(repo), "for-each-ref", "--format=%(refname) %(objectname)", namespace
        )
        if returncode != 0:
            return {}
        refs = {}
        for line in output.splitlines():
            name, _, sha = line.partition(" ")
            if name and sha:
                refs[name] = sha
        return refs

    def _remotes_for(self, target: CloneTarget) -> list[str]:
        remotes = [target.ssh_url]
        gitolite = target.gitolite_url
        if self._use_gitolite and gitolite is not None:
            remotes.append(gitolite)
        return remotes

    async def _head_of(self, repo: Path) -> str | None:
        returncode, output = await self._run_git("-C", str(repo), "rev-parse", "HEAD")
        # An empty repository has no HEAD; that's not an error worth raising.
        return output.strip() if returncode == 0 else None

    async def _run_git(self, *args: str, stdin_text: str | None = None) -> tuple[int | None, str]:
        """Run `git` with our ssh config, returning `(returncode, output)`."""
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            env=self._env,
            stdin=asyncio.subprocess.PIPE if stdin_text else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        payload = stdin_text.encode() if stdin_text else None
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(payload), timeout=self._timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return None, f"Timed out after {self._timeout:g}s"
        return process.returncode, stdout.decode(errors="replace").strip()


def _clear(path: Path) -> None:
    """Remove `path` if it exists, so a clone into it can start clean."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _first_useful_line(output: str) -> str:
    """The most informative line of git/ssh output, for a one-line log.

    DoC's servers print a multi-line legal banner before anything else, so
    the first line is never the interesting one.
    """
    noise = ("warning:", "cloning into", "remote:")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        lowered = line.lower()
        if lowered.startswith(("fatal:", "error:")) or "denied" in lowered:
            return line
    for line in lines:
        if not line.lower().startswith(noise):
            return line
    return lines[-1] if lines else "no output"
