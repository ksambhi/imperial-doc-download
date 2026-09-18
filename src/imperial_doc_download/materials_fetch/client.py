"""The materials API's one route, on top of the shared GET-only client.

```
GET https://materials-api.doc.ic.ac.uk/resources/zipped?year=&course=
```

Everything else — SOCKS-proxied transport, HTTP Basic auth, the
`x-proxied-user` header, the delay, retries, streaming to disk, and the
abc-api enrolment call — comes from `imperial_doc_download.doc_api`.

Its safety rules matter here too, and one of them is sharper than
elsewhere: this API has `PUT /resources/{resource_id}/file`, which
replaces the file students download. There is one request method and the
verb is a literal (`docs/materials-fetch-plan.md` §1).
"""

from __future__ import annotations

import logging
from pathlib import Path

from imperial_doc_download.doc_api import DocApiClient, Download

logger = logging.getLogger(__name__)

MATERIALS_BASE_URL = "https://materials-api.doc.ic.ac.uk"

ZIPPED_PATH = "/resources/zipped"


class MaterialsClient(DocApiClient):
    """GET-only client for the teaching-materials API."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("base_url", MATERIALS_BASE_URL)
        super().__init__(*args, **kwargs)

    async def zipped(self, year: str, module_code: str, directory: Path) -> Download:
        """Download one module's materials zip into `directory`.

        Returns the shared `Download` outcome, so a `404` — which is what
        a module with no materials published answers, 4 of our 50 ✅ —
        comes back as `absent` rather than raising. Streamed to a temp
        file and renamed, and verified against `Content-Length`, which
        this API sends on every response ✅.
        """
        return await self.download(
            ZIPPED_PATH,
            directory,
            params={"year": year, "course": module_code},
            fallback_stem=f"{module_code}_materials",
        )
