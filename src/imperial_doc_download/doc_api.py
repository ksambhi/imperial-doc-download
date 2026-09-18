"""GET-only async HTTP client for DoC's internal APIs.

Three of them sit behind the departmental firewall — eMarking, materials,
and the abc-api that says which modules we were enrolled in — and they
are reached the same way: through the SSH SOCKS proxy, with HTTP Basic
auth and an `x-proxied-user` header. This is the machinery they share;
the per-API routes live with their own pipelines.

The safety rules it exists to enforce (`docs/emarking-fetch-plan.md` §1,
`docs/materials-fetch-plan.md` §1) are the same for every one of them,
because these are not read-only reporting APIs:

- **GET only, by construction.** There is exactly one place a request is
  issued, and it hardcodes the method. There is no
  `post()`/`put()`/`patch()`/`delete()` helper and there must never be
  one — between them these APIs submit coursework, write marks, edit
  feedback and replace lecturers' teaching files.
- **Personal endpoints only.** Subclasses build paths for `/me/*`, our
  own submissions, and modules we were enrolled in. Staff routes are not
  reachable from here, not even to check.
- **Delayed.** A delay after every request, applied *inside* the
  concurrency semaphore, so raising `--concurrency` raises the rate
  rather than removing the throttle. None of these APIs sends
  rate-limit headers, so restraint has to come from our side.
- **Never log the password or the `Authorization` header.** URLs and
  status codes only.

Retries are for **transport errors only**. Every status these APIs return
is meaningful and settled: a 404 means the artefact isn't there, a 403
means it isn't ours, a 500 on a year means we weren't enrolled. Retrying
those would just ask the same question five times.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from imperial_doc_download.naming import (
    filename_from_disposition,
    guess_extension,
    safe_component,
)

logger = logging.getLogger(__name__)

ABC_BASE_URL = "https://abc-api.doc.ic.ac.uk"

#: The largest artefact measured is a 189 MB materials zip, so reads get
#: plenty of room; connects are either fast or broken.
TIMEOUT = httpx.Timeout(connect=30, read=300, write=30, pool=300)

#: 4 retries (5 tries total) with exponential backoff, matching LabTS.
_RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0)

#: Streamed to disk in chunks rather than buffered.
_CHUNK_SIZE = 64 * 1024


class DocApiAuthError(RuntimeError):
    """Raised on a 401 — credentials are missing, wrong, or expired.

    Never carries the response body: an auth failure is exactly the
    response most likely to echo something we don't want in a log file.
    """


class DocApiError(RuntimeError):
    """An API response we don't know how to interpret."""


@dataclass(frozen=True)
class Download:
    """What happened when we asked for one file.

    `status` is one of:

    - `"downloaded"` — it's on disk at `path`
    - `"absent"` — a 404; the artefact genuinely isn't there
    - `"forbidden"` — a 403; it exists but isn't ours to have, which is
      every model answer (plan §9.3)

    `absent` and `forbidden` are terminal and expected, not errors: the
    step records them and does not retry them on a later run.
    """

    status: str
    status_code: int
    path: Path | None = None
    size: int | None = None


