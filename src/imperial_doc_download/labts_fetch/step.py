"""`LabtsFetchStep`: log in to LabTS, walk every academic year, and produce
the list of GitLab repositories behind every exercise.

Cloning those repos is a later, separate pipeline step. This step writes
`<output_dir>/labts-list.json` (the full record) and
`<output_dir>/labts-list.txt` (just the ssh clone URLs, grouped by year,
for feeding straight into the clone step), and stashes the same data in
`ctx.state["labts_exercises"]`. See `docs/labts-fetch-plan.md` throughout.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from rich.console import Console
from rich.table import Table

from imperial_doc_download.labts_fetch.client import LabtsClient
from imperial_doc_download.labts_fetch.models import Exercise, Milestone, load_labts_list
from imperial_doc_download.labts_fetch.parsing import (
    ExerciseRow,
    parse_academic_years,
    parse_detail_page,
    parse_year_page,
)
from imperial_doc_download.pipeline import PipelineContext, Step

logger = logging.getLogger(__name__)

_SHORT_SHA_LEN = 8
_OUTPUT_FILENAME = "labts-list.json"
_SSH_LIST_FILENAME = "labts-list.txt"

_SUBMISSION_DISPLAY = {
    "unsubmitted": "— unsubmitted",
    "mismatch": "⚠ mismatch",
    "not_enabled": "— not enabled",
}


class LabtsFetchStep(Step):
    """Downloads the exercise/repository list (not the code itself) from LabTS."""

    name = "labts-fetch"

    def __init__(
        self,
        *,
        delay: float = 1.5,
        cache_dir: Path | str | None = None,
        force: bool = False,
    ) -> None:
        # No network or filesystem I/O here (the cache directory is only
        # created once something is actually written to it): construction
        # must stay free of side effects so `--dry-run` (which never calls
        # `run()`) and pipeline setup remain instant and offline.
        self._delay = delay
        self._cache_dir = cache_dir
        self._force = force

    def run(self, ctx: PipelineContext) -> None:
        if self._reuse_previous_results(ctx):
            return

        settings = ctx.settings
        if not settings.username or not settings.password:
            raise RuntimeError(
                "labts-fetch requires both IMPERIAL_USERNAME and IMPERIAL_PASSWORD "
                "to be set in the environment."
            )

        client = LabtsClient(
            settings.username,
            settings.password,
            delay=self._delay,
            cache_dir=self._cache_dir,
        )
        try:
            results = self._fetch_all_years(client)
        finally:
            if self._cache_dir is not None:
                logger.info(
                    "Cache: %d hit(s), %d fetched page(s) written to %s",
                    client.cache_hits,
                    client.cache_misses,
                    self._cache_dir,
                )
            client.close()

        output_path = self._write_json(ctx, results)
        ssh_list_path = self._write_ssh_list(ctx, results)
        ctx.state["labts_exercises"] = results
        self._print_report(results, output_path, ssh_list_path)

    def _reuse_previous_results(self, ctx: PipelineContext) -> bool:
        """Short-circuit the whole step when an earlier run's output is there.

        A full LabTS run is 4–6 minutes against a shared teaching server,
        so when the later steps of the pipeline (cloning, ...) are what's
        actually being re-run, reusing `labts-list.json` is both faster and
        politer. `--force` ignores it and refetches.
        """
        output_path = ctx.output_dir / _OUTPUT_FILENAME
        if self._force or not output_path.is_file():
            return False

        try:
            results = load_labts_list(output_path)
        except (KeyError, ValueError) as exc:
            logger.warning("%s is unreadable (%s) — refetching from LabTS.", output_path, exc)
            return False

        total = sum(len(exercises) for exercises in results.values())
        logger.info(
            "Reusing %s: %d repositories across %d academic year(s). Pass --force to refetch.",
            output_path,
            total,
            len(results),
        )
        ctx.state["labts_exercises"] = results
        return True

    def _fetch_all_years(self, client: LabtsClient) -> dict[str, list[Exercise]]:
        landing_page = client.get_html("/labts")
        years = parse_academic_years(landing_page)
        logger.info("LabTS advertises %d academic year(s).", len(years))

        results: dict[str, list[Exercise]] = {}
        for year in years:
            logger.info("Fetching year %s", year)
            year_page = client.get_html(f"/labts/home/index/{year}")
            rows = parse_year_page(year_page)

            if not rows:
                logger.info("Year %s has no exercises -- skipping.", year)
                continue

            exercises = self._fetch_exercises_for_year(client, year, rows)
            results[year] = exercises
            logger.info(
                "Year %s: %d row(s) -> %d exercise(s) after dedup.",
                year,
                len(rows),
                len(exercises),
            )

        return results

    def _fetch_exercises_for_year(
        self, client: LabtsClient, year: str, rows: list[ExerciseRow]
    ) -> list[Exercise]:
        # Dedupe by (exercise_id, repository_id): an exercise with N
        # milestones appears as N rows sharing the same exercise/repo id
        # (plan §2.4). Preserve first-seen order for stable, readable output.
        groups: dict[tuple[str, str], list[ExerciseRow]] = {}
        for row in rows:
            groups.setdefault((row.exercise_id, row.repository_id), []).append(row)

        exercises: list[Exercise] = []
        for (exercise_id, repository_id), group_rows in groups.items():
            exercises.append(
                self._fetch_one_exercise(client, year, exercise_id, repository_id, group_rows)
            )
        return exercises

    def _fetch_one_exercise(
        self,
        client: LabtsClient,
        year: str,
        exercise_id: str,
        repository_id: str,
        group_rows: list[ExerciseRow],
    ) -> Exercise:
        first_row = group_rows[0]
        milestones: list[Milestone] = []
        clone_urls: dict[str, str] = {}
        gitlab_url: str | None = None

        # One detail page per milestone row, not per exercise: the
        # submitted revision differs per milestone even though the clone
        # URLs and GitLab link are identical across all of them (plan §2.4).
        for row in group_rows:
            logger.info(
                "Fetching %s (%s), milestone %s",
                first_row.exercise_name,
                first_row.kind,
                row.milestone_id,
            )
            detail_html = client.get_html(row.detail_url)
            detail = parse_detail_page(detail_html)

            if detail.selected_milestone_id is not None and (
                detail.selected_milestone_id != row.milestone_id
            ):
                logger.debug(
                    "Detail page's selected milestone (%s) doesn't match the "
                    "row's milestone (%s) for %s -- using the row's id.",
                    detail.selected_milestone_id,
                    row.milestone_id,
                    row.detail_url,
                )

            # Clone URLs/GitLab link are identical across an exercise's
            # milestones (plan §2.4) -- keep the first ones we see.
            if not clone_urls:
                clone_urls = detail.clone_urls
                gitlab_url = detail.gitlab_url

            milestones.append(
                Milestone(
                    id=row.milestone_id,
                    name=detail.selected_milestone_name or row.year_page_label,
                    year_page_label=row.year_page_label,
                    submission_status=row.submission_status,
                    submission_state=detail.submission_state,
                    submission_state_raw=detail.submission_state_raw,
                    submitted_revision=detail.submitted_revision,
                )
            )

        return Exercise(
            exercise_name=first_row.exercise_name,
            kind=first_row.kind,
            academic_year=year,
            exercise_id=exercise_id,
            repository_id=repository_id,
            labts_url=first_row.labts_url,
            gitlab_url=gitlab_url,
            clone_urls=clone_urls,
            milestones=milestones,
        )

    def _write_json(self, ctx: PipelineContext, results: dict[str, list[Exercise]]) -> Path:
        output_path = ctx.output_dir / _OUTPUT_FILENAME
        payload = {year: [ex.to_dict() for ex in exercises] for year, exercises in results.items()}
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("Wrote %s", output_path)
        return output_path

    def _write_ssh_list(self, ctx: PipelineContext, results: dict[str, list[Exercise]]) -> Path:
        """Write the plain-text list of ssh clone URLs, grouped by year."""
        output_path = ctx.output_dir / _SSH_LIST_FILENAME

        blocks = []
        for year, exercises in results.items():
            urls = [ex.clone_urls["ssh"] for ex in exercises if ex.clone_urls.get("ssh")]
            blocks.append("\n".join([f"# {year}", *urls]))

        output_path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
        logger.info("Wrote %s", output_path)
        return output_path

    def _print_report(
        self, results: dict[str, list[Exercise]], output_path: Path, ssh_list_path: Path
    ) -> None:
        console = Console()
        total_repos = 0

        for year, exercises in results.items():
            total_repos += len(exercises)
            table = Table(title=f"{year} — {len(exercises)} repositories")
            table.add_column("Exercise")
            table.add_column("Kind")
            table.add_column("Milestone")
            table.add_column("Submitted")

            for exercise in exercises:
                for index, milestone in enumerate(exercise.milestones):
                    table.add_row(
                        exercise.exercise_name if index == 0 else "",
                        exercise.kind if index == 0 else "",
                        milestone.name,
                        _render_submission(milestone),
                    )

            console.print(table)

        console.print(
            f"{total_repos} repositories across {len(results)} academic years\n"
            f"  → {output_path}\n"
            f"  → {ssh_list_path} (ssh clone URLs only)"
        )


def _render_submission(milestone: Milestone) -> str:
    if milestone.submission_state == "submitted" and milestone.submitted_revision:
        return milestone.submitted_revision[:_SHORT_SHA_LEN]
    if milestone.submission_state in _SUBMISSION_DISPLAY:
        return _SUBMISSION_DISPLAY[milestone.submission_state]
    return milestone.submission_state_raw or milestone.submission_state
