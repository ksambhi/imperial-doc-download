"""Data model for `emarking_fetch`, built from the eMarking API's JSON.

`GET /me/{year}/exercises` returns every exercise in the department for
that year, with our submissions, feedback and mark embedded on the ones
that are ours (plan §3.2-3.3). These classes pick out the fields the
downloader actually needs; `raw` keeps the rest, so `exercise.json` on
disk stays a faithful copy of what the server said rather than a lossy
projection of it.

There is no `to_dict()` that rebuilds the API's shape and no `from_dict()`
that round-trips one of ours: the only direction that matters is API JSON
→ model, and the JSON we write is the API's own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Submission:
    """One submission of ours, which is either a file or a git commit.

    `file_path` and `gitlab_hash` are mutually exclusive in practice: a
    LabTS-backed exercise records a commit hash and has no file to
    download (plan §3.2). Asking for a commit submission's file is a 500,
    not a 404, so `has_file` is a precondition rather than a hint —
    see plan §9.2.
    """

    id: int
    exercise_id: int
    username: str | None
    timestamp: str | None
    file_path: str | None
    gitlab_hash: str | None
    target_submission_file_name: str | None
    #: The API's own idea of the size. Unreliable — six submissions report
    #: 0 and then serve real content (plan §9.6) — so it is recorded but
    #: never used to decide whether a file is complete.
    file_size: int | None

    @property
    def has_file(self) -> bool:
        return bool(self.file_path)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Submission:
        return cls(
            id=data["id"],
            exercise_id=data["exercise_id"],
            username=data.get("username"),
            timestamp=data.get("timestamp"),
            file_path=data.get("file_path"),
            gitlab_hash=data.get("gitlab_hash"),
            target_submission_file_name=data.get("target_submission_file_name"),
            file_size=data.get("file_size"),
        )


@dataclass(frozen=True)
class Feedback:
    """The feedback record for one exercise: a file, behind a distribution.

    Every feedback file is served as `<username>.pdf` regardless of module
    (plan §9.1), so `id` is what keeps them apart on disk.
    """

    id: int
    distribution_id: int
    timestamp: str | None
    marker: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Feedback:
        return cls(
            id=data["id"],
            distribution_id=data["distribution_id"],
            timestamp=data.get("timestamp"),
            marker=data.get("marker"),
        )


@dataclass(frozen=True)
class Exercise:
    """One exercise from `/me/{year}/exercises`.

    `spec`, `model_answer` and `supplementary_file` are timestamps, not
    paths: non-null means the artefact exists. That is not the same as
    being allowed to have it — every model answer is a 403 (plan §9.3).
    """

    year: str
    module_code: str
    number: int
    title: str | None
    type: str | None
    spec: str | None
    model_answer: str | None
    supplementary_file: str | None
    mark: dict[str, Any] | None
    requires_group: bool
    submissions: list[Submission] = field(default_factory=list)
    feedback: Feedback | None = None
    #: The untouched API object, written out as `exercise.json`.
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_ours(self) -> bool:
        """Whether this exercise relates to us at all (plan §3.3).

        The endpoint returns the whole department's exercises and marks
        only ours with data, so this local test needs no extra request.
        `mark` is checked against None rather than for truthiness: it is a
        nested object, and an empty one would still mean we were marked.
        """
        return bool(self.submissions) or self.feedback is not None or self.mark is not None

    @property
    def file_submissions(self) -> list[Submission]:
        """The submissions there is actually a file to download for."""
        return [s for s in self.submissions if s.has_file]

    @property
    def commit_submissions(self) -> list[Submission]:
        """Submissions that are a git commit — `gitlab_fetch`'s business."""
        return [s for s in self.submissions if not s.has_file]

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Exercise:
        feedback = data.get("feedback")
        return cls(
            year=data["year"],
            module_code=data["module_code"],
            number=data["number"],
            title=data.get("title"),
            type=data.get("type"),
            spec=data.get("spec"),
            model_answer=data.get("model_answer"),
            supplementary_file=data.get("supplementary_file"),
            mark=data.get("mark"),
            requires_group=bool(data.get("requires_group")),
            submissions=[Submission.from_api(s) for s in data.get("submissions") or []],
            feedback=Feedback.from_api(feedback) if feedback else None,
            raw=data,
        )


def ours_only(payload: list[dict[str, Any]]) -> list[Exercise]:
    """Parse a year's response and keep just the exercises that are ours.

    On the measured data this is where 471-500 exercises per year become
    16-66 (plan §9.5).
    """
    return [exercise for item in payload if (exercise := Exercise.from_api(item)).is_ours]
