"""`MaterialsFetchStep`: download and extract every module's teaching materials.

One zip per (year, module) from the enrolment list, extracted into
`<output_dir>/<year>/<module_code>/materials/` — beside that module's
`emarking/` directory, so a module's coursework and its teaching material
end up together.

The zip is **deleted once it has extracted cleanly** (plan §7.1): the
whole set is 1.75 GB and keeping both halves doubles that for no gain.
`--keep-zip` retains them. "Cleanly" is load-bearing — a failed
extraction keeps its zip so the next run can retry without
re-downloading 189 MB.

A `.materials.json` marker in each directory records what was extracted
and from which zip. Since the zip is usually gone, that marker is what
makes "already have this" answerable, and it's why a re-run costs no
requests at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from imperial_doc_download.doc_api import Download
from imperial_doc_download.emarking_fetch.models import Module, enrolled_modules
from imperial_doc_download.materials_fetch.archive import (
    ExtractionPlan,
    UnsafeArchiveError,
    extract,
)
from imperial_doc_download.materials_fetch.client import MaterialsClient
from imperial_doc_download.naming import safe_component
from imperial_doc_download.pipeline import PipelineContext, Step
from imperial_doc_download.proxy import SocksProxy

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "materials-files.json"
MARKER_FILENAME = ".materials.json"

#: Where `emarking_fetch` caches the enrolment. Read rather than
#: refetched, so running both pipelines costs one abc-api request per
#: year, not two.
ENROLMENT_CACHE_FILENAME = "emarking-enrolment.json"


@dataclass
class ModuleRecord:
    """What became of one module's materials.

    `status` is `downloaded`, `cached` (already extracted by an earlier
    run), `absent` (404 — no materials published), `unsafe` (the archive
    was refused, see `archive.py`), or `failed`.
    """

    year: str
    module_code: str
    status: str
    path: str | None = None
    zip_bytes: int | None = None
    files: int | None = None
    extracted_bytes: int | None = None
    detail: str | None = None


class MaterialsFetchStep(Step):
    """Downloads and extracts teaching materials for every enrolled module."""

    name = "materials-fetch"

    def __init__(
        self,
        *,
        force: bool = False,
        delay: float = 1.0,
        concurrency: int = 1,
        jump_host: str | None = None,
        years: list[str] | None = None,
        keep_zip: bool = False,
        strip_common_prefix: bool = True,
    ) -> None:
        # No I/O in construction, so `--dry-run` stays instant and offline.
        self._force = force
        self._delay = delay
        self._concurrency = concurrency
        self._jump_host = jump_host
        self._only_years = years
        self._keep_zip = keep_zip
        self._strip_common_prefix = strip_common_prefix

    def run(self, ctx: PipelineContext) -> None:
        settings = ctx.settings
        if not settings.username or not settings.password:
            raise RuntimeError(
                "materials-fetch requires both IMPERIAL_USERNAME and IMPERIAL_PASSWORD "
                "to be set in the environment."
            )
        if not settings.doc_ssh_key:
            raise RuntimeError(
                "materials-fetch reaches the API through an SSH SOCKS proxy on a DoC "
                "shell server, so it needs a shell-server key: set IMPERIAL_DOC_SSH_KEY "
                "or pass --doc-ssh-key."
            )

        records = asyncio.run(self._run_async(ctx))
        self._write_manifest(ctx, records)
        ctx.state["materials_files"] = records
        self._print_report(records, ctx.output_dir / MANIFEST_FILENAME)

    async def _run_async(self, ctx: PipelineContext) -> list[ModuleRecord]:
        settings = ctx.settings
        assert settings.username and settings.password and settings.doc_ssh_key

        wanted = self._targets(ctx)
        if not wanted:
            logger.warning(
                "No enrolment found under %s — run the emarking step first, or pass "
                "--year, so there are modules to fetch materials for.",
                ctx.output_dir,
            )
            return []

        pending: list[tuple[str, Module]] = []
        cached: list[ModuleRecord] = []
        for year, module in wanted:
            if self._already_have(ctx, year, module):
                cached.append(self._cached_record(ctx, year, module))
            else:
                pending.append((year, module))
        if cached:
            logger.info("%d module(s) already extracted; pass --force to redo them.", len(cached))
        if not pending:
            return cached

        logger.info("Fetching materials for %d module(s).", len(pending))
        proxy = SocksProxy(
            key=Path(settings.doc_ssh_key),
            username=settings.username,
            key_passphrase=settings.doc_ssh_key_passphrase,
            jump_host=self._jump_host,
        )
        with proxy:
            client = MaterialsClient(
                settings.username,
                settings.password,
                proxy=proxy.url,
                delay=self._delay,
                concurrency=self._concurrency,
            )
            try:
                fresh = await asyncio.gather(
                    *(self._one_module(ctx, client, year, module) for year, module in pending)
                )
            finally:
                await client.aclose()
                logger.info("Made %d request(s) to the API.", client.request_count)

        by_key = {(r.year, r.module_code): r for r in [*cached, *fresh]}
        return [by_key[(year, m.code)] for year, m in wanted if (year, m.code) in by_key]

    async def _one_module(
        self, ctx: PipelineContext, client: MaterialsClient, year: str, module: Module
    ) -> ModuleRecord:
        # The zip lands beside `materials/`, not inside it, because
        # extraction replaces that directory wholesale.
        staging_dir = self._materials_dir(ctx, year, module.code).parent
        try:
            result = await client.zipped(year, module.code, staging_dir)
        except Exception as exc:
            # One module failing must not lose a 20-minute run.
            logger.warning("Materials for %s/%s failed: %s", year, module.code, exc)
            return ModuleRecord(year, module.code, "failed", detail=str(exc))

        if result.status != "downloaded" or result.path is None:
            # 404 is the expected answer for a module with no materials
            # published — 4 of our 50.
            return ModuleRecord(year, module.code, result.status)

        return self._extract(ctx, year, module, result)

    def _extract(
        self, ctx: PipelineContext, year: str, module: Module, result: Download
    ) -> ModuleRecord:
        assert result.path is not None
        directory = self._materials_dir(ctx, year, module.code)
        try:
            extraction = extract(
                result.path,
                directory,
                strip_common_prefix=self._strip_common_prefix,
            )
        except UnsafeArchiveError as exc:
            # The zip is deliberately kept: it's the evidence, and it
            # shouldn't be silently re-downloaded next run either.
            logger.error("Refused to extract %s/%s: %s", year, module.code, exc)
            return ModuleRecord(
                year,
                module.code,
                "unsafe",
                path=self._relative(result.path, ctx.output_dir),
                zip_bytes=result.size,
                detail=str(exc),
            )
        except (OSError, ValueError) as exc:
            logger.warning("Extracting %s/%s failed: %s", year, module.code, exc)
            return ModuleRecord(year, module.code, "failed", zip_bytes=result.size, detail=str(exc))

        # Only now that the tree is on disk. 1.75 GB is a lot to keep
        # twice, but a zip whose extraction failed is worth more than the
        # bandwidth it would cost to fetch again.
        zip_name = result.path.name
        if self._keep_zip:
            result.path.replace(directory / zip_name)
        else:
            result.path.unlink(missing_ok=True)

        # Written last, deliberately: the marker is the "this module is
        # done" signal that `_already_have` reads, so anything that dies
        # earlier leaves no marker and is simply retried.
        self._write_marker(directory, zip_name, result.size, extraction, module)
        return ModuleRecord(
            year=year,
            module_code=module.code,
            status="downloaded",
            path=self._relative(directory, ctx.output_dir),
            zip_bytes=result.size,
            files=extraction.file_count,
            extracted_bytes=extraction.total_bytes,
        )

    # ------------------------------------------------------------- scope

    def _targets(self, ctx: PipelineContext) -> list[tuple[str, Module]]:
        """Every (year, module) we were enrolled in, from the shared cache."""
        stashed: dict[str, list[Module]] = ctx.state.get("emarking_modules") or {}
        targets: list[tuple[str, Module]] = []

        years = self._only_years or sorted(
            {p.parent.name for p in ctx.output_dir.glob(f"*/{ENROLMENT_CACHE_FILENAME}")}
            | set(stashed)
        )
        for year in years:
            modules = stashed.get(year) or self._cached_modules(ctx, year)
            targets += [(year, module) for module in modules]
        return targets

    @staticmethod
    def _cached_modules(ctx: PipelineContext, year: str) -> list[Module]:
        path = ctx.output_dir / year / ENROLMENT_CACHE_FILENAME
        if not path.is_file():
            return []
        try:
            return enrolled_modules(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            logger.warning("%s is unreadable — skipping year %s.", path, year)
            return []

    # ----------------------------------------------------------- caching

    def _already_have(self, ctx: PipelineContext, year: str, module: Module) -> bool:
        if self._force:
            return False
        marker = self._materials_dir(ctx, year, module.code) / MARKER_FILENAME
        return marker.is_file()

    def _cached_record(self, ctx: PipelineContext, year: str, module: Module) -> ModuleRecord:
        directory = self._materials_dir(ctx, year, module.code)
        try:
            marker = json.loads((directory / MARKER_FILENAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            marker = {}
        return ModuleRecord(
            year=year,
            module_code=module.code,
            status="cached",
            path=self._relative(directory, ctx.output_dir),
            zip_bytes=marker.get("zip_bytes"),
            files=marker.get("files"),
            extracted_bytes=marker.get("extracted_bytes"),
        )

    def _write_marker(
        self,
        directory: Path,
        zip_name: str,
        zip_bytes: int | None,
        extraction: ExtractionPlan,
        module: Module,
    ) -> None:
        payload = {
            "module_code": module.code,
            "module_title": module.title,
            "zip_name": zip_name,
            "zip_bytes": zip_bytes,
            "files": extraction.file_count,
            "extracted_bytes": extraction.total_bytes,
            "stripped_prefix": extraction.common_prefix,
            "skipped": [{"member": name, "reason": why} for name, why in extraction.skipped],
        }
        (directory / MARKER_FILENAME).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ---------------------------------------------------------- plumbing

    @staticmethod
    def _materials_dir(ctx: PipelineContext, year: str, module_code: str) -> Path:
        return ctx.output_dir / year / safe_component(module_code) / "materials"

    @staticmethod
    def _relative(path: Path, output_dir: Path) -> str:
        try:
            return str(path.relative_to(output_dir))
        except ValueError:  # pragma: no cover - always under it
            return str(path)

    def _write_manifest(self, ctx: PipelineContext, records: list[ModuleRecord]) -> None:
        by_year: dict[str, list[dict]] = {}
        for record in records:
            by_year.setdefault(record.year, []).append(asdict(record))
        path = ctx.output_dir / MANIFEST_FILENAME
        path.write_text(json.dumps({"years": by_year}, indent=2), encoding="utf-8")
        logger.info("Wrote %s", path)

    def _print_report(self, records: list[ModuleRecord], manifest_path: Path) -> None:
        console = Console()
        by_year: dict[str, list[ModuleRecord]] = {}
        for record in records:
            by_year.setdefault(record.year, []).append(record)

        for year, year_records in sorted(by_year.items()):
            table = Table(title=f"{year} — {len(year_records)} module(s)")
            table.add_column("Module")
            table.add_column("Status")
            table.add_column("Files", justify="right")
            table.add_column("Size", justify="right")
            for record in sorted(year_records, key=lambda r: r.module_code):
                table.add_row(
                    record.module_code,
                    _render_status(record.status),
                    str(record.files) if record.files is not None else "—",
                    _megabytes(record.extracted_bytes),
                )
            console.print(table)

        downloaded = [r for r in records if r.status == "downloaded"]
        failures = [r for r in records if r.status in ("failed", "unsafe")]
        total = sum(r.extracted_bytes or 0 for r in records)
        console.print(
            f"{len(downloaded)} downloaded, "
            f"{sum(1 for r in records if r.status == 'cached')} already present, "
            f"{sum(1 for r in records if r.status == 'absent')} with no materials, "
            f"{len(failures)} failed ({_megabytes(total)} extracted)"
            f"\n  → {manifest_path}"
        )
        for record in failures:
            console.print(
                f"  [red]✗[/red] {record.year}/{record.module_code}: "
                f"{record.detail or record.status}"
            )


def _render_status(status: str) -> str:
    return {
        "downloaded": "[green]downloaded[/green]",
        "cached": "[cyan]cached[/cyan]",
        "absent": "[dim]no materials[/dim]",
        "unsafe": "[red]refused[/red]",
    }.get(status, "[red]failed[/red]")


def _megabytes(value: int | None) -> str:
    if not value:
        return "—"
    return f"{value / 1_000_000:.1f} MB"
