"""Offline tests for `gitlab_fetch.cloning`.

The URL/target logic is pure and tested directly. `GitCloner` is driven
against a **fake `git`** on PATH (see `fake_git`), so the fallback from
GitLab to gitolite, the retry bookkeeping and the cleanup of half-written
directories are all exercised without touching the network.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from imperial_doc_download.gitlab_fetch.cloning import (
    MIRROR_REF_NAMESPACE,
    CloneTarget,
    GitCloner,
    Submission,
    build_clone_targets,
    repo_path,
    to_gitolite_url,
)
from imperial_doc_download.labts_fetch.models import Exercise

GITLAB_URL = "git@gitlab.doc.ic.ac.uk:lab2324_autumn/pintos_17.git"
GITOLITE_URL = "gitolite@gitolite.doc.ic.ac.uk:lab2324_autumn/pintos_17.git"

REAL_GIT = shutil.which("git") or "git"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# Stands in for git, but only for `clone`: it decides whether the remote
# "exists" (anything matching $FAKE_GIT_FAIL doesn't), then clones a local
# repository built by the test instead. Every other git command is the
# real thing, so the archival work afterwards — mirroring refs, creating
# branches, checking out a revision — is exercised for real.
#
# With $FAKE_GIT_TRACE set it also dawdles and logs how many copies of
# itself were running, which is how the concurrency test sees overlap.
_FAKE_GIT = """#!/bin/sh
if [ "$1" = clone ]; then
    remote="$2"; dest="$3"
    if [ -n "$FAKE_GIT_TRACE" ]; then
        mkdir -p "$FAKE_GIT_TRACE/live"
        : > "$FAKE_GIT_TRACE/live/$$"
        ls "$FAKE_GIT_TRACE/live" | wc -l >> "$FAKE_GIT_TRACE/counts"
        sleep 0.2
        rm -f "$FAKE_GIT_TRACE/live/$$"
    fi
    if echo "$remote" | grep -qE "${FAKE_GIT_FAIL:-^$}"; then
        echo "remote: legal banner nobody reads"
        echo "fatal: Could not read from remote repository." >&2
        exit 128
    fi
    "$REAL_GIT" clone --quiet "$FAKE_GIT_ORIGIN" "$dest" || exit $?
    # Which URL was asked for, recorded where it can't disturb the clone.
    echo "$remote" > "$dest/.git/cloned-from"
    exit 0
fi
exec "$REAL_GIT" "$@"
"""


def _git(repo: Path, *args: str, **kwargs: str) -> str:
    result = subprocess.run(
        [REAL_GIT, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_IDENTITY, **kwargs},
    )
    return result.stdout.strip()


_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A repository shaped like the real ones, for the fake git to clone.

    Modelled on `pintos_17`: several commits on the default branch, a
    second branch, a LabTS-style milestone tag partway back, and a
    `refs/keep-around/*` ref pinning a commit nothing else reaches — the
    kind GitLab keeps and a plain clone silently drops.
    """
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=master")

    revisions = []
    for n in range(3):
        (repo / f"file{n}").write_text(f"content {n}", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "--quiet", "-m", f"commit {n}")
        revisions.append(_git(repo, "rev-parse", "HEAD"))

    _git(repo, "branch", "side", revisions[0])
    _git(repo, "tag", "Task_1", revisions[1])

    # An orphan commit, reachable only from a keep-around ref.
    _git(repo, "checkout", "--quiet", "--orphan", "abandoned")
    (repo / "abandoned").write_text("force-pushed away", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "abandoned work")
    orphan = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "--quiet", "master")
    _git(repo, "update-ref", f"refs/keep-around/{orphan}", orphan)
    _git(repo, "branch", "--quiet", "-D", "abandoned")

    (tmp_path / "origin-revisions").write_text("\n".join([*revisions, orphan]), encoding="utf-8")
    return repo


def _revisions(tmp_path: Path) -> list[str]:
    """`[commit0, commit1, commit2, orphan]` of the `origin` fixture."""
    return (tmp_path / "origin-revisions").read_text(encoding="utf-8").split()


