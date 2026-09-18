"""Offline end-to-end tests for `imperial_doc_download.emarking_fetch.step`.

The API is faked with `httpx.MockTransport` and the SOCKS proxy is stubbed
out — no ssh, no network. The fixture payload mirrors the real response
shape and the real awkward cases: a group submission uploaded by someone
else, a submission that is a git commit rather than a file, a model answer
that exists but is a 403, and an exercise belonging to another cohort.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from imperial_doc_download.config import Settings
from imperial_doc_download.emarking_fetch import step as step_module
from imperial_doc_download.emarking_fetch.client import EmarkingClient as RealEmarkingClient
from imperial_doc_download.emarking_fetch.step import (
    MANIFEST_FILENAME,
    EmarkingFetchStep,
)
from imperial_doc_download.pipeline import PipelineContext

PDF = b"%PDF-1.4\n" + b"spec" * 50
SUBMISSION = b"%PDF-1.4\n" + b"mine" * 50
FEEDBACK = b"%PDF-1.4\n" + b"feed" * 50


def _exercise(**overrides) -> dict:
    base = {
        "id": 302,
        "year": "2324",
        "module_code": "40001",
        "number": 1,
        "title": "Data representation",
        "type": "CW",
        "requires_group": False,
        "spec": "2023-06-13T04:06:12+00:00",
        "supplementary_file": None,
        "model_answer": None,
        "mark": None,
        "maximum_mark": 20,
        "pass_mark": 40,
        "weight": 50,
        "end": "2024-11-06T19:00:00+00:00",
        "extended_end": None,
        "marks_published": "2024-12-02T11:04:19.442+00:00",
        "submissions": [],
        "feedback": None,
    }
    return {**base, **overrides}


#: One year's response: four exercises of ours, one that isn't.
YEAR_2324 = [
    # Ours: a spec, a file submission, and feedback.
    _exercise(
        mark={"mark": 48, "marker": "aa9120"},
        submissions=[
            {
                "id": 5901,
                "exercise_id": 302,
                "username": "jbloggs",
                "timestamp": "2022-11-02T17:17:45Z",
                "file_path": "2324/40001/1/submissions/jbloggs/abc_cw1.pdf",
                "file_size": 0,  # the API lies about this; see plan §9.6
                "gitlab_hash": None,
                "target_submission_file_name": "cw1.pdf",
            }
        ],
        feedback={
            "id": 88127,
            "distribution_id": 2611,
            "timestamp": "2022-11-21T14:11:37Z",
            "marker": "aa9120",
        },
    ),
    # Ours: a model answer that exists but is never released (403).
    _exercise(
        id=303,
        number=2,
        title="Task 1: Framework and Labts warm-up",
        model_answer="2023-01-16T00:00:00+00:00",
        mark={"mark": 70, "marker": "aa9120"},
    ),
    # Ours: a group exercise whose submission a teammate uploaded, and
    # which is a git commit rather than a file.
    _exercise(
        id=304,
        number=3,
        title="pintos",
        requires_group=True,
        spec=None,
        mark={"mark": 65, "marker": "aa9120"},
        submissions=[
            {
                "id": 8483,
                "exercise_id": 304,
                "username": "someone-else",
                "timestamp": "2023-03-02T17:17:45Z",
                "file_path": None,
                "file_size": None,
                "gitlab_hash": "113c9ed0314d7cfc5e93b58a480284a8e0445e7f",
                "target_submission_file_name": None,
            }
        ],
    ),
    # Ours: a supplementary file, in a different module.
    _exercise(
        id=305,
        module_code="50007.1",
        number=4,
        title=None,
        spec=None,
        supplementary_file="2023-02-01T00:00:00+00:00",
        mark={"mark": 55, "marker": "aa9120"},
    ),
    # In an enrolled module, but we never touched it: no submission, no
    # feedback, no mark. The old involvement-based rule dropped these;
    # they're tutorials and optional courseworks whose specs are worth
    # having (plan §3.3.1), so the enrolment-based scope keeps them.
    _exercise(
        id=306,
        number=5,
        type="TUT",
        title="Linking and Loading",
        maximum_mark=0,
    ),
    # Not ours: another cohort's module entirely. 473 of these come back
    # in a real year and none is in our enrolment.
    _exercise(
        id=999,
        module_code="99999",
        number=99,
        title="Someone else's coursework",
    ),
]

#: The abc-api student record. Personal fields are present precisely so
#: the tests can assert none of them reaches disk.
ENROLMENT = {
    "login": "jbloggs",
    "firstname": "Joe",
    "lastname": "Bloggs",
    "cid": "01234567",
    "email": "joe.bloggs@example.ac.uk",
    "personal_tutor": "a-staff-member",
    "modules": [
        {"code": "40001", "title": "Introduction to Computer Systems", "ects": 5.0},
        {"code": "50007.1", "title": "Laboratory 2", "ects": 5.0},
        {"code": "60099", "title": "A module with no exercises", "ects": 5.0},
    ],
    # Modules helped with are a teaching role, never scope (plan §3.3.1).
    "modules_helped": [{"code": "99999", "title": "Someone else's coursework"}],
}


class FakeApi:
    """The five download endpoints plus /years and /me/{year}/exercises."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.methods: set[str] = set()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        self.methods.add(request.method)

        if path == "/years":
            return httpx.Response(200, json=["2223", "2324"])
        if path == "/2324/students":
            # The route is only ever called filtered to our own login.
            assert request.url.params.get("login") == "jbloggs"
            return httpx.Response(200, json=[ENROLMENT])
        if path == "/2223/students":
            # A year we weren't enrolled in answers 200 [] (plan §9.5).
            return httpx.Response(200, json=[])
        if path == "/me/2324/exercises":
            return httpx.Response(200, json=YEAR_2324)
        if path == "/me/2223/exercises":
            # A year we weren't here for: 500, not an empty list.
            return httpx.Response(500, text="Internal Server Error")

        if path.endswith("/spec"):
            return httpx.Response(200, content=PDF, headers=_disposition("40001_1_spec.pdf"))
        if path.endswith("/model-answer"):
            return httpx.Response(403, json={"detail": "You are not allowed to access this."})
        if path.endswith("/supplementary"):
            return httpx.Response(
                200, content=PDF, headers=_disposition("50007.1_4_supplementary.zip")
            )
        if path == "/2324/40001/exercises/1/submissions/5901/file":
            return httpx.Response(
                200, content=SUBMISSION, headers=_disposition("deadbeefuuid_cw1.pdf")
            )
        if path == "/distributions/2611/feedback/88127/file":
            # Every feedback file is served as <username>.pdf.
            return httpx.Response(200, content=FEEDBACK, headers=_disposition("jbloggs.pdf"))

        return httpx.Response(404, json={"detail": "File not found."})


