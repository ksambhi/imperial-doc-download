"""The eMarking API's own routes, on top of the shared GET-only client.

Everything that isn't eMarking-specific — the SOCKS-proxied transport,
HTTP Basic auth, the `x-proxied-user` header, the delay, the retry policy,
streaming a file to disk, and the abc-api enrolment call — lives in
`imperial_doc_download.doc_api`. See that module for the safety rules;
they apply here in full, and for the original reason: this API is what
coursework is submitted to and marks are recorded in.

What's left here is one route:

```
GET /me/{year}/exercises
```

which carries a year's exercises with our submissions, feedback and marks
embedded (plan §3.2), and the download paths built from it.
"""

from __future__ import annotations

import logging

from imperial_doc_download.doc_api import (
    ABC_BASE_URL,
    TIMEOUT,
    DocApiAuthError,
    DocApiClient,
    DocApiError,
    Download,
)

logger = logging.getLogger(__name__)

EMARKING_BASE_URL = "https://emarking-api.doc.ic.ac.uk"

#: The shared errors under the names this package has always used. They
#: are the same classes, not subclasses, so `except EmarkingError` and
#: `except DocApiError` catch each other.
EmarkingAuthError = DocApiAuthError
EmarkingError = DocApiError

__all__ = [
    "ABC_BASE_URL",
    "EMARKING_BASE_URL",
    "TIMEOUT",
    "Download",
    "EmarkingAuthError",
    "EmarkingClient",
    "EmarkingError",
]


class EmarkingClient(DocApiClient):
    """GET-only client for eMarking."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("base_url", EMARKING_BASE_URL)
        # Kept as a named parameter because the tests and callers use it.
        if "emarking_base_url" in kwargs:
            kwargs["base_url"] = kwargs.pop("emarking_base_url")
        super().__init__(*args, **kwargs)

    async def exercises(self, year: str) -> list[dict] | None:
        """One year's exercises, or None if the API has nothing for it.

        21 of the 25 advertised years answer 500 or 502 rather than coming
        back empty (plan §9.5), so "no data for this year" and "the server
        broke" are indistinguishable by status alone — and since that is
        the normal case for most years, it must not fail the run.
        """
        response = await self._request(f"{self._base_url}/me/{year}/exercises")
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
        raise DocApiError(f"/me/{year}/exercises returned {response.status_code}.")
