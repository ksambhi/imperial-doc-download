"""`GitlabFetchStep`: clone every repository `labts_fetch` found.

Input is the LabTS output — either straight from `ctx.state` when both
steps run in the same pipeline, or re-read from `<output_dir>/labts-list.json`
when only this step is being run.

Output is one clone per repository at
`<output_dir>/<academic_year>/gitlab/<repo>/`, plus a manifest
(`<output_dir>/gitlab-clones.json`) recording what was cloned, from which
remote, and at which commit. The manifest is also the cache: a repository
already cloned successfully is left alone on the next run unless `--force`
is passed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from rich.console import Console
from rich.table import Table

from imperial_doc_download.config import Settings
from imperial_doc_download.gitlab_fetch.cloning import (
    DEFAULT_CLONE_TIMEOUT,
    CloneResult,
    CloneTarget,
    GitCloner,
    build_clone_targets,
)
from imperial_doc_download.gitlab_fetch.ssh import SshEnvironment
from imperial_doc_download.labts_fetch.models import Exercise, load_labts_list
from imperial_doc_download.pipeline import PipelineContext, Step

logger = logging.getLogger(__name__)

_MANIFEST_FILENAME = "gitlab-clones.json"
_LABTS_LIST_FILENAME = "labts-list.json"
_SHORT_SHA_LEN = 8


class GitlabFetchStep(Step):
    """Clones the DoC GitLab repositories behind every LabTS exercise."""

    name = "gitlab-fetch"

    def __init__(
        self,
        *,
        force: bool = False,
        concurrency: int = 1,
        timeout: float = DEFAULT_CLONE_TIMEOUT,
        jump_host: str | None = None,
    ) -> None:
        # Construction stays side-effect free (no I/O, no ssh setup) so
        # `--dry-run`, which never calls `run()`, stays instant and offline.
        self._force = force
        self._concurrency = concurrency
        self._timeout = timeout
        self._jump_host = jump_host

    def run(self, ctx: PipelineContext) -> None:
        settings = ctx.settings
        # Checked up front, before any of the work: being told the key is
        # missing after a minute of cloning would be needlessly annoying.
        gitlab_key = settings.gitlab_ssh_key
        if not gitlab_key:
            raise RuntimeError(
                "gitlab-fetch needs a GitLab SSH key: set IMPERIAL_GITLAB_SSH_KEY "
                "or pass --gitlab-ssh-key."
            )

        exercises = self._load_exercises(ctx)
        targets = build_clone_targets(exercises, ctx.output_dir)
        if not targets:
            logger.warning("No repositories to clone.")
            return

        manifest = self._load_manifest(ctx)
        cached, pending = self._split_cached(targets, manifest)
        if cached:
            logger.info(
                "Reusing %d repositor%s already cloned; pass --force to re-clone.",
                len(cached),
                "y" if len(cached) == 1 else "ies",
            )

        fresh = self._clone(pending, settings, gitlab_key) if pending else []
        results = self._in_target_order(targets, cached + fresh)

        self._write_manifest(ctx, results)
        ctx.state["gitlab_clones"] = results
        self._print_report(results, ctx.output_dir / _MANIFEST_FILENAME)

    def _clone(
        self, targets: list[CloneTarget], settings: Settings, gitlab_key: str
    ) -> list[CloneResult]:
        # `SshSetupError` is a RuntimeError, so a bad key or an unusable
        # passphrase surfaces as a plain message from the CLI like every
        # other "you haven't given me what I need" problem.
        ssh = SshEnvironment(
            gitlab_key=Path(gitlab_key),
            doc_key=Path(settings.doc_ssh_key) if settings.doc_ssh_key else None,
            username=settings.username,
            gitlab_key_passphrase=settings.gitlab_ssh_key_passphrase,
            doc_key_passphrase=settings.doc_ssh_key_passphrase,
            jump_host=self._jump_host,
        )

        with ssh:
            if ssh.supports_gitolite:
                logger.info("gitolite fallback enabled, proxy-jumping via %s.", ssh.jump_host)
            else:
                # Not fatal: most repositories clone straight from GitLab.
                # Only the older years really need the fallback.
                logger.warning(
                    "No gitolite fallback: it needs both IMPERIAL_DOC_SSH_KEY and "
                    "IMPERIAL_USERNAME. Repositories GitLab no longer serves will fail."
                )

            cloner = GitCloner(
                ssh.env,
                use_gitolite=ssh.supports_gitolite,
                concurrency=self._concurrency,
                timeout=self._timeout,
            )
            logger.info(
                "Cloning %d repositor%s.", len(targets), "y" if len(targets) == 1 else "ies"
            )
            return cloner.clone_all(targets)

    def _load_exercises(self, ctx: PipelineContext) -> dict[str, list[Exercise]]:
        """Take the LabTS results from the pipeline, or off disk."""
        stashed = ctx.state.get("labts_exercises")
        if stashed:
            return stashed

        labts_list = ctx.output_dir / _LABTS_LIST_FILENAME
        if not labts_list.is_file():
            raise RuntimeError(
                f"gitlab-fetch needs the LabTS repository list, but {labts_list} doesn't "
                "exist. Run the labts step first."
            )
        logger.info("Reading the repository list from %s", labts_list)
        return load_labts_list(labts_list)

    def _load_manifest(self, ctx: PipelineContext) -> dict[str, dict]:
        """Previous run's results, keyed by clone URL (empty when forcing)."""
        path = ctx.output_dir / _MANIFEST_FILENAME
        if self._force or not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("%s is unreadable — ignoring it and cloning afresh.", path)
            return {}
        return {entry["ssh_url"]: entry for entry in payload.get("repos", [])}

    def _split_cached(
        self, targets: list[CloneTarget], manifest: dict[str, dict]
    ) -> tuple[list[CloneResult], list[CloneTarget]]:
        """Partition into "already have it" and "still needs cloning".

        A manifest entry is only trusted if the clone it describes is still
        on disk — the directory may have been moved or deleted since.
        """
        cached: list[CloneResult] = []
        pending: list[CloneTarget] = []

        for target in targets:
            entry = manifest.get(target.ssh_url)
            if (
                entry
                and entry.get("status") in ("cloned", "reused")
                and _is_clone(target.destination)
            ):
                cached.append(
                    CloneResult(
                        target=target,
                        status="reused",
                        remote=entry.get("remote"),
                        head=entry.get("head"),
                        branch=entry.get("branch"),
                        checked_out_milestone=entry.get("checked_out_milestone"),
                        local_branches=entry.get("local_branches", 0),
                        mirrored_refs=entry.get("mirrored_refs"),
                    )
                )
            else:
                pending.append(target)

        return cached, pending

    @staticmethod
    def _in_target_order(
        targets: list[CloneTarget], results: list[CloneResult]
    ) -> list[CloneResult]:
        """Restore the LabTS ordering, which the cached/pending split broke."""
        by_url = {r.target.ssh_url: r for r in results}
        return [by_url[t.ssh_url] for t in targets if t.ssh_url in by_url]

    def _write_manifest(self, ctx: PipelineContext, results: list[CloneResult]) -> None:
        path = ctx.output_dir / _MANIFEST_FILENAME
        payload = {"repos": [r.to_dict() for r in results]}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("Wrote %s", path)

    def _print_report(self, results: list[CloneResult], manifest_path: Path) -> None:
        console = Console()
        by_year: dict[str, list[CloneResult]] = {}
        for result in results:
            by_year.setdefault(result.target.academic_year, []).append(result)

        for year, year_results in by_year.items():
            table = Table(title=f"{year} — {len(year_results)} repositories")
            table.add_column("Repository")
            table.add_column("Via")
            table.add_column("Status")
            table.add_column("On branch")
            table.add_column("HEAD")

            for result in year_results:
                table.add_row(
                    result.target.destination.name,
                    result.via,
                    _render_status(result),
                    _render_checkout(result),
                    (result.head or "—")[:_SHORT_SHA_LEN],
                )
            console.print(table)

        failures = [r for r in results if r.status == "failed"]
        cloned = [r for r in results if r.status == "cloned"]
        reused = [r for r in results if r.status == "reused"]

        # A repository with submissions that isn't sitting at any of them
        # cloned fine but doesn't show the submitted work — worth saying.
        not_at_submission = [
            r
            for r in results
            if r.status == "cloned" and r.target.submissions and not r.checked_out_milestone
        ]

        console.print(
            f"{len(cloned)} cloned, {len(reused)} already present, "
            f"{len(failures)} failed\n  → {manifest_path}"
        )
        for result in not_at_submission:
            console.print(
                f"  [yellow]![/yellow] {result.target.label}: left as cloned — the submitted "
                "revision isn't in the repository"
            )
        for result in failures:
            console.print(f"  [red]✗[/red] {result.target.label}: {result.error or 'unknown'}")


def _render_checkout(result: CloneResult) -> str:
    """Where the working tree was left: a branch, or detached on purpose."""
    if result.branch:
        return result.branch
    if result.checked_out_milestone:
        return "[dim]detached[/dim]"
    return "—"


def _render_status(result: CloneResult) -> str:
    if result.status == "cloned":
        return "[green]cloned[/green]"
    if result.status == "reused":
        return "[cyan]cached[/cyan]"
    return "[red]failed[/red]"


def _is_clone(path: Path) -> bool:
    """True if `path` looks like a git repository we cloned earlier."""
    return (path / ".git").exists()