class DocApiClient:
    """A GET-only client for one firewalled DoC API, over the SOCKS proxy.

    Subclasses add the routes they need on top; this owns the transport,
    the auth, the throttle and the retry policy. `base_url` is the API
    the subclass talks to — abc-api is reachable from here too, since
    every pipeline needs the enrolment list.
    """

    def __init__(
        self,
        username: str,
        password: str,
        *,
        proxy: str | None = None,
        delay: float = 1.0,
        concurrency: int = 1,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = "",
        abc_base_url: str = ABC_BASE_URL,
    ) -> None:
        if not username or not password:
            raise ValueError(f"{type(self).__name__} requires both a username and a password.")

        self._username = username
        self._delay = delay
        self._abc_base_url = abc_base_url.rstrip("/")
        self._base_url = base_url.rstrip("/")
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self.request_count = 0

        self._auth = httpx.BasicAuth(username, password)
        self._client = httpx.AsyncClient(
            proxy=proxy,
            timeout=TIMEOUT,
            transport=transport,
            follow_redirects=True,
            # A declared (optional) header parameter on every operation of
            # both eMarking and materials. abc-api ignores it.
            headers={"x-proxied-user": username},
        )

    async def __aenter__(self) -> DocApiClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------- shared API

    async def years(self) -> list[str]:
        """Every academic year the system knows about (abc-api, no auth)."""
        response = await self._request(f"{self._abc_base_url}/years", auth=False)
        if response.status_code != 200:
            raise DocApiError(f"/years returned {response.status_code}.")
        return list(response.json())

    async def enrolment(self, year: str) -> list[dict] | None:
        """The modules we were enrolled in for `year`, from abc-api.

        **This method deliberately takes no username.** It always filters
        on the configured one, because the unfiltered form of this route
        (`/{year}/students` with no `login`) is every student in the
        department. Aiming it at anyone else is not an expressible call,
        which is the same by-construction approach as GET-only.

        Returns the raw `modules` array, `[]` for a year we weren't
        enrolled in (the API answers `200 []` for those ✅), or `None`
        when the year predates abc-api's records and it 500s.
        """
        response = await self._request(
            f"{self._abc_base_url}/{year}/students",
            params={"login": self._username},
        )
        if response.status_code in (500, 502):
            logger.debug(
                "Year %s: %d from abc-api — before its records.", year, response.status_code
            )
            return None
        if response.status_code != 200:
            raise DocApiError(f"/{year}/students returned {response.status_code}.")

        records = response.json()
        if not records:
            return []

        # Belt and braces: the filter is a query parameter, so confirm the
        # server honoured it before reading anything out of the record.
        # If it ever hands back somebody else, that is a bug to report,
        # not data to use.
        ours = [r for r in records if r.get("login") == self._username]
        if not ours:
            raise DocApiError(
                f"/{year}/students?login=… returned {len(records)} record(s), none of them "
                "ours. Refusing to read another student's enrolment."
            )
        return list(ours[0].get("modules") or [])

    async def download(
        self,
        path: str,
        directory: Path,
        *,
        params: dict[str, str] | None = None,
        preferred_name: str | None = None,
        fallback_stem: str = "download",
    ) -> Download:
        """Stream one artefact into `directory`, if we're allowed it.

        The filename is `preferred_name` when the caller has a better one
        than the wire does — submissions do, since the header's name
        carries an opaque uuid prefix (plan §9.1) — then the
        `Content-Disposition` filename, then `fallback_stem` plus a guessed
        extension.

        Written to a temp file and renamed into place, so an interrupted
        run never leaves a half-file that a later run would mistake for a
        complete one. A truncated transfer is caught against the response's
        `Content-Length` — never against the metadata's `file_size`, which
        lies (plan §9.6).
        """
        url = f"{self._base_url}{path}"

        async with self._semaphore:
            self.request_count += 1
            logger.info("GET %s", path)
            response = await self._stream_with_retry(
                url, directory, preferred_name, fallback_stem, params
            )
            await self._sleep()
            return response

    # ------------------------------------------------------------ plumbing

    async def _stream_with_retry(
        self,
        url: str,
        directory: Path,
        preferred_name: str | None,
        fallback_stem: str,
        params: dict[str, str] | None = None,
    ) -> Download:
        last_exc: httpx.TransportError | None = None

        for attempt, backoff in enumerate((0.0, *_RETRY_DELAYS)):
            if backoff:
                logger.warning(
                    "Retrying GET %s in %gs (attempt %d/%d)",
                    url,
                    backoff,
                    attempt,
                    len(_RETRY_DELAYS),
                )
                await asyncio.sleep(backoff)
            try:
                return await self._stream_once(
                    url, directory, preferred_name, fallback_stem, params
                )
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning("GET %s failed: %s", url, type(exc).__name__)

        assert last_exc is not None  # the loop always runs at least once
        raise last_exc

    async def _stream_once(
        self,
        url: str,
        directory: Path,
        preferred_name: str | None,
        fallback_stem: str,
        params: dict[str, str] | None = None,
    ) -> Download:
        async with self._client.stream("GET", url, auth=self._auth, params=params) as response:
            terminal = _terminal_status(response)
            if terminal is not None:
                # The body of a 401 is the one thing never to read or log.
                if terminal.status_code != 401:
                    await response.aread()
                return terminal

            directory.mkdir(parents=True, exist_ok=True)

            # The first chunk is pulled before the name is settled, because
            # guessing an extension needs bytes to look at.
            chunks = response.aiter_bytes(_CHUNK_SIZE)
            try:
                first = await anext(chunks)
            except StopAsyncIteration:
                first = b""

            name = (
                safe_component(preferred_name, fallback="") if preferred_name else None
            ) or filename_from_disposition(response.headers.get("content-disposition"))
            if not name:
                name = f"{safe_component(fallback_stem)}{guess_extension(first)}"

            destination = directory / name
            temporary = directory / f".{name}.part"
            written = 0
            try:
                with temporary.open("wb") as handle:
                    if first:
                        handle.write(first)
                        written += len(first)
                    async for chunk in chunks:
                        handle.write(chunk)
                        written += len(chunk)
                _check_complete(response, written, url)
                temporary.replace(destination)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise

        return Download("downloaded", response.status_code, destination, written)

    async def _request(
        self, url: str, *, auth: bool = True, params: dict[str, str] | None = None
    ) -> httpx.Response:
        async with self._semaphore:
            self.request_count += 1
            logger.info("GET %s", url)
            response = await self._request_with_retry(url, auth=auth, params=params)
            await self._sleep()
            return response

    async def _request_with_retry(
        self, url: str, *, auth: bool, params: dict[str, str] | None = None
    ) -> httpx.Response:
        last_exc: httpx.TransportError | None = None

        for attempt, backoff in enumerate((0.0, *_RETRY_DELAYS)):
            if backoff:
                logger.warning(
                    "Retrying GET %s in %gs (attempt %d/%d)",
                    url,
                    backoff,
                    attempt,
                    len(_RETRY_DELAYS),
                )
                await asyncio.sleep(backoff)
            try:
                # The one and only place this codebase issues a request to
                # any of these APIs, and the method is a literal. Keep it
                # that way -- see the module docstring.
                response = await self._client.request(
                    "GET", url, auth=self._auth if auth else None, params=params
                )
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning("GET %s failed: %s", url, type(exc).__name__)
                continue

            if response.status_code == 401:
                raise DocApiAuthError(
                    "The API rejected the credentials (401). Check IMPERIAL_USERNAME "
                    "and IMPERIAL_PASSWORD -- and note that `source`-ing .env truncates "
                    "a password containing shell metacharacters (plan §8)."
                )
            return response

        assert last_exc is not None  # the loop always runs at least once
        raise last_exc

    async def _sleep(self) -> None:
        if self._delay:
            await asyncio.sleep(self._delay)


def _terminal_status(response: httpx.Response) -> Download | None:
    """Map the statuses that mean "stop asking" onto a `Download`.

    Deliberately not retried, and deliberately not raised. Every status
    this API returns is meaningful and settled (plan §9.3): a 404 means
    the artefact isn't there, a 403 means it isn't ours. Only transport
    errors are worth another go.
    """
    if response.status_code == 401:
        raise DocApiAuthError("The API rejected the credentials (401).")
    if response.status_code == 403:
        return Download("forbidden", 403)
    if response.status_code == 404:
        return Download("absent", 404)
    if response.status_code != 200:
        raise DocApiError(f"GET {response.request.url.path} returned {response.status_code}.")
    return None


def _check_complete(response: httpx.Response, written: int, url: str) -> None:
    declared = response.headers.get("content-length")
    if declared is None:
        return
    try:
        expected = int(declared)
    except ValueError:  # pragma: no cover - a server sending nonsense
        return
    if written != expected:
        raise DocApiError(
            f"GET {url} was truncated: got {written} bytes, Content-Length said {expected}."
        )
