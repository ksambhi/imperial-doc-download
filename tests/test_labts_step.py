"""Offline tests for the `labts-fetch` step's outputs.

Drives `LabtsFetchStep.run()` end to end against `httpx.MockTransport`
wired to the committed fixtures -- no network access.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from imperial_doc_download.config import Settings
from imperial_doc_download.labts_fetch.client import LabtsClient
from imperial_doc_download.labts_fetch.step import LabtsFetchStep
from imperial_doc_download.pipeline import PipelineContext

FIXTURES = Path(__file__).parent / "fixtures" / "labts"

SIGN_IN_HTML = """<html><body>
<form id="new_user" action="/labts/users/sign_in" method="post">
<input type="hidden" name="authenticity_token" value="test-token"/>
</form></body></html>"""


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/labts/users/sign_in":
        if request.method == "POST":
            return httpx.Response(302, headers={"Location": "/labts"})
        return httpx.Response(200, text=SIGN_IN_HTML)
    # The landing page and the one year with data both serve the year
    # fixture; every other year in its dropdown serves the blank one.
    if path in ("/labts", "/labts/home/index/2324"):
        return httpx.Response(200, text=_fixture("home_year_multi_milestone.html"))
    if path.startswith("/labts/home/index/"):
        return httpx.Response(200, text=_fixture("home_year_blank.html"))
    if "/exercises/809/" in path:
        return httpx.Response(200, text=_fixture("detail_multi_milestone.html"))
    return httpx.Response(200, text=_fixture("detail_submitted.html"))


@pytest.fixture
def results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PipelineContext:
    """Run the step once against the fixtures and return its context."""
    import imperial_doc_download.labts_fetch.step as step_module

    def fake_client(username, password, **kwargs):
        kwargs.pop("delay", None)
        return LabtsClient(
            username,
            password,
            delay=0,
            base_url="https://teaching.doc.ic.ac.uk",
            transport=httpx.MockTransport(_handler),
            **kwargs,
        )

    monkeypatch.setattr(step_module, "LabtsClient", fake_client)

    ctx = PipelineContext(
        output_dir=tmp_path,
        settings=Settings(username="abc123", password="hunter2"),
    )
    LabtsFetchStep().run(ctx)
    return ctx


def test_writes_both_output_files(results: PipelineContext) -> None:
    assert (results.output_dir / "labts-list.json").is_file()
    assert (results.output_dir / "labts-list.txt").is_file()


def test_ssh_list_is_grouped_by_year_and_lists_one_url_per_repo(
    results: PipelineContext,
) -> None:
    text = (results.output_dir / "labts-list.txt").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]

    assert lines[0] == "# 2324"
    urls = [line for line in lines if not line.startswith("#")]

    # 24 year-page rows dedupe to 20 repositories -> one ssh URL each.
    # (The URLs themselves repeat here only because this stub serves the
    # same detail fixture for every exercise; real runs give 20 distinct
    # repos, and `test_ssh_list_matches_the_json` pins the correspondence.)
    assert len(urls) == 20
    assert all(u.startswith("git@gitlab.doc.ic.ac.uk:") for u in urls)
    assert all(u.endswith(".git") for u in urls)
    assert not any("git clone" in u for u in urls)


def test_ssh_list_matches_the_json(results: PipelineContext) -> None:
    data = json.loads((results.output_dir / "labts-list.json").read_text(encoding="utf-8"))
    from_json = [ex["clone_urls"]["ssh"] for year in data for ex in data[year]]

    text = (results.output_dir / "labts-list.txt").read_text(encoding="utf-8")
    from_txt = [line for line in text.splitlines() if line and not line.startswith("#")]

    assert from_json == from_txt


def test_results_are_stashed_for_the_later_clone_step(results: PipelineContext) -> None:
    stashed = results.state["labts_exercises"]
    assert set(stashed) == {"2324"}
    assert len(stashed["2324"]) == 20


def test_a_second_run_reuses_the_repository_list_without_refetching(
    results: PipelineContext,
) -> None:
    # No mock transport this time: if the step tried to talk to LabTS it
    # would attempt a real request, and it has no credentials to do it with.
    ctx = PipelineContext(output_dir=results.output_dir, settings=Settings())
    LabtsFetchStep().run(ctx)

    assert len(ctx.state["labts_exercises"]["2324"]) == 20


def test_reusing_the_list_round_trips_every_field(results: PipelineContext) -> None:
    ctx = PipelineContext(output_dir=results.output_dir, settings=Settings())
    LabtsFetchStep().run(ctx)

    before = results.state["labts_exercises"]["2324"]
    after = ctx.state["labts_exercises"]["2324"]
    assert [e.to_dict() for e in after] == [e.to_dict() for e in before]


def test_force_refetches_even_when_the_list_is_already_there(
    results: PipelineContext,
) -> None:
    ctx = PipelineContext(output_dir=results.output_dir, settings=Settings())

    # Forcing means going back to LabTS, which needs credentials — so the
    # step must complain about those rather than quietly reusing the file.
    with pytest.raises(RuntimeError, match="IMPERIAL_USERNAME"):
        LabtsFetchStep(force=True).run(ctx)


def test_an_unreadable_list_is_refetched_rather_than_trusted(
    results: PipelineContext,
) -> None:
    (results.output_dir / "labts-list.json").write_text("{not json", encoding="utf-8")
    ctx = PipelineContext(output_dir=results.output_dir, settings=Settings())

    with pytest.raises(RuntimeError, match="IMPERIAL_USERNAME"):
        LabtsFetchStep().run(ctx)
