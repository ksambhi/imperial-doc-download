"""Fetch coursework data from eMarking — Imperial DoC's marking system.

Downloads every coursework spec, submission, supplementary file and
feedback file this account has, into
`<output_dir>/<year>/<module_code>/emarking/`. See
`docs/emarking-fetch-plan.md` for the full design.

This API is not a read-only reports system: it is what coursework is
submitted to and marks are recorded in. So the client is **GET only by
construction**, touches **personal endpoints only** (`/me/*` and our own
submissions), and is sequential with a delay by default — it's a shared
teaching server. See `client.py`.

Needs `IMPERIAL_USERNAME`, `IMPERIAL_PASSWORD` and `IMPERIAL_DOC_SSH_KEY`:
both API hosts are inside the DoC firewall, so everything goes through an
SSH SOCKS proxy on a shell server (`proxy.py`).
"""

from __future__ import annotations

from imperial_doc_download.emarking_fetch.step import EmarkingFetchStep

__all__ = ["EmarkingFetchStep"]