def _disposition(filename: str) -> dict[str, str]:
    return {"content-disposition": f'inline; filename="{filename}"'}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    """Wire a fake API in, and stub the SSH SOCKS proxy out."""
    fake = FakeApi()

    class StubProxy:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def __enter__(self) -> StubProxy:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

        url = "socks5://127.0.0.1:1080"

    monkeypatch.setattr(step_module, "SocksProxy", StubProxy)
    monkeypatch.setattr(step_module, "EmarkingClient", _client_factory(fake.transport()))
    return fake


def _client_factory(transport: httpx.MockTransport):
    """A stand-in for `EmarkingClient` that talks to `transport` instead.

    Always built from the real class rather than from whatever
    `step_module.EmarkingClient` currently is, so a test that re-patches
    the transport isn't silently overridden by the fixture's own wrapper.
    """

    def build(*args: object, **kwargs: object) -> RealEmarkingClient:
        kwargs.pop("proxy", None)
        kwargs["transport"] = transport
        kwargs["delay"] = 0
        return RealEmarkingClient(*args, **kwargs)

    return build


def _context(tmp_path: Path) -> PipelineContext:
    return PipelineContext(
        output_dir=tmp_path,
        settings=Settings(
            username="jbloggs",
            password="hunter2",
            doc_ssh_key="/dev/null",
        ),
    )


def _manifest(tmp_path: Path) -> dict[str, list[dict]]:
    payload = json.loads((tmp_path / MANIFEST_FILENAME).read_text())
    return payload["years"]


