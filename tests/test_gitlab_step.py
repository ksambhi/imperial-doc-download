"""Offline tests for the `gitlab-fetch` step's orchestration.

The SSH setup and the cloning itself are faked out here (they have their
own tests) so these can focus on what the step is actually responsible
for: finding its input, deciding what still needs cloning, and recording
what happened.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imperial_doc_download.config import Settings
from imperial_doc_download.gitlab_fetch import step as step_module
from imperial_doc_download.gitlab_fetch.cloning import CloneResult, CloneTarget
from imperial_doc_download.gitlab_fetch.step import GitlabFetchStep
from imperial_doc_download.labts_fetch.models import Exercise, Milestone
from imperial_doc_download.pipeline import PipelineContext

URLS = {
    "2324": "git@gitlab.doc.ic.ac.uk:lab2324_autumn/pintos_17.git",
    "2223": "git@gitlab.doc.ic.ac.uk:lab2223_autumn/haskellsequences_kss22.git",
}


class FakeSsh:
    """Stands in for `SshEnvironment`: no keys, no agent, no config."""

    supports_gitolite = True
    jump_host = "shell9.example"
    env: dict[str, str] = {}

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def __enter__(self) -> FakeSsh:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class FakeCloner:
    """Stands in for `GitCloner`: "clones" by making the directory."""

    #: Every batch of targets handed to a cloner, across all instances.
    calls: list[list[CloneTarget]] = []
    #: URLs that should fail instead of succeeding.
    failing: set[str] = set()

    def __init__(self, env: dict[str, str], **kwargs: object) -> None:
        self.kwargs = kwargs

    def clone_all(self, targets: list[CloneTarget]) -> list[CloneResult]:
        FakeCloner.calls.append(list(targets))
        results = []
        for target in targets:
            if target.ssh_url in FakeCloner.failing:
                results.append(CloneResult(target=target, status="failed"))
                continue
            (target.destination / ".git").mkdir(parents=True, exist_ok=True)
            results.append(
                CloneResult(target=target, status="cloned", remote=target.ssh_url, head="a" * 40)
            )
        return results


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeCloner.calls = []
    FakeCloner.failing = set()
    monkeypatch.setattr(step_module, "SshEnvironment", FakeSsh)
    monkeypatch.setattr(step_module, "GitCloner", FakeCloner)


def _exercise(year: str, name: str = "pintos") -> Exercise:
    return Exercise(
        exercise_name=name,
        kind="group",
        academic_year=year,
        exercise_id="809",
        repository_id="1234",
        labts_url="https://example.invalid",
        gitlab_url=None,
        clone_urls={"ssh": URLS[year]},
        milestones=[
            Milestone(
                id="1",
                name="Task 1",
                year_page_label="1",
                submission_status="Submitted",
                submission_state="submitted",
                submission_state_raw=None,
                submitted_revision="b" * 40,
            )
        ],
    )


def _ctx(tmp_path: Path, *, with_state: bool = True) -> PipelineContext:
    ctx = PipelineContext(
        output_dir=tmp_path,
        settings=Settings(
            username="abc123",
            gitlab_ssh_key="/keys/gitlab",
            doc_ssh_key="/keys/doc",
        ),
    )
    if with_state:
        ctx.state["labts_exercises"] = {year: [_exercise(year)] for year in ("2324", "2223")}
    return ctx


def _manifest(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "gitlab-clones.json").read_text(encoding="utf-8"))["repos"]


def test_clones_into_year_and_system_directories(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    GitlabFetchStep().run(ctx)

    assert (tmp_path / "2324" / "gitlab" / "pintos_17" / ".git").is_dir()
    assert (tmp_path / "2223" / "gitlab" / "haskellsequences_kss22" / ".git").is_dir()


def test_records_what_it_did_in_the_manifest(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    GitlabFetchStep().run(ctx)

    entries = {entry["ssh_url"]: entry for entry in _manifest(tmp_path)}
    assert set(entries) == set(URLS.values())
    entry = entries[URLS["2324"]]
    assert entry["status"] == "cloned"
    assert entry["academic_year"] == "2324"
    assert entry["exercise_names"] == ["pintos"]
    assert entry["path"].endswith("2324/gitlab/pintos_17")


def test_results_are_stashed_for_later_steps(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    GitlabFetchStep().run(ctx)

    assert len(ctx.state["gitlab_clones"]) == 2


def test_a_second_run_reuses_what_is_already_cloned(tmp_path: Path) -> None:
    GitlabFetchStep().run(_ctx(tmp_path))
    GitlabFetchStep().run(_ctx(tmp_path))

    # One batch of clones, from the first run only.
    assert len(FakeCloner.calls) == 1
    assert all(entry["status"] == "reused" for entry in _manifest(tmp_path))


def test_force_re_clones_everything(tmp_path: Path) -> None:
    GitlabFetchStep().run(_ctx(tmp_path))
    GitlabFetchStep(force=True).run(_ctx(tmp_path))

    assert len(FakeCloner.calls) == 2
    assert len(FakeCloner.calls[1]) == 2


def test_a_clone_that_was_deleted_since_is_fetched_again(tmp_path: Path) -> None:
    GitlabFetchStep().run(_ctx(tmp_path))
    # The manifest still claims it, but it isn't on disk any more.
    (tmp_path / "2324" / "gitlab" / "pintos_17" / ".git").rmdir()

    GitlabFetchStep().run(_ctx(tmp_path))

    assert [t.ssh_url for t in FakeCloner.calls[1]] == [URLS["2324"]]


def test_a_failed_clone_is_retried_on_the_next_run(tmp_path: Path) -> None:
    FakeCloner.failing = {URLS["2223"]}
    GitlabFetchStep().run(_ctx(tmp_path))

    failed = [e for e in _manifest(tmp_path) if e["status"] == "failed"]
    assert [e["ssh_url"] for e in failed] == [URLS["2223"]]

    # Nothing to reuse for that one, so the next run tries it again...
    FakeCloner.failing = set()
    GitlabFetchStep().run(_ctx(tmp_path))
    assert [t.ssh_url for t in FakeCloner.calls[1]] == [URLS["2223"]]
    # ...and the one that already worked is still recorded.
    assert {e["status"] for e in _manifest(tmp_path)} == {"cloned", "reused"}


def test_reads_the_repository_list_from_disk_when_run_on_its_own(tmp_path: Path) -> None:
    # What `imperial-doc-download gitlab` does: no LabTS step ran first,
    # so the list has to come from the earlier run's output file.
    payload = {year: [_exercise(year).to_dict()] for year in ("2324", "2223")}
    (tmp_path / "labts-list.json").write_text(json.dumps(payload), encoding="utf-8")

    ctx = _ctx(tmp_path, with_state=False)
    GitlabFetchStep().run(ctx)

    assert {t.ssh_url for t in FakeCloner.calls[0]} == set(URLS.values())


def test_says_what_to_do_when_there_is_no_repository_list_at_all(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Run the labts step first"):
        GitlabFetchStep().run(_ctx(tmp_path, with_state=False))


def test_says_what_to_do_when_no_gitlab_key_is_configured(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.settings.gitlab_ssh_key = None

    with pytest.raises(RuntimeError, match="IMPERIAL_GITLAB_SSH_KEY"):
        GitlabFetchStep().run(ctx)


def test_concurrency_and_timeout_reach_the_cloner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class RecordingCloner(FakeCloner):
        def __init__(self, env: dict[str, str], **kwargs: object) -> None:
            captured.update(kwargs)
            super().__init__(env, **kwargs)

    monkeypatch.setattr(step_module, "GitCloner", RecordingCloner)
    GitlabFetchStep(concurrency=4, timeout=12.5).run(_ctx(tmp_path))

    assert captured["concurrency"] == 4
    assert captured["timeout"] == 12.5
