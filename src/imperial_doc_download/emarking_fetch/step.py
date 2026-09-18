"""`EmarkingFetchStep`: download every coursework artefact from eMarking.

One request per academic year yields that year's exercises with our
submissions, feedback and marks embedded (plan §3.2), which is enough to
know every artefact worth asking for without another metadata call. What
follows is one download per artefact, into the layout in plan §4:

    <output_dir>/<year>/<module_code>/emarking/
    ├── exercises.json                  this module's exercises
    └── <number>-<title>/
        ├── exercise.json
        ├── <spec>.pdf
        ├── model-answer/<filename>
        ├── supplementary/<filename>
        ├── submissions/<id>/<filename>
        └── feedback/<id>/{metadata.json,<filename>}

Everything the run did lands in `<output_dir>/emarking-files.json`, which
is both the record and the cache: a file already downloaded is left alone
on the next run, and an outcome the API has settled (a 403 on a model
answer, a 404 on a missing artefact) is not asked about again. `--force`
ignores all of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from imperial_doc_download.emarking_fetch.client import (
    Download,
    EmarkingClient,
)
from imperial_doc_download.emarking_fetch.models import (
    Exercise,
    Module,
    Submission,
    enrolled_modules,
    for_modules,
)
from imperial_doc_download.naming import exercise_directory, safe_component
from imperial_doc_download.pipeline import PipelineContext, Step
from imperial_doc_download.proxy import SocksProxy

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "emarking-files.json"
YEAR_CACHE_FILENAME = "emarking-exercises.json"
ENROLMENT_CACHE_FILENAME = "emarking-enrolment.json"

#: Stamped into the year cache so a re-run can tell which rule produced
#: it. The scope widened from "exercises we worked on" to "exercises in
#: modules we were enrolled in" (plan §3.3.1), and a cache written under
#: the old rule is missing ~40% of the rows -- reusing it would look
#: exactly like a successful run.
CACHE_SCOPE = "enrolled-modules"

#: Outcomes the API has settled. Re-asking would earn the same answer, so
#: a re-run skips them unless `--force` (plan §9.3).
_TERMINAL = frozenset({"absent", "forbidden"})


@dataclass
class FileRecord:
    """One artefact we asked about, and what became of it.

    `status` is `downloaded`, `cached` (already on disk from an earlier
    run), `absent` (404), `forbidden` (403), `skipped` (never requested —
    a submission that is a git commit), or `failed`.
    """

    year: str
    module_code: str
    number: int
    kind: str
    url: str
    status: str
    path: str | None = None
    size: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class _Artefact:
    """One thing to download, resolved to a URL and a destination."""

    kind: str
    url: str
    directory: Path
    preferred_name: str | None = None
    fallback_stem: str = "download"


class EmarkingFetchStep(Step):
    """Downloads coursework specs, submissions and feedback from eMarking."""

    name = "emarking-fetch"

    def __init__(
        self,
        *,
        force: bool = False,
        delay: float = 1.0,
        concurrency: int = 1,
        jump_host: str | None = None,
        years: list[str] | None = None,
        model_answers: bool = False,
    ) -> None:
        # Construction stays side-effect free (no I/O, no ssh) so
        # `--dry-run`, which never calls `run()`, stays instant and offline.
        self._force = force
        self._delay = delay
        self._concurrency = concurrency
        self._jump_host = jump_host
        self._only_years = years
        #: Model answers are a 403 every time (45/45 probed), and that is
        #: the system working: releasing them would leak future years'
        #: answers. Off by default so a cold run doesn't spend 117
        #: requests being refused (plan §9.3.1).
        self._model_answers = model_answers
        #: Years we weren't enrolled in, carried across runs in the
        #: manifest so they aren't re-probed. `--force`, or naming a year
        #: with `--year`, checks again -- which matters for the current
        #: academic year, since it can gain data after an empty run.
        self._empty_years: set[str] = set()
        #: Enrolled modules and scoped exercises per year, handed to the
        #: marks step downstream.
        self._modules: dict[str, list[Module]] = {}
        self._exercises: dict[str, list[Exercise]] = {}

    def run(self, ctx: PipelineContext) -> None:
        settings = ctx.settings
        # All checked up front: being told the key is missing after twenty
        # minutes of downloading would be needlessly annoying.
        if not settings.username or not settings.password:
            raise RuntimeError(
                "emarking-fetch requires both IMPERIAL_USERNAME and IMPERIAL_PASSWORD "
                "to be set in the environment. Note that `source`-ing a .env file "
                "truncates a password containing shell metacharacters — export the "
                "variables or let the tool read them literally."
            )
        if not settings.doc_ssh_key:
            raise RuntimeError(
                "emarking-fetch reaches the API through an SSH SOCKS proxy on a DoC "
                "shell server, so it needs a shell-server key: set IMPERIAL_DOC_SSH_KEY "
                "or pass --doc-ssh-key."
            )

        records = asyncio.run(self._run_async(ctx))
        self._write_manifest(ctx, records)
        ctx.state["emarking_files"] = records
        # Handed to `emarking-marks` in the same pipeline so it needs no
        # requests of its own; it falls back to reading these off disk
        # when run on its own.
        ctx.state["emarking_exercises"] = self._exercises
        ctx.state["emarking_modules"] = self._modules
        self._print_report(records, ctx.output_dir / MANIFEST_FILENAME)

    async def _run_async(self, ctx: PipelineContext) -> list[FileRecord]:
        settings = ctx.settings
        assert settings.username and settings.password and settings.doc_ssh_key

        previous = self._load_manifest(ctx)

        proxy = SocksProxy(
            key=Path(settings.doc_ssh_key),
            username=settings.username,
            key_passphrase=settings.doc_ssh_key_passphrase,
            jump_host=self._jump_host,
        )
        with proxy:
            client = EmarkingClient(
                settings.username,
                settings.password,
                proxy=proxy.url,
                delay=self._delay,
                concurrency=self._concurrency,
            )
            try:
                records = await self._fetch_everything(ctx, client, previous)
            finally:
                await client.aclose()
                logger.info("Made %d request(s) to the API.", client.request_count)
        return records

    async def _fetch_everything(
        self,
        ctx: PipelineContext,
        client: EmarkingClient,
        previous: dict[str, FileRecord],
    ) -> list[FileRecord]:
        # Naming years explicitly is also a way to re-check one that was
        # empty last time — the current academic year, say.
        years = self._only_years or await client.years()
        logger.info("Checking %d academic year(s).", len(years))

        records: list[FileRecord] = []
        for year in years:
            if year in self._empty_years and not self._only_years:
                logger.debug("Year %s had nothing for us last run — not asking again.", year)
                continue

            modules = await self._modules_for_year(ctx, client, year)
            if not modules:
                # abc-api answers `200 []` for a year we weren't enrolled
                # in ✅, so this is a clean answer rather than an error —
                # and asking it first means we never provoke the 500 that
                # /me/{year}/exercises returns for those years.
                self._empty_years.add(year)
                continue

            exercises = await self._exercises_for_year(ctx, client, year, modules)
            if not exercises:
                self._empty_years.add(year)
                continue

            self._empty_years.discard(year)
            self._modules[year] = modules
            self._exercises[year] = exercises
            worked_on = sum(1 for e in exercises if e.is_ours)
            logger.info(
                "Year %s: %d exercise(s) across %d enrolled module(s); "
                "%d you worked on, %d marked.",
                year,
                len(exercises),
                len({e.module_code for e in exercises}),
                worked_on,
                sum(1 for e in exercises if e.is_marked),
            )
            self._write_year_metadata(ctx, year, exercises)
            records += await self._fetch_year_artefacts(ctx, client, year, exercises, previous)

        return records

    async def _modules_for_year(
        self, ctx: PipelineContext, client: EmarkingClient, year: str
    ) -> list[Module]:
        """Modules we were enrolled in, from the cache or from abc-api.

        Only the code and title are ever written out — the record this
        comes from also carries our cid, email, name and personal tutor
        (plan §3.3.1).
        """
        cache = ctx.output_dir / year / ENROLMENT_CACHE_FILENAME
        if not self._force and cache.is_file():
            try:
                payload = json.loads(cache.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("%s is unreadable — refetching it.", cache)
            else:
                return enrolled_modules(payload)

        payload = await client.enrolment(year)
        if not payload:
            return []

        modules = enrolled_modules(payload)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps([{"code": m.code, "title": m.title} for m in modules], indent=2),
            encoding="utf-8",
        )
        return modules

    async def _exercises_for_year(
        self,
        ctx: PipelineContext,
        client: EmarkingClient,
        year: str,
        modules: list[Module],
    ) -> list[Exercise]:
        """This year's exercises in our modules, from the cache or the API."""
        cache = ctx.output_dir / year / YEAR_CACHE_FILENAME
        if not self._force and cache.is_file():
            cached = read_year_cache(cache, year)
            if cached is not None:
                logger.info("Reusing %s (pass --force to refetch).", cache)
                return for_modules(cached, modules)

        payload = await client.exercises(year)
        if payload is None:
            return []

        exercises = for_modules(payload, modules)
        logger.debug(
            "Year %s: %d exercise(s) returned, %d in our %d enrolled module(s).",
            year,
            len(payload),
            len(exercises),
            len(modules),
        )
        if not exercises:
            return []

        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps({"scope": CACHE_SCOPE, "exercises": [e.raw for e in exercises]}, indent=2),
            encoding="utf-8",
        )
        return exercises

    def _write_year_metadata(
        self, ctx: PipelineContext, year: str, exercises: list[Exercise]
    ) -> None:
        """`exercises.json` per module, `exercise.json` per exercise."""
        by_module: dict[str, list[Exercise]] = {}
        for exercise in exercises:
            by_module.setdefault(exercise.module_code, []).append(exercise)

        for module_code, module_exercises in by_module.items():
            module_dir = self._module_dir(ctx, year, module_code)
            module_dir.mkdir(parents=True, exist_ok=True)
            (module_dir / "exercises.json").write_text(
                json.dumps([e.raw for e in module_exercises], indent=2),
                encoding="utf-8",
            )
            for exercise in module_exercises:
                directory = self._exercise_dir(ctx, exercise)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "exercise.json").write_text(
                    json.dumps(exercise.raw, indent=2), encoding="utf-8"
                )

    async def _fetch_year_artefacts(
        self,
        ctx: PipelineContext,
        client: EmarkingClient,
        year: str,
        exercises: list[Exercise],
        previous: dict[str, FileRecord],
    ) -> list[FileRecord]:
        tasks = []
        records: list[FileRecord] = []

        for exercise in exercises:
            # Commit-hash submissions are recorded, never requested: that
            # endpoint answers 500 for them (plan §9.2), and the code
            # itself is what `gitlab_fetch` clones.
            for submission in exercise.commit_submissions:
                records.append(_skipped_commit(exercise, submission))

            if exercise.model_answer and not self._model_answers:
                records.append(_skipped_model_answer(exercise))

            for artefact in self._artefacts_for(ctx, exercise):
                cached = self._reuse(artefact, exercise, previous, ctx.output_dir)
                if cached is not None:
                    records.append(cached)
                    continue
                tasks.append(self._download(client, artefact, exercise, ctx.output_dir))

        if tasks:
            records += await asyncio.gather(*tasks)
        return records

    async def _download(
        self,
        client: EmarkingClient,
        artefact: _Artefact,
        exercise: Exercise,
        output_dir: Path,
    ) -> FileRecord:
        try:
            result = await client.download(
                artefact.url,
                artefact.directory,
                preferred_name=artefact.preferred_name,
                fallback_stem=artefact.fallback_stem,
            )
        except Exception as exc:
            # One artefact failing must not lose the rest of a long run;
            # it's recorded as failed and retried on the next one.
            logger.warning("GET %s failed: %s", artefact.url, exc)
            return _record(exercise, artefact, "failed", detail=str(exc))

        return _record(
            exercise,
            artefact,
            result.status,
            path=_relative(result, output_dir),
            size=result.size,
        )

    def _artefacts_for(self, ctx: PipelineContext, exercise: Exercise) -> list[_Artefact]:
        """Every artefact this exercise's metadata says exists.

        The `spec`/`model_answer`/`supplementary_file` timestamps are what
        stops us firing requests we already know would 404 (plan §3.2) —
        though a non-null `model_answer` still turns out to be a 403 every
        time (plan §9.3), which is recorded rather than retried.
        """
        base = f"/{exercise.year}/{exercise.module_code}/exercises/{exercise.number}"
        directory = self._exercise_dir(ctx, exercise)
        artefacts: list[_Artefact] = []

        if exercise.spec:
            artefacts.append(_Artefact("spec", f"{base}/spec", directory, fallback_stem="spec"))
        if exercise.model_answer and self._model_answers:
            artefacts.append(
                _Artefact(
                    "model-answer",
                    f"{base}/model-answer",
                    directory / "model-answer",
                    fallback_stem="model-answer",
                )
            )
        if exercise.supplementary_file:
            artefacts.append(
                _Artefact(
                    "supplementary",
                    f"{base}/supplementary",
                    directory / "supplementary",
                    fallback_stem="supplementary",
                )
            )

        for submission in exercise.file_submissions:
            artefacts.append(
                _Artefact(
                    "submission",
                    f"{base}/submissions/{submission.id}/file",
                    directory / "submissions" / str(submission.id),
                    # The header's name carries an opaque uuid prefix, so
                    # the metadata's name is the better one (plan §9.1).
                    preferred_name=submission.target_submission_file_name,
                    fallback_stem="submission",
                )
            )

        if exercise.feedback is not None:
            feedback_dir = directory / "feedback" / str(exercise.feedback.id)
            feedback_dir.mkdir(parents=True, exist_ok=True)
            (feedback_dir / "metadata.json").write_text(
                json.dumps(asdict(exercise.feedback), indent=2), encoding="utf-8"
            )
            artefacts.append(
                _Artefact(
                    "feedback",
                    f"/distributions/{exercise.feedback.distribution_id}"
                    f"/feedback/{exercise.feedback.id}/file",
                    feedback_dir,
                    fallback_stem="feedback",
                )
            )

        return artefacts

    @staticmethod
    def _reuse(
        artefact: _Artefact,
        exercise: Exercise,
        previous: dict[str, FileRecord],
        output_dir: Path,
    ) -> FileRecord | None:
        """The earlier run's answer, when there's no point asking again."""
        record = previous.get(artefact.url)
        if record is None:
            return None

        if record.status in _TERMINAL:
            return _record(exercise, artefact, record.status, detail=record.detail)

        # A manifest entry is only trusted if the file it describes is
        # still on disk — it may have been moved or deleted since.
        if record.status in ("downloaded", "cached") and record.path:
            if (output_dir / record.path).is_file():
                return _record(exercise, artefact, "cached", path=record.path, size=record.size)

        return None

    def _load_manifest(self, ctx: PipelineContext) -> dict[str, FileRecord]:
        """The previous run's records, keyed by URL (empty when forcing)."""
        path = ctx.output_dir / MANIFEST_FILENAME
        if self._force or not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("%s is unreadable — ignoring it and fetching afresh.", path)
            return {}

        self._empty_years = set(payload.get("empty_years", []))

        records: dict[str, FileRecord] = {}
        for entries in payload.get("years", {}).values():
            for entry in entries:
                try:
                    record = FileRecord(**entry)
                except TypeError:
                    logger.debug("Ignoring an unrecognised manifest entry: %r", entry)
                    continue
                records[record.url] = record
        logger.info("Read %d record(s) from %s.", len(records), path)
        return records

    def _write_manifest(self, ctx: PipelineContext, records: list[FileRecord]) -> None:
        by_year: dict[str, list[dict]] = {}
        for record in records:
            by_year.setdefault(record.year, []).append(asdict(record))

        path = ctx.output_dir / MANIFEST_FILENAME
        payload = {
            "years": by_year,
            # Remembered so the next run doesn't ask about them again.
            "empty_years": sorted(self._empty_years),
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("Wrote %s", path)

    def _module_dir(self, ctx: PipelineContext, year: str, module_code: str) -> Path:
        return ctx.output_dir / year / safe_component(module_code) / "emarking"

    def _exercise_dir(self, ctx: PipelineContext, exercise: Exercise) -> Path:
        return self._module_dir(ctx, exercise.year, exercise.module_code) / exercise_directory(
            exercise.number, exercise.title
        )

    def _print_report(self, records: list[FileRecord], manifest_path: Path) -> None:
        console = Console()
        by_year: dict[str, list[FileRecord]] = {}
        for record in records:
            by_year.setdefault(record.year, []).append(record)

        kinds = ("spec", "model-answer", "supplementary", "submission", "feedback")
        for year, year_records in sorted(by_year.items()):
            table = Table(title=f"{year} — {len(year_records)} artefact(s)")
            table.add_column("Artefact")
            for column in ("Downloaded", "Cached", "Absent", "Forbidden", "Skipped", "Failed"):
                table.add_column(column, justify="right")

            for kind in kinds:
                of_kind = [r for r in year_records if r.kind == kind]
                if not of_kind:
                    continue
                table.add_row(kind, *(_count(of_kind, s) for s in _STATUSES))
            console.print(table)

        failures = [r for r in records if r.status == "failed"]
        console.print(
            f"{_total(records, 'downloaded')} downloaded, "
            f"{_total(records, 'cached')} already present, "
            f"{len(failures)} failed "
            f"({_downloaded_bytes(records)})\n  → {manifest_path}"
        )

        forbidden = [r for r in records if r.status == "forbidden"]
        if forbidden:
            # Expected, not a fault — but silently omitting 45 model
            # answers would look like they were never there.
            console.print(
                f"  [yellow]![/yellow] {len(forbidden)} artefact(s) exist but aren't ours to "
                "download (403) — model answers are never released to students."
            )
        for record in failures:
            console.print(
                f"  [red]✗[/red] {record.year}/{record.module_code}/{record.number} "
                f"{record.kind}: {record.detail or 'unknown'}"
            )


_STATUSES = ("downloaded", "cached", "absent", "forbidden", "skipped", "failed")


def _total(records: list[FileRecord], status: str) -> int:
    return sum(1 for r in records if r.status == status)


def _count(records: list[FileRecord], status: str) -> str:
    """A table cell: an em dash reads better than a column of zeroes."""
    total = _total(records, status)
    return str(total) if total else "—"


def _downloaded_bytes(records: list[FileRecord]) -> str:
    total = sum(r.size or 0 for r in records if r.status == "downloaded")
    if total < 1_000_000:
        return f"{total / 1000:.0f} KB"
    return f"{total / 1_000_000:.1f} MB"


def _relative(result: Download, output_dir: Path) -> str | None:
    if result.path is None:
        return None
    try:
        return str(result.path.relative_to(output_dir))
    except ValueError:  # pragma: no cover - the path is always under it
        return str(result.path)


def _record(
    exercise: Exercise,
    artefact: _Artefact,
    status: str,
    *,
    path: str | None = None,
    size: int | None = None,
    detail: str | None = None,
) -> FileRecord:
    return FileRecord(
        year=exercise.year,
        module_code=exercise.module_code,
        number=exercise.number,
        kind=artefact.kind,
        url=artefact.url,
        status=status,
        path=path,
        size=size,
        detail=detail,
    )


def read_year_cache(cache: Path, year: str) -> list[dict] | None:
    """The cached year response, or None if it can't be trusted.

    A cache written before the scope widened is a bare JSON list and
    holds only the exercises we worked on. Honouring it would quietly
    drop the tutorials and optional courseworks the new scope exists to
    collect, and the run would look entirely successful — so an unmarked
    or mismatched cache is refetched, not reused.
    """
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("%s is unreadable — refetching year %s.", cache, year)
        return None

    if not isinstance(payload, dict):
        logger.info(
            "%s predates the enrolment-based scope, so it's missing exercises from "
            "your modules that you didn't submit to — refetching year %s.",
            cache,
            year,
        )
        return None
    if payload.get("scope") != CACHE_SCOPE:
        logger.info(
            "%s was written with scope %r, not %r — refetching year %s.",
            cache,
            payload.get("scope"),
            CACHE_SCOPE,
            year,
        )
        return None
    return list(payload.get("exercises") or [])


def _skipped_model_answer(exercise: Exercise) -> FileRecord:
    """Recorded, not requested — and not a failure.

    Every model answer is a 403 (45/45 probed), and that is eMarking
    working as intended rather than something to route around: model
    answers are withheld from students so they can't leak into the next
    year's coursework.
    """
    return FileRecord(
        year=exercise.year,
        module_code=exercise.module_code,
        number=exercise.number,
        kind="model-answer",
        url=(f"/{exercise.year}/{exercise.module_code}/exercises/{exercise.number}/model-answer"),
        status="skipped",
        detail="not released to students (always 403) — pass --model-answers to try anyway",
    )


def _skipped_commit(exercise: Exercise, submission: Submission) -> FileRecord:
    return FileRecord(
        year=exercise.year,
        module_code=exercise.module_code,
        number=exercise.number,
        kind="submission",
        url=(
            f"/{exercise.year}/{exercise.module_code}/exercises/{exercise.number}"
            f"/submissions/{submission.id}/file"
        ),
        status="skipped",
        detail=(
            f"submission is a git commit ({(submission.gitlab_hash or '')[:8]}) "
            "— gitlab-fetch has the code"
        ),
    )