def _records(tmp_path: Path) -> dict[str, dict]:
    return {r["url"]: r for r in _manifest(tmp_path)["2324"]}


class TestSafety:
    def test_only_ever_issues_GET(self, tmp_path: Path, api: FakeApi) -> None:
        EmarkingFetchStep().run(_context(tmp_path))
        assert api.methods == {"GET"}

    def test_never_touches_a_staff_endpoint(self, tmp_path: Path, api: FakeApi) -> None:
        # /{year}/students/{username}/exercises, /submissions/staff,
        # /marks, /missing-marks, /flat-zero-marks are other people's
        # data and other people's business (plan §1).
        EmarkingFetchStep().run(_context(tmp_path))
        for path in api.paths:
            assert "/students/" not in path
            assert "/staff" not in path
            assert not path.endswith("/marks")
            assert "missing-marks" not in path
            assert "flat-zero" not in path

    def test_never_requests_a_commit_submissions_file(self, tmp_path: Path, api: FakeApi) -> None:
        # That endpoint answers 500 for a commit submission (plan §9.2),
        # so asking would mean deliberately causing server errors.
        EmarkingFetchStep().run(_context(tmp_path))
        assert "/2324/40001/exercises/3/submissions/8483/file" not in api.paths

    def test_missing_credentials_fail_before_any_request(
        self, tmp_path: Path, api: FakeApi
    ) -> None:
        ctx = PipelineContext(output_dir=tmp_path, settings=Settings(doc_ssh_key="/dev/null"))
        with pytest.raises(RuntimeError, match="IMPERIAL_USERNAME"):
            EmarkingFetchStep().run(ctx)
        assert api.paths == []

    def test_missing_ssh_key_fails_before_any_request(self, tmp_path: Path, api: FakeApi) -> None:
        ctx = PipelineContext(
            output_dir=tmp_path,
            settings=Settings(username="jbloggs", password="hunter2"),
        )
        with pytest.raises(RuntimeError, match="IMPERIAL_DOC_SSH_KEY"):
            EmarkingFetchStep().run(ctx)
        assert api.paths == []


class TestOutputLayout:
    @pytest.fixture(autouse=True)
    def _run(self, tmp_path: Path, api: FakeApi) -> None:
        EmarkingFetchStep().run(_context(tmp_path))

    def test_spec_lands_in_the_exercise_directory(self, tmp_path: Path) -> None:
        spec = tmp_path / "2324/40001/emarking/1-Data representation/40001_1_spec.pdf"
        assert spec.read_bytes() == PDF

    def test_submission_uses_the_metadata_filename_not_the_uuid_one(self, tmp_path: Path) -> None:
        submission = tmp_path / "2324/40001/emarking/1-Data representation/submissions/5901/cw1.pdf"
        assert submission.read_bytes() == SUBMISSION

    def test_feedback_is_isolated_by_id_with_its_metadata(self, tmp_path: Path) -> None:
        directory = tmp_path / "2324/40001/emarking/1-Data representation/feedback/88127"
        assert (directory / "jbloggs.pdf").read_bytes() == FEEDBACK
        metadata = json.loads((directory / "metadata.json").read_text())
        assert metadata["distribution_id"] == 2611
        assert metadata["marker"] == "aa9120"

    def test_supplementary_gets_its_own_subdirectory(self, tmp_path: Path) -> None:
        path = tmp_path / "2324/50007.1/emarking/4/supplementary/50007.1_4_supplementary.zip"
        assert path.read_bytes() == PDF

    def test_a_title_with_a_colon_is_made_filesystem_safe(self, tmp_path: Path) -> None:
        # "Task 1: Framework and Labts warm-up" — a colon is illegal on
        # Windows and breaks macOS.
        directory = tmp_path / "2324/40001/emarking"
        names = {p.name for p in directory.iterdir() if p.is_dir()}
        assert "2-Task 1-Framework and Labts warm-up" in names

    def test_per_module_and_per_exercise_metadata_is_written(self, tmp_path: Path) -> None:
        module = json.loads((tmp_path / "2324/40001/emarking/exercises.json").read_text())
        # Includes 5, the tutorial we never touched — that's the point of
        # scoping by enrolment rather than by involvement.
        assert {e["number"] for e in module} == {1, 2, 3, 5}

        exercise = json.loads(
            (tmp_path / "2324/40001/emarking/1-Data representation/exercise.json").read_text()
        )
        assert exercise["id"] == 302

    def test_another_cohorts_exercise_is_not_written_at_all(self, tmp_path: Path) -> None:
        # 473 of these come back per year; only ours belong on disk.
        assert not list(tmp_path.glob("**/99-*"))
        assert not list(tmp_path.glob("**/*Someone else*"))


