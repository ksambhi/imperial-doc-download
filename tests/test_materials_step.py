"""Offline end-to-end tests for `materials_fetch.step`.

The API is faked with `httpx.MockTransport` and the SOCKS proxy is stubbed
out — no ssh, no network. The fixture serves real zip bytes, so the
download and the extraction are both exercised for real.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from imperial_doc_download.config import Settings
from imperial_doc_download.materials_fetch import step as step_module
from imperial_doc_download.materials_fetch.client import MaterialsClient
from imperial_doc_download.materials_fetch.step import (
    MANIFEST_FILENAME,
    MARKER_FILENAME,
    MaterialsFetchStep,
)
from imperial_doc_download.pipeline import PipelineContext

#: Modules the enrolment cache will claim, per year.
ENROLMENT = {
    "2324": [
        {"code": "50007.2", "title": "Laboratory 2"},
        {"code": "50008", "title": "Operating Systems"},
        {"code": "50099", "title": "A module with no materials"},
    ],
    "2425": [{"code": "60001", "title": "Advanced Computer Architecture"}],
}


def _zip_bytes(module_code: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{module_code}/Written Notes/notes.pdf", b"%PDF-1.4 notes")
        archive.writestr(f"{module_code}/links.md", b"# links\n")
    return buffer.getvalue()


class FakeApi:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @property
    def asked_for(self) -> list[tuple[str, str]]:
        return [
            (r.url.params.get("year"), r.url.params.get("course"))
            for r in self.requests
            if r.url.path == "/resources/zipped"
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        course = request.url.params.get("course")
        if course == "50099":
            # A module with no materials published — 4 of the real 50.
            return httpx.Response(404, json={"detail": "No resources found"})
        body = _zip_bytes(course)
        return httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "application/zip",
                "content-disposition": f'attachment; filename="{course}_materials.zip"',
            },
        )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()

    class StubProxy:
        def __init__(self, **kwargs: object) -> None: ...
        def __enter__(self) -> StubProxy:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

        url = "socks5://127.0.0.1:1080"

    def build(*args: object, **kwargs: object) -> MaterialsClient:
        kwargs.pop("proxy", None)
        kwargs["transport"] = fake.transport()
        kwargs["delay"] = 0
        return MaterialsClient(*args, **kwargs)

    monkeypatch.setattr(step_module, "SocksProxy", StubProxy)
    monkeypatch.setattr(step_module, "MaterialsClient", build)
    return fake


@pytest.fixture
def output(tmp_path: Path) -> Path:
    """An output directory with the enrolment already cached."""
    for year, modules in ENROLMENT.items():
        (tmp_path / year).mkdir(parents=True)
        (tmp_path / year / "emarking-enrolment.json").write_text(json.dumps(modules))
    return tmp_path


def _context(output: Path) -> PipelineContext:
    return PipelineContext(
        output_dir=output,
        settings=Settings(username="jbloggs", password="pw", doc_ssh_key="/dev/null"),
    )


def _records(output: Path) -> dict[tuple[str, str], dict]:
    payload = json.loads((output / MANIFEST_FILENAME).read_text())
    return {
        (entry["year"], entry["module_code"]): entry
        for entries in payload["years"].values()
        for entry in entries
    }


class TestSafety:
    def test_only_ever_issues_GET(self, output: Path, api: FakeApi) -> None:
        MaterialsFetchStep().run(_context(output))
        assert {r.method for r in api.requests} == {"GET"}

    def test_there_is_no_write_helper(self) -> None:
        # materials-api has PUT /resources/{id}/file, which replaces the
        # file students download.
        for verb in ("post", "put", "patch", "delete", "request", "send"):
            assert not hasattr(MaterialsClient, verb)

    def test_only_enrolled_modules_are_requested(self, output: Path, api: FakeApi) -> None:
        MaterialsFetchStep().run(_context(output))
        assert set(api.asked_for) == {
            ("2324", "50007.2"),
            ("2324", "50008"),
            ("2324", "50099"),
            ("2425", "60001"),
        }

    def test_missing_credentials_fail_before_any_request(self, output: Path, api: FakeApi) -> None:
        ctx = PipelineContext(output_dir=output, settings=Settings(doc_ssh_key="/dev/null"))
        with pytest.raises(RuntimeError, match="IMPERIAL_USERNAME"):
            MaterialsFetchStep().run(ctx)
        assert api.requests == []

    def test_missing_ssh_key_fails_before_any_request(self, output: Path, api: FakeApi) -> None:
        ctx = PipelineContext(output_dir=output, settings=Settings(username="j", password="p"))
        with pytest.raises(RuntimeError, match="IMPERIAL_DOC_SSH_KEY"):
            MaterialsFetchStep().run(ctx)
        assert api.requests == []


class TestOutput:
    @pytest.fixture(autouse=True)
    def _run(self, output: Path, api: FakeApi) -> None:
        MaterialsFetchStep().run(_context(output))

    def test_extracts_beside_the_modules_emarking_directory(self, output: Path) -> None:
        assert (output / "2324/50007.2/materials/Written Notes/notes.pdf").is_file()
        assert (output / "2324/50007.2/materials/links.md").is_file()
        assert (output / "2425/60001/materials/links.md").is_file()

    def test_the_redundant_module_code_level_is_gone(self, output: Path) -> None:
        assert not (output / "2324/50007.2/materials/50007.2").exists()

    def test_the_zip_is_deleted_once_extracted(self, output: Path) -> None:
        assert list(output.rglob("*.zip")) == []

    def test_a_marker_records_what_was_extracted(self, output: Path) -> None:
        marker = json.loads((output / "2324/50007.2/materials" / MARKER_FILENAME).read_text())
        assert marker["module_code"] == "50007.2"
        assert marker["module_title"] == "Laboratory 2"
        assert marker["files"] == 2
        assert marker["zip_name"] == "50007.2_materials.zip"
        assert marker["stripped_prefix"] == "50007.2"

    def test_a_module_with_no_materials_is_recorded_not_failed(self, output: Path) -> None:
        record = _records(output)[("2324", "50099")]
        assert record["status"] == "absent"
        assert not (output / "2324/50099/materials").exists()

    def test_the_manifest_records_sizes(self, output: Path) -> None:
        record = _records(output)[("2324/50007.2".split("/")[0], "50007.2")]
        assert record["status"] == "downloaded"
        assert record["files"] == 2
        assert record["extracted_bytes"] > 0


class TestKeepZip:
    def test_the_zip_is_kept_when_asked(self, output: Path, api: FakeApi) -> None:
        MaterialsFetchStep(keep_zip=True).run(_context(output))
        kept = output / "2324/50007.2/materials/50007.2_materials.zip"
        assert kept.is_file()
        # ...and it's still a valid zip, i.e. moved rather than mangled.
        with zipfile.ZipFile(kept) as archive:
            assert len(archive.namelist()) == 2


class TestCaching:
    def test_a_rerun_redownloads_nothing_it_already_has(self, output: Path, api: FakeApi) -> None:
        MaterialsFetchStep().run(_context(output))
        assert api.requests

        api.requests.clear()
        MaterialsFetchStep().run(_context(output))

        # Only the module with no materials is asked about again — see
        # `test_an_absent_module_is_retried`. Nothing already extracted
        # costs a request, which is what keeps a re-run off 1.75 GB.
        assert api.asked_for == [("2324", "50099")]
        assert _records(output)[("2324", "50007.2")]["status"] == "cached"

    def test_a_cached_run_keeps_the_extracted_tree(self, output: Path, api: FakeApi) -> None:
        MaterialsFetchStep().run(_context(output))
        MaterialsFetchStep().run(_context(output))
        assert (output / "2324/50007.2/materials/links.md").is_file()

    def test_force_redownloads(self, output: Path, api: FakeApi) -> None:
        MaterialsFetchStep().run(_context(output))
        api.requests.clear()

        MaterialsFetchStep(force=True).run(_context(output))

        assert ("2324", "50007.2") in api.asked_for

    def test_a_module_with_no_marker_is_fetched_again(self, output: Path, api: FakeApi) -> None:
        MaterialsFetchStep().run(_context(output))
        (output / "2324/50007.2/materials" / MARKER_FILENAME).unlink()
        api.requests.clear()

        MaterialsFetchStep().run(_context(output))

        assert ("2324", "50007.2") in api.asked_for
        assert ("2324", "50008") not in api.asked_for

    def test_an_absent_module_is_retried(self, output: Path, api: FakeApi) -> None:
        # A 404 writes no marker, so it's re-asked. That's deliberate:
        # materials get published late, unlike a graded artefact.
        MaterialsFetchStep().run(_context(output))
        api.requests.clear()
        MaterialsFetchStep().run(_context(output))
        assert ("2324", "50099") in api.asked_for


class TestScope:
    def test_year_selection(self, output: Path, api: FakeApi) -> None:
        MaterialsFetchStep(years=["2425"]).run(_context(output))
        assert {year for year, _ in api.asked_for} == {"2425"}

    def test_modules_come_from_the_pipeline_when_available(
        self, output: Path, api: FakeApi
    ) -> None:
        from imperial_doc_download.emarking_fetch.models import Module

        ctx = _context(output)
        ctx.state["emarking_modules"] = {"2526": [Module("70011", "Individual Project")]}
        MaterialsFetchStep(years=["2526"]).run(ctx)

        assert api.asked_for == [("2526", "70011")]

    def test_no_enrolment_anywhere_is_a_warning_not_a_crash(
        self, tmp_path: Path, api: FakeApi
    ) -> None:
        MaterialsFetchStep().run(_context(tmp_path))
        assert api.requests == []
        assert json.loads((tmp_path / MANIFEST_FILENAME).read_text())["years"] == {}


class TestFailures:
    def test_an_unsafe_archive_is_refused_and_its_zip_kept(
        self, output: Path, monkeypatch: pytest.MonkeyPatch, api: FakeApi
    ) -> None:
        def explode(*args: object, **kwargs: object):
            from imperial_doc_download.materials_fetch.archive import UnsafeArchiveError

            raise UnsafeArchiveError("member 'x' would be written outside")

        monkeypatch.setattr(step_module, "extract", explode)
        MaterialsFetchStep().run(_context(output))

        record = _records(output)[("2324", "50007.2")]
        assert record["status"] == "unsafe"
        assert "outside" in record["detail"]
        # The zip is evidence, and shouldn't be silently refetched.
        assert (output / "2324/50007.2/50007.2_materials.zip").is_file()

    def test_one_module_failing_does_not_lose_the_others(
        self, output: Path, monkeypatch: pytest.MonkeyPatch, api: FakeApi
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("course") == "50008":
                raise httpx.ConnectError("dropped")
            return api.handle(request)

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", no_sleep)

        def build(*args: object, **kwargs: object) -> MaterialsClient:
            kwargs.pop("proxy", None)
            kwargs["transport"] = httpx.MockTransport(handler)
            kwargs["delay"] = 0
            return MaterialsClient(*args, **kwargs)

        monkeypatch.setattr(step_module, "MaterialsClient", build)
        MaterialsFetchStep().run(_context(output))

        records = _records(output)
        assert records[("2324", "50008")]["status"] == "failed"
        assert records[("2324", "50007.2")]["status"] == "downloaded"
        assert (output / "2324/50007.2/materials/links.md").is_file()
