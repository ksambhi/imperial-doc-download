"""`EmarkingMarksStep`: write the personal marks record.

Reshapes what `emarking-fetch` already has into
`<output_dir>/emarking-marks.json` — every marked exercise, with its
deadline, mark, percentage, grade and status colour, across every year.

Costs **no requests at all** when it runs after `emarking-fetch` in the
same pipeline: the exercises and the enrolled modules are handed over in
`ctx.state`. Run on its own, it reads both back off disk from an earlier
run — so the record can always be rebuilt without touching the API.

Only if a year's enrolment isn't cached does it need the network, and
only then does it open the SOCKS proxy. Starting an ssh tunnel to do
nothing is both slow and rude.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from imperial_doc_download.emarking_fetch.client import EmarkingClient
from imperial_doc_download.emarking_fetch.marks import build_record
from imperial_doc_download.emarking_fetch.models import (
    Exercise,
    Module,
    enrolled_modules,
)
from imperial_doc_download.emarking_fetch.proxy import SocksProxy
from imperial_doc_download.emarking_fetch.results_html import (
    feedback_links_from_manifest,
    render_results,
)
from imperial_doc_download.emarking_fetch.step import (
    ENROLMENT_CACHE_FILENAME,
    MANIFEST_FILENAME,
    YEAR_CACHE_FILENAME,
    read_year_cache,
)
from imperial_doc_download.pipeline import PipelineContext, Step

logger = logging.getLogger(__name__)

MARKS_FILENAME = "emarking-marks.json"
RESULTS_FILENAME = "emarking-results.html"


class EmarkingMarksStep(Step):
    """Writes the personal record of marks and grades."""

    name = "emarking-marks"

    def __init__(self, *, force: bool = False, jump_host: str | None = None) -> None:
        self._force = force
        self._jump_host = jump_host

    def run(self, ctx: PipelineContext) -> None:
        exercises, modules = self._load(ctx)

        missing = sorted(set(exercises) - set(modules))
        if missing:
            # Only reachable when the exercise cache exists but the
            # enrolment one doesn't — an output directory from before
            # this step existed.
            modules |= self._fetch_missing_enrolments(ctx, missing)

        if not exercises:
            raise RuntimeError(
                "emarking-marks has no exercise data to work from. Run the emarking "
                "step first, or point --output-dir at a directory an earlier run wrote."
            )

        record = build_record(
            exercises,
            modules,
            username=ctx.settings.username or "",
            generated_at=datetime.now(UTC).isoformat(),
        )
        path = ctx.output_dir / MARKS_FILENAME
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        logger.info("Wrote %s", path)

        html_path = ctx.output_dir / RESULTS_FILENAME
        html_path.write_text(render_results(record, self._feedback_links(ctx)), encoding="utf-8")
        logger.info("Wrote %s", html_path)

        ctx.state["emarking_marks"] = record
        self._print_report(record, path, html_path)

    @staticmethod
    def _feedback_links(ctx: PipelineContext) -> dict[tuple[str, str, int], str]:
        """Where each feedback PDF landed, for the page to link at.

        Read from the manifest rather than guessed from the layout, so a
        file that 403'd or failed shows a dash instead of a dead link.
        """
        path = ctx.output_dir / MANIFEST_FILENAME
        if not path.is_file():
            logger.info("No %s, so the results page will have no feedback links.", path)
            return {}
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("%s is unreadable — the results page will have no links.", path)
            return {}
        return feedback_links_from_manifest(manifest)

    # ------------------------------------------------------------- loading

    def _load(
        self, ctx: PipelineContext
    ) -> tuple[dict[str, list[Exercise]], dict[str, list[Module]]]:
        """Exercises and modules, from the pipeline or from disk."""
        modules: dict[str, list[Module]] = dict(ctx.state.get("emarking_modules") or {})
        exercises: dict[str, list[Exercise]] = dict(ctx.state.get("emarking_exercises") or {})
        if exercises:
            logger.info("Using the exercise data from this run's emarking-fetch step.")
            return exercises, modules

        logger.info("Reading what an earlier run wrote to %s.", ctx.output_dir)
        for year_dir in sorted(p for p in ctx.output_dir.glob("*") if p.is_dir()):
            year = year_dir.name
            year_exercises = _read_cached_exercises(year_dir / YEAR_CACHE_FILENAME, year)
            if year_exercises:
                exercises[year] = year_exercises
            year_modules = _read_cached_modules(year_dir / ENROLMENT_CACHE_FILENAME)
            if year_modules:
                modules[year] = year_modules

        return exercises, modules

    def _fetch_missing_enrolments(
        self, ctx: PipelineContext, years: list[str]
    ) -> dict[str, list[Module]]:
        """Fetch the enrolment for years that have exercises but no cache.

        The only path in this step that touches the network, and it opens
        the proxy only because it has to.
        """
        settings = ctx.settings
        if not (settings.username and settings.password and settings.doc_ssh_key):
            logger.warning(
                "No enrolment cached for %s and no credentials to fetch it — "
                "module titles will be missing for those years.",
                ", ".join(years),
            )
            return {}

        logger.info("Fetching the enrolment for %s.", ", ".join(years))
        return asyncio.run(self._fetch_enrolments(ctx, years))

    async def _fetch_enrolments(
        self, ctx: PipelineContext, years: list[str]
    ) -> dict[str, list[Module]]:
        settings = ctx.settings
        assert settings.username and settings.password and settings.doc_ssh_key

        fetched: dict[str, list[Module]] = {}
        proxy = SocksProxy(
            key=Path(settings.doc_ssh_key),
            username=settings.username,
            key_passphrase=settings.doc_ssh_key_passphrase,
            jump_host=self._jump_host,
        )
        with proxy:
            client = EmarkingClient(
                settings.username, settings.password, proxy=proxy.url, delay=1.0
            )
            try:
                for year in years:
                    payload = await client.enrolment(year)
                    if not payload:
                        continue
                    modules = enrolled_modules(payload)
                    fetched[year] = modules
                    cache = ctx.output_dir / year / ENROLMENT_CACHE_FILENAME
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(
                        json.dumps([{"code": m.code, "title": m.title} for m in modules], indent=2),
                        encoding="utf-8",
                    )
            finally:
                await client.aclose()
        return fetched

    # ------------------------------------------------------------ reporting

    def _print_report(self, record: dict, path: Path, html_path: Path) -> None:
        console = Console()

        for year, modules in sorted(record["years"].items()):
            exercises = [e for m in modules for e in m["exercises"]]
            table = Table(title=f"{year} — {len(exercises)} marked exercise(s)")
            table.add_column("Module")
            table.add_column("Exercise")
            table.add_column("Mark", justify="right")
            table.add_column("%", justify="right")
            table.add_column("Grade", justify="right")

            for module in modules:
                for index, exercise in enumerate(module["exercises"]):
                    table.add_row(
                        f"{module['module_code']} {module['title'] or ''}".strip()
                        if index == 0
                        else "",
                        _exercise_cell(exercise),
                        f"{_plain(exercise['mark'])}/{_plain(exercise['maximum_mark'])}",
                        f"{exercise['percentage']:.1f}" if exercise["percentage"] else "—",
                        exercise["grade"] or "—",
                    )
            console.print(table)

        summary = record["summary"]
        console.print(
            f"{summary['exercises']} marked exercise(s) across {summary['modules']} module(s) "
            f"and {summary['years']} year(s)\n  → {path}\n  → {html_path}"
        )


def _exercise_cell(exercise: dict) -> str:
    """The exercise label, tinted by its status colour like the page."""
    colour = {"green": "green", "purple": "magenta"}.get(exercise["colour"], "white")
    label = exercise["label"]
    return f"[{colour}]●[/{colour}] {label[:44]}"


def _plain(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _read_cached_modules(path: Path) -> list[Module]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("%s is unreadable — ignoring it.", path)
        return []
    return enrolled_modules(payload)


def _read_cached_exercises(path: Path, year: str) -> list[Exercise]:
    """Exercises from a year cache, if it was written under the current scope.

    No module filtering here: the cache was already scoped when it was
    written. Re-filtering would also make this depend on the enrolment
    cache, and the whole point of the `missing` check in `_load` is to
    cope with a year that has one and not the other.
    """
    if not path.is_file():
        return []
    payload = read_year_cache(path, year)
    if not payload:
        return []
    return [Exercise.from_api(item) for item in payload]
