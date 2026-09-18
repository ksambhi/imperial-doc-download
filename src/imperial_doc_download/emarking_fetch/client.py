"""GET-only async HTTP client for the eMarking API.

The hard safety rules from `docs/emarking-fetch-plan.md` §1 are enforced
here, because this is the only module that speaks to the API:

- **GET only, by construction.** There is exactly one place a request is
  issued (`_request`), and it hardcodes the method. There is no
  `post()`/`put()`/`patch()`/`delete()` helper and there must never be
  one: among this API's 55 routes are ones that submit coursework, edit
  feedback and write marks.
- **Personal endpoints only.** The paths this module builds are `/me/*`,
  our own submissions, and the per-exercise artefacts. Staff routes
  (`/{year}/students/{username}/...`, `/submissions/staff`, `/marks`, ...)
  are not reachable from here, not even to check.
- **Sequential, delayed.** One request at a time by default, with a delay
  after each. The API advertises no rate limits and sends no rate-limit
  headers (plan §9.4), so restraint has to come from our side.
- **Never log the password or the `Authorization` header.** URLs and
  status codes only.

Two hosts, two auth models (plan §3.1): `abc-api` serves `/years`
unauthenticated, `emarking-api` wants HTTP Basic plus an `x-proxied-user`
header. Both are inside the DoC firewall, so both go through the SOCKS
proxy (`proxy.py`).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from imperial_doc_download.emarking_fetch.naming import (
    filename_from_disposition,
    guess_extension,
    safe_component,
)

logger = logging.getLogger(__name__)

ABC_BASE_URL = "https://abc-api.doc.ic.ac.uk"
EMARKING_BASE_URL = "https://emarking-api.doc.ic.ac.uk"

#: The largest artefact measured is a 48 MB zip (plan §9.6), so reads get
#: plenty of room; connects are either fast or broken.
TIMEOUT = httpx.Timeout(connect=30, read=120, write=30, pool=120)

#: 4 retries (5 tries total) with exponential backoff, matching LabTS.
_RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0)

#: Streamed to disk in chunks rather than buffered (plan §9.6).
_CHUNK_SIZE = 64 * 1024


class EmarkingAuthError(RuntimeError):
    """Raised on a 401 — credentials are missing, wrong, or expired.

    Never carries the response body: an auth failure is exactly the
    response most likely to echo something we don't want in a log file.
    """


class EmarkingError(RuntimeError):
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


class EmarkingClient:
    """A GET-only client for the eMarking API, over the SOCKS proxy.

    Sequential by default; `concurrency` fans out through a semaphore for
    the impatient, but the delay is applied per request either way, so a
    higher concurrency raises the request rate rather than removing the
    throttle.
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
        abc_base_url: str = ABC_BASE_URL,
        emarking_base_url: str = EMARKING_BASE_URL,
    ) -> None:
        if not username or not password:
            raise ValueError("EmarkingClient requires both a username and a password.")

        self._username = username
        self._delay = delay
        self._abc_base_url = abc_base_url.rstrip("/")
        self._emarking_base_url = emarking_base_url.rstrip("/")
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self.request_count = 0

        self._auth = httpx.BasicAuth(username, password)
        self._client = httpx.AsyncClient(
            proxy=proxy,
            timeout=TIMEOUT,
            transport=transport,
            follow_redirects=True,
            # Declared as an optional header parameter on every emarking
            # operation (plan §3.1). abc-api ignores it.
            headers={"x-proxied-user": username},
        )

    async def __aenter__(self) -> EmarkingClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- API

    async def years(self) -> list[str]:
        """Every academic year the system knows about (abc-api, no auth)."""
        response = await self._request(f"{self._abc_base_url}/years", auth=False)
        if response.status_code != 200:
            raise EmarkingError(f"/years returned {response.status_code}.")
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
            raise EmarkingError(f"/{year}/students returned {response.status_code}.")

        records = response.json()
        if not records:
            return []

        # Belt and braces: the filter is a query parameter, so confirm the
        # server honoured it before reading anything out of the record.
        # If it ever hands back somebody else, that is a bug to report,
        # not data to use.
        ours = [r for r in records if r.get("login") == self._username]
        if not ours:
            raise EmarkingError(
                f"/{year}/students?login=… returned {len(records)} record(s), none of them "
                "ours. Refusing to read another student's enrolment."
            )
        return list(ours[0].get("modules") or [])

    async def exercises(self, year: str) -> list[dict] | None:
        """One year's exercises, or None if the API has nothing for it.

        21 of the 25 advertised years answer 500 or 502 rather than coming
        back empty (plan §9.5), so "no data for this year" and "the server
        broke" are indistinguishable by status alone — and since that is
        the normal case for most years, it must not fail the run.
        """
        response = await self._request(f"{self._emarking_base_url}/me/{year}/exercises")
        if response.status_code == 200:
            return list(response.json())
        if response.status_code in (500, 502):
            logger.debug(
                "Year %s: %d — no exercises for this account (the expected answer for "
                "a year we weren't here for).",
                year,
                response.status_code,
            )
            return None
        raise EmarkingError(f"/me/{year}/exercises returned {response.status_code}.")

    async def download(
        self,
        path: str,
        directory: Path,
        *,
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
        url = f"{self._emarking_base_url}{path}"

        async with self._semaphore:
            self.request_count += 1
            logger.info("GET %s", path)
            response = await self._stream_with_retry(url, directory, preferred_name, fallback_stem)
            await self._sleep()
            return response

    # ------------------------------------------------------------ plumbing

    async def _stream_with_retry(
        self,
        url: str,
        directory: Path,
        preferred_name: str | None,
        fallback_stem: str,
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
                return await self._stream_once(url, directory, preferred_name, fallback_stem)
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
    ) -> Download:
        async with self._client.stream("GET", url, auth=self._auth) as response:
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
                # the eMarking API, and the method is a literal. Keep it
                # that way -- see the module docstring.
                response = await self._client.request(
                    "GET", url, auth=self._auth if auth else None, params=params
                )
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning("GET %s failed: %s", url, type(exc).__name__)
                continue

            if response.status_code == 401:
                raise EmarkingAuthError(
                    "eMarking rejected the credentials (401). Check IMPERIAL_USERNAME "
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
        raise EmarkingAuthError("eMarking rejected the credentials (401).")
    if response.status_code == 403:
        return Download("forbidden", 403)
    if response.status_code == 404:
        return Download("absent", 404)
    if response.status_code != 200:
        raise EmarkingError(f"GET {response.request.url.path} returned {response.status_code}.")
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
        raise EmarkingError(
            f"GET {url} was truncated: got {written} bytes, Content-Length said {expected}."
        )