class TestManifest:
    @pytest.fixture(autouse=True)
    def _run(self, tmp_path: Path, api: FakeApi) -> None:
        EmarkingFetchStep().run(_context(tmp_path))

    def test_a_model_answer_is_recorded_but_never_requested(self, tmp_path: Path) -> None:
        # They are a 403 every time, and that is the system working:
        # releasing them would leak future years' answers. So by default
        # we record without asking (plan §9.3.1).
        record = _records(tmp_path)["/2324/40001/exercises/2/model-answer"]
        assert record["status"] == "skipped"
        assert record["path"] is None
        assert "--model-answers" in record["detail"]

    def test_a_commit_submission_is_recorded_as_skipped_with_its_hash(self, tmp_path: Path) -> None:
        record = _records(tmp_path)["/2324/40001/exercises/3/submissions/8483/file"]
        assert record["status"] == "skipped"
        assert "113c9ed0" in record["detail"]
        assert "gitlab-fetch" in record["detail"]

    def test_downloads_record_a_relative_path_and_the_real_size(self, tmp_path: Path) -> None:
        record = _records(tmp_path)["/2324/40001/exercises/1/submissions/5901/file"]
        assert record["status"] == "downloaded"
        assert not Path(record["path"]).is_absolute()
        # The metadata claimed file_size 0; the real size is what counts.
        assert record["size"] == len(SUBMISSION)

    def test_no_records_for_a_year_the_api_had_nothing_for(self, tmp_path: Path) -> None:
        assert "2223" not in _manifest(tmp_path)


class TestCachingAndForce:
    def test_a_rerun_only_asks_which_years_exist(self, tmp_path: Path, api: FakeApi) -> None:
        EmarkingFetchStep().run(_context(tmp_path))
        assert len(api.paths) > 0

        api.paths.clear()
        EmarkingFetchStep().run(_context(tmp_path))

        # /years is one cheap unauthenticated request, and a new academic
        # year could have appeared. Everything else is served from disk --
        # including the knowledge that 2223 had nothing, so it isn't
        # re-probed for another deliberate 500.
        assert api.paths == ["/years"]

    def test_an_empty_year_is_remembered(self, tmp_path: Path, api: FakeApi) -> None:
        EmarkingFetchStep().run(_context(tmp_path))
        payload = json.loads((tmp_path / MANIFEST_FILENAME).read_text())
        assert payload["empty_years"] == ["2223"]

    def test_naming_an_empty_year_explicitly_checks_it_again(
        self, tmp_path: Path, api: FakeApi
    ) -> None:
        # The escape hatch: the current academic year may gain data.
        EmarkingFetchStep().run(_context(tmp_path))
        api.paths.clear()

        EmarkingFetchStep(years=["2223"]).run(_context(tmp_path))
        assert "/2223/students" in api.paths

    def test_an_unenrolled_year_never_reaches_emarking(self, tmp_path: Path, api: FakeApi) -> None:
        # abc-api answers 200 [] for a year we weren't enrolled in, so
        # asking it first means we never provoke the 500 that
        # /me/{year}/exercises returns for those years.
        EmarkingFetchStep().run(_context(tmp_path))
        assert "/2223/students" in api.paths
        assert "/me/2223/exercises" not in api.paths

    def test_a_rerun_keeps_the_same_records(self, tmp_path: Path, api: FakeApi) -> None:
        EmarkingFetchStep().run(_context(tmp_path))
        before = _records(tmp_path)

        EmarkingFetchStep().run(_context(tmp_path))
        after = _records(tmp_path)

        assert set(before) == set(after)
        assert after["/2324/40001/exercises/1/spec"]["status"] == "cached"
        assert after["/2324/40001/exercises/2/model-answer"]["status"] == "skipped"

    def test_a_deleted_file_is_downloaded_again(self, tmp_path: Path, api: FakeApi) -> None:
        EmarkingFetchStep().run(_context(tmp_path))
        spec = tmp_path / "2324/40001/emarking/1-Data representation/40001_1_spec.pdf"
        spec.unlink()

        api.paths.clear()
        EmarkingFetchStep().run(_context(tmp_path))

        assert "/2324/40001/exercises/1/spec" in api.paths
        assert spec.read_bytes() == PDF

    def test_force_refetches_everything(self, tmp_path: Path, api: FakeApi) -> None:
        EmarkingFetchStep().run(_context(tmp_path))
        api.paths.clear()

        EmarkingFetchStep(force=True).run(_context(tmp_path))

        assert "/me/2324/exercises" in api.paths
        assert "/2324/40001/exercises/1/spec" in api.paths
        # ...but still not the model answer: --force means "redo the work",
        # not "ignore the flag that says don't ask".
        assert "/2324/40001/exercises/2/model-answer" not in api.paths

    def test_an_unreadable_manifest_is_ignored_rather_than_fatal(
        self, tmp_path: Path, api: FakeApi
    ) -> None:
        EmarkingFetchStep().run(_context(tmp_path))
        (tmp_path / MANIFEST_FILENAME).write_text("{not json")

        api.paths.clear()
        EmarkingFetchStep().run(_context(tmp_path))

        # The year cache still spares the metadata request; the downloads
        # are re-verified and found on disk.
        assert "/me/2324/exercises" not in api.paths
        assert _records(tmp_path)["/2324/40001/exercises/1/spec"]["status"] == "downloaded"


