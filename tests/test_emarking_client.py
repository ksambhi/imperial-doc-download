"""Offline tests for `imperial_doc_download.emarking_fetch.client`.

Faked with `httpx.MockTransport`. This suite must never reach the real
eMarking API -- it is the system coursework is submitted to and marks are
recorded in, and CI has no credentials anyway.

The statuses asserted here are the ones the live API actually returns
(`docs/emarking-fetch-plan.md` §9), not invented ones.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from imperial_doc_download.emarking_fetch.client import (
    EmarkingAuthError,
    EmarkingClient,
    EmarkingError,
)

PDF = b"%PDF-1.4\n" + b"x" * 200


class Recorder:
    """A MockTransport that remembers every request it was handed."""

    def __init__(self, handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def transport(self) -> httpx.MockTransport:
        def wrapped(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._handler(request)

        return httpx.MockTransport(wrapped)

    @property
    def methods(self) -> set[str]:
        return {r.method for r in self.requests}


def _default_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path

    if path == "/years":
        return httpx.Response(200, json=["2223", "2324"])
    if path == "/me/2324/exercises":
        return httpx.Response(200, json=[{"id": 1}])
    # 21 of 25 years answer like this rather than coming back empty.
    if path == "/me/1819/exercises":
        return httpx.Response(
            502, json={"detail": "ABC API call returned a 404: Student not found."}
        )
    if path == "/me/0203/exercises":
        return httpx.Response(500, text="Internal Server Error")

    if path.endswith("/spec"):
        return httpx.Response(
            200,
            content=PDF,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'inline; filename="40001_1_spec.pdf"',
            },
        )
    if path.endswith("/model-answer"):
        return httpx.Response(403, json={"detail": "You are not allowed to access this resource."})
    if path.endswith("/supplementary"):
        return httpx.Response(404, json={"detail": "File not found."})
    if path.endswith("/file"):
        return httpx.Response(
            200,
            content=PDF,
            headers={"content-disposition": 'inline; filename="deadbeef_cw1.pdf"'},
        )
    return httpx.Response(404, json={"detail": "File not found."})


def _client(handler=_default_handler) -> tuple[EmarkingClient, Recorder]:
    recorder = Recorder(handler)
    client = EmarkingClient(
        "jbloggs",
        "hunter2",
        delay=0,
        transport=recorder.transport(),
    )
    return client, recorder


class TestSafety:
    @pytest.mark.asyncio
    async def test_only_ever_issues_GET(self, tmp_path: Path) -> None:
        """The single most important test in this file.

        This API submits coursework, edits feedback and writes marks. If
        anything in this module ever learns another verb, this fails.
        """
        client, recorder = _client()
        async with client:
            await client.years()
            await client.exercises("2324")
            await client.download("/2324/40001/exercises/1/spec", tmp_path)
            await client.download("/2324/40001/exercises/1/model-answer", tmp_path)

        assert recorder.methods == {"GET"}
        assert recorder.requests

    def test_there_is_no_write_helper(self) -> None:
        # Belt and braces with the test above: catches a helper that was
        # added but not yet called from anywhere.
        for verb in ("post", "put", "patch", "delete", "request", "send"):
            assert not hasattr(EmarkingClient, verb), f"EmarkingClient.{verb} must not exist"

    @pytest.mark.asyncio
    async def test_credentials_are_sent_as_basic_auth_with_the_proxied_user(self) -> None:
        client, recorder = _client()
        async with client:
            await client.exercises("2324")

        request = recorder.requests[-1]
        expected = base64.b64encode(b"jbloggs:hunter2").decode()
        assert request.headers["authorization"] == f"Basic {expected}"
        assert request.headers["x-proxied-user"] == "jbloggs"

    @pytest.mark.asyncio
    async def test_years_is_requested_without_credentials(self) -> None:
        # abc-api needs none, and sending them anyway would put the
        # password on a host that never asked for it.
        client, recorder = _client()
        async with client:
            await client.years()

        assert "authorization" not in recorder.requests[0].headers

    def test_requires_a_username_and_password(self) -> None:
        with pytest.raises(ValueError):
            EmarkingClient("", "hunter2")
        with pytest.raises(ValueError):
            EmarkingClient("jbloggs", "")


class TestYearsAndExercises:
    @pytest.mark.asyncio
    async def test_years(self) -> None:
        client, _ = _client()
        async with client:
            assert await client.years() == ["2223", "2324"]

    @pytest.mark.asyncio
    async def test_a_year_with_data(self) -> None:
        client, _ = _client()
        async with client:
            assert await client.exercises("2324") == [{"id": 1}]

    @pytest.mark.parametrize("year", ["1819", "0203"])
    @pytest.mark.asyncio
    async def test_a_year_we_were_not_here_for_is_none_not_an_error(self, year: str) -> None:
        # 21 of 25 years answer 500/502 (plan §9.5). That is the normal
        # case, so it must not raise and must not fail the run.
        client, _ = _client()
        async with client:
            assert await client.exercises(year) is None

    @pytest.mark.asyncio
    async def test_an_unexpected_status_does_raise(self) -> None:
        client, _ = _client(lambda request: httpx.Response(418, text="teapot"))
        async with client:
            with pytest.raises(EmarkingError):
                await client.exercises("2324")


class TestDownload:
    @pytest.mark.asyncio
    async def test_downloads_using_the_content_disposition_name(self, tmp_path: Path) -> None:
        client, _ = _client()
        async with client:
            result = await client.download("/2324/40001/exercises/1/spec", tmp_path)

        assert result.status == "downloaded"
        assert result.path == tmp_path / "40001_1_spec.pdf"
        assert result.path.read_bytes() == PDF
        assert result.size == len(PDF)

    @pytest.mark.asyncio
    async def test_a_preferred_name_beats_the_header(self, tmp_path: Path) -> None:
        # Submissions carry an opaque uuid prefix on the wire, so the
        # metadata's target_submission_file_name is the better name.
        client, _ = _client()
        async with client:
            result = await client.download(
                "/2324/40001/exercises/1/submissions/5901/file",
                tmp_path,
                preferred_name="cw1.pdf",
            )

        assert result.path == tmp_path / "cw1.pdf"

    @pytest.mark.asyncio
    async def test_a_preferred_name_is_sanitised_too(self, tmp_path: Path) -> None:
        client, _ = _client()
        async with client:
            result = await client.download(
                "/2324/40001/exercises/1/submissions/5901/file",
                tmp_path,
                preferred_name="../../escape.pdf",
            )

        assert result.path is not None
        assert result.path.parent == tmp_path

    @pytest.mark.asyncio
    async def test_falls_back_to_the_stem_plus_a_guessed_extension(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=PDF)  # no Content-Disposition

        client, _ = _client(handler)
        async with client:
            result = await client.download("/x/spec", tmp_path, fallback_stem="spec")

        assert result.path == tmp_path / "spec.pdf"

    @pytest.mark.asyncio
    async def test_403_is_a_recorded_outcome_not_an_error(self, tmp_path: Path) -> None:
        # Every model answer is a 403 (plan §9.3). Raising here would turn
        # 45 expected refusals into 45 failures.
        client, _ = _client()
        async with client:
            result = await client.download("/2324/40001/exercises/1/model-answer", tmp_path)

        assert result.status == "forbidden"
        assert result.status_code == 403
        assert result.path is None
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_404_is_a_recorded_outcome_not_an_error(self, tmp_path: Path) -> None:
        client, _ = _client()
        async with client:
            result = await client.download("/2324/40001/exercises/1/supplementary", tmp_path)

        assert result.status == "absent"
        assert result.status_code == 404
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_401_raises_without_echoing_the_body(self, tmp_path: Path) -> None:
        secret = "hunter2 was wrong"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": secret})

        client, _ = _client(handler)
        async with client:
            with pytest.raises(EmarkingAuthError) as excinfo:
                await client.download("/x/spec", tmp_path)

        assert secret not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_truncated_transfer_is_caught_and_leaves_no_file(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            # Content-Length promises more than the body delivers. The
            # metadata's file_size is not trusted for this (plan §9.6).
            return httpx.Response(
                200,
                content=b"short",
                headers={
                    "content-length": "999999",
                    "content-disposition": 'inline; filename="spec.pdf"',
                },
            )

        client, _ = _client(handler)
        async with client:
            with pytest.raises(EmarkingError, match="truncated"):
                await client.download("/x/spec", tmp_path)

        # Nothing left behind for a later run to mistake for complete.
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_an_interrupted_download_leaves_no_partial_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A dropped connection is a transport error, so it's retried --
        # patch the backoff out rather than waiting 2+4+8+16s for it.
        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        class DropsMidTransfer(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"%PDF-1.4 first chunk"
                raise httpx.ReadError("connection dropped")

        class Failing(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    headers={"content-disposition": 'inline; filename="spec.pdf"'},
                    stream=DropsMidTransfer(),
                )

        client = EmarkingClient("jbloggs", "hunter2", delay=0, transport=Failing())
        async with client:
            with pytest.raises(httpx.ReadError):
                await client.download("/x/spec", tmp_path)

        assert list(tmp_path.iterdir()) == []


class TestRetry:
    @pytest.mark.asyncio
    async def test_retries_a_transport_error_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectError("nope")
            return httpx.Response(200, json=["2324"])

        client, _ = _client(handler)
        async with client:
            assert await client.years() == ["2324"]
        assert attempts["n"] == 3

    @pytest.mark.asyncio
    async def test_gives_up_after_the_last_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            raise httpx.ConnectError("nope")

        client, _ = _client(handler)
        async with client:
            with pytest.raises(httpx.ConnectError):
                await client.years()
        assert attempts["n"] == 5  # 1 try + 4 retries

    @pytest.mark.asyncio
    async def test_terminal_statuses_are_not_retried(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A 403 is settled. Retrying it would mean 5x the requests for
        # every model answer, to be told the same thing five times.
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        client, recorder = _client()
        async with client:
            await client.download("/2324/40001/exercises/1/model-answer", tmp_path)

        assert len(recorder.requests) == 1


async def _no_sleep(_seconds: float) -> None:
    return None