@pytest.fixture
def fake_git(tmp_path: Path, origin: Path) -> Path:
    """A directory holding a fake `git`, to put at the front of PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(_FAKE_GIT, encoding="utf-8")
    git.chmod(git.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _cloner(fake_git: Path, *, fail: str = "", trace: Path | None = None, **kwargs) -> GitCloner:
    # The fake git goes in front of the real PATH, not instead of it: the
    # script still needs the usual shell utilities behind it.
    env = {
        **os.environ,
        **_GIT_IDENTITY,
        "PATH": f"{fake_git}{os.pathsep}{os.environ['PATH']}",
        "REAL_GIT": REAL_GIT,
        "FAKE_GIT_ORIGIN": str(fake_git.parent / "origin"),
        "FAKE_GIT_FAIL": fail,
        "FAKE_GIT_TRACE": str(trace) if trace else "",
    }
    return GitCloner(env, **kwargs)


def _target(
    tmp_path: Path, url: str = GITLAB_URL, submissions: tuple[Submission, ...] = ()
) -> CloneTarget:
    return CloneTarget(
        academic_year="2324",
        ssh_url=url,
        destination=tmp_path / "clone" / "pintos_17",
        submissions=submissions,
    )


def test_gitolite_url_only_rewrites_the_host_and_user() -> None:
    assert to_gitolite_url(GITLAB_URL) == GITOLITE_URL


def test_gitolite_url_declines_anything_that_isnt_a_doc_gitlab_url() -> None:
    assert to_gitolite_url("git@github.com:me/thing.git") is None
    assert to_gitolite_url("https://gitlab.doc.ic.ac.uk/lab/thing.git") is None


def test_repo_path_drops_the_host_and_the_git_suffix() -> None:
    assert repo_path(GITLAB_URL) == "lab2324_autumn/pintos_17"


def test_one_target_per_repository_however_many_milestones(tmp_path: Path) -> None:
    # A multi-milestone exercise is still a single repository -> one clone.
    exercise = _exercise("pintos", GITLAB_URL, milestones=3)
    targets = build_clone_targets({"2324": [exercise]}, tmp_path)

    assert len(targets) == 1
    assert targets[0].destination == tmp_path / "2324" / "gitlab" / "pintos_17"
    assert targets[0].exercise_names == ("pintos",)


def test_two_exercises_sharing_a_repository_collapse_into_one_target(tmp_path: Path) -> None:
    exercises = [_exercise("pintos", GITLAB_URL), _exercise("pintos_again", GITLAB_URL)]
    targets = build_clone_targets({"2324": exercises}, tmp_path)

    assert len(targets) == 1
    assert targets[0].exercise_names == ("pintos", "pintos_again")


def test_same_project_name_in_two_namespaces_gets_distinct_directories(tmp_path: Path) -> None:
    autumn = "git@gitlab.doc.ic.ac.uk:lab2324_autumn/CW1_kss22.git"
    spring = "git@gitlab.doc.ic.ac.uk:lab2324_spring/CW1_kss22.git"
    targets = build_clone_targets(
        {"2324": [_exercise("CW1", autumn), _exercise("CW1", spring)]}, tmp_path
    )

    destinations = {t.destination for t in targets}
    assert len(destinations) == 2
    assert tmp_path / "2324" / "gitlab" / "CW1_kss22" in destinations
    assert tmp_path / "2324" / "gitlab" / "lab2324_spring__CW1_kss22" in destinations


def test_exercise_without_an_ssh_url_is_skipped(tmp_path: Path) -> None:
    exercise = Exercise(
        exercise_name="no-repo",
        kind="individual",
        academic_year="2324",
        exercise_id="1",
        repository_id="2",
        labts_url="https://example.invalid",
        gitlab_url=None,
        clone_urls={},
    )
    assert build_clone_targets({"2324": [exercise]}, tmp_path) == []


def test_clones_straight_from_gitlab_when_that_works(tmp_path: Path, fake_git: Path) -> None:
    target = _target(tmp_path)
    (result,) = _cloner(fake_git).clone_all([target])

    assert result.status == "cloned"
    assert result.via == "gitlab"
    assert result.head == _revisions(tmp_path)[2]
    assert len(result.attempts) == 1
    assert (target.destination / ".git").is_dir()


def test_falls_back_to_gitolite_when_gitlab_refuses(tmp_path: Path, fake_git: Path) -> None:
    target = _target(tmp_path)
    (result,) = _cloner(fake_git, fail="gitlab").clone_all([target])

    assert result.status == "cloned"
    assert result.via == "gitolite"
    assert [a.remote for a in result.attempts] == [GITLAB_URL, GITOLITE_URL]
    cloned_from = (target.destination / ".git" / "cloned-from").read_text(encoding="utf-8")
    assert cloned_from.strip() == GITOLITE_URL


def test_gitolite_is_not_tried_when_it_is_unavailable(tmp_path: Path, fake_git: Path) -> None:
    target = _target(tmp_path)
    (result,) = _cloner(fake_git, fail="gitlab", use_gitolite=False).clone_all([target])

    assert result.status == "failed"
    assert [a.remote for a in result.attempts] == [GITLAB_URL]


def test_a_repo_no_remote_will_serve_fails_and_leaves_nothing_behind(
    tmp_path: Path, fake_git: Path
) -> None:
    target = _target(tmp_path)
    (result,) = _cloner(fake_git, fail="gitlab|gitolite").clone_all([target])

    assert result.status == "failed"
    assert len(result.attempts) == 2
    # No half-written directory left for the next run to trip over.
    assert not target.destination.exists()
    # The banner the DoC servers print must not be what gets reported.
    assert "Could not read from remote repository" in (result.error or "")


def test_a_stale_directory_is_replaced_rather_than_confusing_git(
    tmp_path: Path, fake_git: Path
) -> None:
    target = _target(tmp_path)
    target.destination.mkdir(parents=True)
    (target.destination / "leftover").write_text("junk", encoding="utf-8")

    (result,) = _cloner(fake_git).clone_all([target])

    assert result.status == "cloned"
    assert not (target.destination / "leftover").exists()


def test_every_target_is_cloned_and_results_come_back_in_order(
    tmp_path: Path, fake_git: Path
) -> None:
    targets = [
        CloneTarget(
            academic_year="2324",
            ssh_url=f"git@gitlab.doc.ic.ac.uk:lab2324_autumn/repo{n}.git",
            destination=tmp_path / "clone" / f"repo{n}",
        )
        for n in range(5)
    ]
    results = _cloner(fake_git).clone_all(targets)

    assert [r.target.ssh_url for r in results] == [t.ssh_url for t in targets]
    assert all(r.status == "cloned" for r in results)


def test_every_remote_branch_becomes_a_local_branch(tmp_path: Path, fake_git: Path) -> None:
    # Once origin is deleted, refs/remotes/origin/* is all there'd be to
    # show for a branch — so each one gets a real local branch.
    target = _target(tmp_path)
    (result,) = _cloner(fake_git).clone_all([target])

    local = _git(target.destination, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert set(local.split()) == {"master", "side"}
    assert result.local_branches == 1  # master already existed; `side` was created


def test_tags_come_across(tmp_path: Path, fake_git: Path) -> None:
    target = _target(tmp_path)
    _cloner(fake_git).clone_all([target])

    assert _git(target.destination, "tag").split() == ["Task_1"]


def test_refs_a_plain_clone_would_drop_are_mirrored(tmp_path: Path, fake_git: Path) -> None:
    # The whole point: GitLab's keep-around refs can be the only thing
    # holding a force-pushed commit, and the server is about to be deleted.
    target = _target(tmp_path)
    (result,) = _cloner(fake_git).clone_all([target])
    orphan = _revisions(tmp_path)[3]

    assert result.mirrored_refs and result.mirrored_refs > 0
    mirrored = _git(target.destination, "for-each-ref", "--format=%(refname)", MIRROR_REF_NAMESPACE)
    assert f"{MIRROR_REF_NAMESPACE}/keep-around/{orphan}" in mirrored.split()
    # And the object itself is really here, not just the ref.
    _git(target.destination, "cat-file", "-e", f"{orphan}^{{commit}}")


def test_a_submission_at_a_branch_tip_checks_out_that_branch(
    tmp_path: Path, fake_git: Path
) -> None:
    tip = _revisions(tmp_path)[2]
    target = _target(tmp_path, submissions=(Submission("Only milestone", tip),))
    (result,) = _cloner(fake_git).clone_all([target])

    assert result.branch == "master"
    assert result.checked_out_milestone == "Only milestone"
    assert result.head == tip
    _assert_on_a_branch(target.destination, "master", tip)


def test_a_submission_mid_history_is_checked_out_detached(tmp_path: Path, fake_git: Path) -> None:
    # The pintos case: an earlier milestone's revision has later commits on
    # top, so no branch points at it. Detaching there is the honest answer;
    # inventing a branch would claim something the repository never had.
    middle = _revisions(tmp_path)[1]
    target = _target(tmp_path, submissions=(Submission("PintOS Task 1 - Scheduling", middle),))
    (result,) = _cloner(fake_git).clone_all([target])

    assert result.branch is None
    assert result.checked_out_milestone == "PintOS Task 1 - Scheduling"
    assert result.head == middle
    _assert_detached_at(target.destination, middle)
    # Nothing invented: the branches are exactly the remote's.
    local = _git(target.destination, "for-each-ref", "--format=%(refname)", "refs/heads")
    assert set(local.split()) == {"refs/heads/master", "refs/heads/side"}


def test_with_several_milestones_head_lands_on_the_last_one(tmp_path: Path, fake_git: Path) -> None:
    first, middle, last = _revisions(tmp_path)[:3]
    target = _target(
        tmp_path,
        submissions=(
            Submission("Task 1", first),
            Submission("Task 2", middle),
            Submission("Task 3", last),
        ),
    )
    (result,) = _cloner(fake_git).clone_all([target])

    assert result.checked_out_milestone == "Task 3"
    assert result.branch == "master"
    _assert_on_a_branch(target.destination, "master", last)
    # The earlier submissions are still there in the history that came with it.
    _git(target.destination, "cat-file", "-e", f"{middle}^{{commit}}")


def test_a_submitted_revision_the_remote_no_longer_has_is_survivable(
    tmp_path: Path, fake_git: Path
) -> None:
    missing = "0" * 40
    target = _target(tmp_path, submissions=(Submission("Vanished", missing),))
    (result,) = _cloner(fake_git).clone_all([target])

    # The clone is still worth having; it's just left where it landed.
    assert result.status == "cloned"
    assert result.checked_out_milestone is None
    _assert_on_a_branch(target.destination, "master", _revisions(tmp_path)[2])


def _assert_on_a_branch(repo: Path, branch: str, revision: str) -> None:
    """HEAD is on `branch` at `revision` — attached, not detached."""
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == branch
    assert _git(repo, "rev-parse", "HEAD") == revision


def _assert_detached_at(repo: Path, revision: str) -> None:
    """HEAD is detached, sitting exactly at `revision`."""
    assert _git(repo, "rev-parse", "HEAD") == revision
    on_branch = subprocess.run(
        [REAL_GIT, "-C", str(repo), "symbolic-ref", "--quiet", "HEAD"], capture_output=True
    )
    assert on_branch.returncode != 0, "HEAD is on a branch; expected it to be detached"


def test_the_default_is_one_clone_at_a_time(tmp_path: Path, fake_git: Path) -> None:
    trace = tmp_path / "trace"
    _cloner(fake_git, trace=trace).clone_all(_many_targets(tmp_path, 4))

    assert _peak_concurrency(trace) == 1


def test_raising_concurrency_actually_overlaps_the_clones(tmp_path: Path, fake_git: Path) -> None:
    # The step runs sequentially today, but the whole point of the async
    # plumbing is that this is the only knob that needs turning.
    trace = tmp_path / "trace"
    targets = _many_targets(tmp_path, 6)
    results = _cloner(fake_git, trace=trace, concurrency=3).clone_all(targets)

    assert all(r.status == "cloned" for r in results)
    assert [r.target.ssh_url for r in results] == [t.ssh_url for t in targets]
    assert 1 < _peak_concurrency(trace) <= 3


def _many_targets(tmp_path: Path, count: int) -> list[CloneTarget]:
    return [
        CloneTarget(
            academic_year="2324",
            ssh_url=f"git@gitlab.doc.ic.ac.uk:lab2324_autumn/repo{n}.git",
            destination=tmp_path / "clone" / f"repo{n}",
        )
        for n in range(count)
    ]


def _peak_concurrency(trace: Path) -> int:
    counts = (trace / "counts").read_text(encoding="utf-8").split()
    return max(int(c) for c in counts)


def _exercise(name: str, ssh_url: str, milestones: int = 1) -> Exercise:
    from imperial_doc_download.labts_fetch.models import Milestone

    return Exercise(
        exercise_name=name,
        kind="group",
        academic_year="2324",
        exercise_id="809",
        repository_id="1234",
        labts_url="https://example.invalid",
        gitlab_url=ssh_url,
        clone_urls={"ssh": ssh_url},
        milestones=[
            Milestone(
                id=str(n),
                name=f"Task {n}",
                year_page_label=str(n),
                submission_status="Submitted",
                submission_state="submitted",
                submission_state_raw=None,
                submitted_revision="a" * 40,
            )
            for n in range(milestones)
        ],
    )
