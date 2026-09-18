"""Fetch teaching materials from DoC's materials system.

Downloads one zip of lecture notes, slides and handouts per module, for
every module enrolled in, and extracts it into
`<output_dir>/<year>/<module_code>/materials/` — beside that module's
`emarking/` directory. The zip is deleted once it has extracted cleanly
(`--keep-zip` retains it); the whole set is ~1.75 GB.

Like the other API pipelines this is **GET only by construction** and
touches only our own modules. That matters here too: `materials-api`
exposes `PUT /resources/{id}/file`, which replaces the file students
download. See `docs/materials-fetch-plan.md`.

Needs `IMPERIAL_USERNAME`, `IMPERIAL_PASSWORD` and `IMPERIAL_DOC_SSH_KEY`
for the SOCKS proxy through a DoC shell server.
"""

from __future__ import annotations

from imperial_doc_download.materials_fetch.step import MaterialsFetchStep

__all__ = ["MaterialsFetchStep"]