class TestYearSelection:
    def test_only_the_requested_years_are_asked_about(self, tmp_path: Path, api: FakeApi) -> None:
        EmarkingFetchStep(years=["2324"]).run(_context(tmp_path))

        assert "/years" not in api.paths
        assert "/me/2223/exercises" not in api.paths
        assert "/me/2324/exercises" in api.paths


class TestFailures:
    def test_one_failed_artefact_does_not_lose_the_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: FakeApi
    ) -> None:
        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/spec"):
                raise httpx.ConnectError("the connection dropped")
            return api.handle(request)

        monkeypatch.setattr(
            step_module, "EmarkingClient", _client_factory(httpx.MockTransport(handler))
        )
        EmarkingFetchStep().run(_context(tmp_path))

        records = _records(tmp_path)
        assert records["/2324/40001/exercises/1/spec"]["status"] == "failed"
        # The submission and feedback in the same exercise still arrived.
        assert records["/2324/40001/exercises/1/submissions/5901/file"]["status"] == "downloaded"
        assert records["/distributions/2611/feedback/88127/file"]["status"] == "downloaded"

    def test_a_failure_is_retried_on_the_next_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: FakeApi
    ) -> None:
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        fail = {"spec": True}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/spec") and fail["spec"]:
                raise httpx.ConnectError("the connection dropped")
            return api.handle(request)

        monkeypatch.setattr(
            step_module, "EmarkingClient", _client_factory(httpx.MockTransport(handler))
        )
        EmarkingFetchStep().run(_context(tmp_path))
        assert _records(tmp_path)["/2324/40001/exercises/1/spec"]["status"] == "failed"

        fail["spec"] = False
        EmarkingFetchStep().run(_context(tmp_path))
        assert _records(tmp_path)["/2324/40001/exercises/1/spec"]["status"] == "downloaded"


async def _no_sleep(_seconds: float) -> None:
    return None


