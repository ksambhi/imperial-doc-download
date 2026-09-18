"""HTTP client for LabTS: login, GET-with-retry, session-expiry detection.

Hard safety rules (`docs/labts-fetch-plan.md` §1) enforced by this module:

- **GET only.** `LabtsClient` exposes exactly one public request method,
  `get()`. The single POST in the entire codebase is the login POST inside
  `_login()` below; there is no generic `post()` helper, and there must
  never be one -- LabTS detail pages carry forms that submit coursework
  late or queue jobs on the shared test-VM fleet.
- **Sequential only.** No concurrency of any kind.
- **A delay between every request** (default 1.5s, configurable).
- **Long timeouts** and **retry with exponential backoff** (4 attempts,
  2/4/8/16s) on `httpx.HTTPError`, since pages can take up to ~23s and
  connections do drop mid-transfer.
- **Never log the password, the POST body, or the session cookie.** Only
  URLs and status codes are logged.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path

import httpx

from imperial_doc_download.labts_fetch.parsing import parse_csrf_token

logger = logging.getLogger(__name__)

BASE_URL = "https://teaching.doc.ic.ac.uk"
SIGN_IN_PATH = "/labts/users/sign_in"

# Pages take up to ~23s to render (plan §1.4).
TIMEOUT = httpx.Timeout(connect=30, read=120, write=30, pool=120)

# 4 retry attempts (5 tries total) with exponential backoff (plan §1.5).
_RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0)


class LabtsAuthError(RuntimeError):
    """Raised when LabTS login fails, or an existing session has expired.

    LabTS doesn't return a 4xx/5xx for either case -- an unauthenticated
    request to a protected URL returns 200 after silently redirecting to
    the sign-in page (plan §2.1). So this is raised whenever the *final*
    response URL ends in `/users/sign_in`, not based on status code.
    """


class LabtsClient:
    """A GET-only HTTP client for LabTS, with retry/backoff and a mandatory
    delay between requests so as not to hammer a shared teaching server.

    Login happens lazily on the first `get()` call. All other I/O (delay,
    retry, expiry checks) is centralised here so `parsing.py` can stay pure
    and `step.py` can stay focused on orchestration.
    """

    def __init__(
        self,
        username: str | None,
        password: str | None,
        *,
        delay: float = 1.5,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        cache_dir: Path | str | None = None,
    ) -> None:
        if not username or not password:
            raise ValueError("LabtsClient requires both a username and a password.")

        self._username = username
        self._password = password
        self._delay = delay
        self._base_url = base_url
        self._logged_in = False
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.cache_hits = 0
        self.cache_misses = 0
        self._client = httpx.Client(
            base_url=base_url,
            follow_redirects=True,
            timeout=TIMEOUT,
            transport=transport,
        )

    def __enter__(self) -> LabtsClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_html(self, url: str) -> str:
        """Fetch `url` (absolute, or relative to the LabTS base URL) as HTML.

        Served from the on-disk cache when one is configured and the page
        is already there -- a cache hit costs no request, no delay, and no
        login, so a fully cached re-run touches the network zero times.

        Otherwise: logs in if this is the first real request, retries on
        `httpx.HTTPError` with exponential backoff, caches the result, then
        sleeps the configured delay before returning -- so every live fetch
        is naturally throttled and requests stay strictly sequential.
        """
        cached = self._cache_read(url)
        if cached is not None:
            self.cache_hits += 1
            logger.info("GET %s (cached)", url)
            return cached

        if not self._logged_in:
            self._login()

        logger.info("GET %s", url)
        response = self._request_with_retry(url)
        self._raise_if_session_expired(response)

        self.cache_misses += 1
        self._cache_write(url, response.text)
        time.sleep(self._delay)
        return response.text

    def _cache_path(self, url: str) -> Path | None:
        """Readable, collision-free cache filename for `url`.

        A slug of the URL keeps the cache directory browsable; the hash
        suffix guarantees uniqueness once the slug is truncated.
        """
        if self._cache_dir is None:
            return None
        absolute = str(httpx.URL(self._base_url).join(url))
        slug = re.sub(r"[^A-Za-z0-9]+", "_", absolute.removeprefix("https://")).strip("_")
        digest = hashlib.sha256(absolute.encode()).hexdigest()[:8]
        return self._cache_dir / f"{slug[:100]}-{digest}.html"

    def _cache_read(self, url: str) -> str | None:
        path = self._cache_path(url)
        if path is None or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def _cache_write(self, url: str, html: str) -> None:
        path = self._cache_path(url)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")

    def _login(self) -> None:
        """POST the sign-in form. The only POST anywhere in this codebase.

        Note this deliberately fetches the sign-in page through
        `_request_with_retry` rather than `get_html`: the CSRF token is tied
        to the session cookie of the response it came from, so a cached
        sign-in page would hand back a stale token and the login would fail.
        Never route this through the cache.
        """
        logger.info("GET %s (sign-in page)", SIGN_IN_PATH)
        sign_in_page = self._request_with_retry(SIGN_IN_PATH)
        token = parse_csrf_token(sign_in_page.text)

        logger.info("POST %s (login)", SIGN_IN_PATH)
        response = self._client.post(
            SIGN_IN_PATH,
            data={
                "utf8": "✓",
                "authenticity_token": token,
                "user[uid]": self._username,
                "user[password]": self._password,
                "user[remember_me]": "0",
                "commit": "Sign in",
            },
        )
        response.raise_for_status()

        if str(response.url).endswith("/users/sign_in"):
            raise LabtsAuthError(
                "LabTS login failed -- check IMPERIAL_USERNAME and IMPERIAL_PASSWORD."
            )

        logger.info("Logged in to LabTS (status %d)", response.status_code)
        self._logged_in = True
        time.sleep(self._delay)

    def _request_with_retry(self, url: str) -> httpx.Response:
        last_exc: httpx.HTTPError | None = None
        for attempt, backoff in enumerate((0.0, *_RETRY_DELAYS)):
            if backoff:
                logger.warning(
                    "Retrying GET %s in %gs (attempt %d/%d)",
                    url,
                    backoff,
                    attempt,
                    len(_RETRY_DELAYS),
                )
                time.sleep(backoff)
            try:
                response = self._client.get(url)
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning("GET %s failed: %s", url, type(exc).__name__)

        assert last_exc is not None  # the loop always runs at least once
        raise last_exc

    @staticmethod
    def _raise_if_session_expired(response: httpx.Response) -> None:
        if str(response.url).endswith("/users/sign_in"):
            raise LabtsAuthError("LabTS session expired or credentials were rejected.")
