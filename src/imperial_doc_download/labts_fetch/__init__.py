"""Fetch data from LabTS — Imperial DoC's coursework submission/test system.

Logs in to LabTS, walks every academic year, and produces the list of
GitLab repositories (with clone URLs and per-milestone submission info)
behind every exercise — written to `<output_dir>/labts-list.json`. Cloning
those repos is a separate, later pipeline step. See
`docs/labts-fetch-plan.md` for the full design.
"""

from __future__ import annotations

from imperial_doc_download.labts_fetch.step import LabtsFetchStep

__all__ = ["LabtsFetchStep"]