class TestEnrolmentScope:
    """The scope change: enrolment, not involvement (plan §3.3.1)."""

    def test_an_exercise_we_never_touched_is_still_downloaded(
        self, tmp_path: Path, api: FakeApi
    ) -> None:
        # The whole point: a tutorial in a module we took, with no
        # submission and no mark, still has a spec worth keeping.
        spec = tmp_path / "2324/40001/emarking/5-Linking and Loading/40001_1_spec.pdf"
        EmarkingFetchStep().run(_context(tmp_path))
        assert spec.is_file()
        assert _records(tmp_path)["/2324/40001/exercises/5/spec"]["status"] == "downloaded"

    def test_a_module_we_were_not_enrolled_in_is_excluded(
        self, tmp_path: Path, api: FakeApi
    ) -> None:
        EmarkingFetchStep().run(_context(tmp_path))
        assert not (tmp_path / "2324/99999").exists()
        assert not any("99999" in p for p in api.paths)

    def test_modules_helped_never_becomes_scope(self, tmp_path: Path, api: FakeApi) -> None:
        # 99999 is in modules_helped. Helping teach a module is not a
        # licence to download its material.
        EmarkingFetchStep().run(_context(tmp_path))
        assert not any(r["module_code"] == "99999" for r in _records(tmp_path).values())

    def test_the_enrolment_route_is_only_ever_called_for_us(
        self, tmp_path: Path, api: FakeApi
    ) -> None:
        # The FakeApi asserts `login=jbloggs` on every call; this checks
        # the call is actually made, so that assertion isn't vacuous.
        EmarkingFetchStep().run(_context(tmp_path))
        assert "/2324/students" in api.paths

    def test_no_personal_details_from_the_enrolment_reach_disk(
        self, tmp_path: Path, api: FakeApi
    ) -> None:
        EmarkingFetchStep().run(_context(tmp_path))
        written = "\n".join(
            p.read_text(errors="ignore") for p in tmp_path.rglob("*.json") if p.is_file()
        )
        for leak in ("01234567", "joe.bloggs@example.ac.uk", "a-staff-member", "Bloggs"):
            assert leak not in written

    def test_the_cached_enrolment_holds_only_code_and_title(
        self, tmp_path: Path, api: FakeApi
    ) -> None:
        EmarkingFetchStep().run(_context(tmp_path))
        cached = json.loads((tmp_path / "2324/emarking-enrolment.json").read_text())
        assert all(set(m) == {"code", "title"} for m in cached)


class TestYearCacheScopeMarker:
    """§8.1: a cache from the old, narrower rule must not be trusted."""

    def test_a_cache_without_a_scope_marker_is_refetched(
        self, tmp_path: Path, api: FakeApi
    ) -> None:
        EmarkingFetchStep().run(_context(tmp_path))
        cache = tmp_path / "2324/emarking-exercises.json"

        # What an older version wrote: a bare list, holding only the
        # exercises we'd worked on.
        payload = json.loads(cache.read_text())["exercises"]
        cache.write_text(json.dumps([e for e in payload if e["number"] != 5]))
        api.paths.clear()

        EmarkingFetchStep().run(_context(tmp_path))

        assert "/me/2324/exercises" in api.paths
        assert json.loads(cache.read_text())["scope"] == "enrolled-modules"
        assert 5 in {e["number"] for e in json.loads(cache.read_text())["exercises"]}

    def test_a_cache_with_a_foreign_scope_marker_is_refetched(
        self, tmp_path: Path, api: FakeApi
    ) -> None:
        EmarkingFetchStep().run(_context(tmp_path))
        cache = tmp_path / "2324/emarking-exercises.json"
        payload = json.loads(cache.read_text())
        cache.write_text(json.dumps({"scope": "something-else", "exercises": payload["exercises"]}))
        api.paths.clear()

        EmarkingFetchStep().run(_context(tmp_path))
        assert "/me/2324/exercises" in api.paths


class TestModelAnswers:
    def test_the_flag_turns_them_back_on(self, tmp_path: Path, api: FakeApi) -> None:
        EmarkingFetchStep(model_answers=True).run(_context(tmp_path))

        assert "/2324/40001/exercises/2/model-answer" in api.paths
        # And the 403-is-terminal handling still applies.
        assert _records(tmp_path)["/2324/40001/exercises/2/model-answer"]["status"] == "forbidden"

    def test_off_by_default_means_no_request(self, tmp_path: Path, api: FakeApi) -> None:
        EmarkingFetchStep().run(_context(tmp_path))
        assert "/2324/40001/exercises/2/model-answer" not in api.paths
