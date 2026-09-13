"""Output data model for `labts_fetch`.

One `Exercise` per deduplicated `(exercise_id, repository_id)` pair -- i.e.
one GitLab repository -- holding one or more `Milestone`s. See
`docs/labts-fetch-plan.md` §2.4 for why a single exercise can have several
milestones (each with its own submitted revision), and §3 for the exact
output schema these `to_dict()` methods produce.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Milestone:
    """One milestone row for an exercise, plus what its detail page said.

    `name` is the proper name read from the detail page's milestone
    dropdown (e.g. "PintOS Task 1 - Scheduling"); `year_page_label` is the
    (sometimes ordinal-only, e.g. "1") text from the year page's Milestone
    column. Both are kept since neither alone is always as informative.

    `submission_state` is one of `"submitted"`, `"unsubmitted"`,
    `"mismatch"`, `"not_enabled"`, or `"unknown"` (an unseen banner
    variant -- see plan §2.5). `submission_state_raw` is the banner's raw
    text (or `None` when there is no banner element at all), kept so an
    unseen variant still survives into the JSON.
    """

    id: str
    name: str
    year_page_label: str
    submission_status: str
    submission_state: str
    submission_state_raw: str | None
    submitted_revision: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "year_page_label": self.year_page_label,
            "submission_status": self.submission_status,
            "submission_state": self.submission_state,
            "submission_state_raw": self.submission_state_raw,
            "submitted_revision": self.submitted_revision,
        }


@dataclass
class Exercise:
    """One GitLab repository behind a LabTS exercise, for one academic year.

    Deduplicated by `(exercise_id, repository_id)` -- an exercise with N
    milestones appears as N rows on the year page but is exactly one repo,
    hence one `Exercise` carrying N `Milestone`s.
    """

    exercise_name: str
    kind: str  # "individual" | "group"
    academic_year: str
    exercise_id: str
    repository_id: str
    labts_url: str
    gitlab_url: str | None
    clone_urls: dict[str, str]
    milestones: list[Milestone] = field(default_factory=list)

    @property
    def has_submission(self) -> bool:
        """True if any milestone has a submitted revision (plan §3)."""
        return any(m.submitted_revision for m in self.milestones)

    @property
    def submitted_revision(self) -> str | None:
        """Convenience mirror of the single milestone's revision.

        Only meaningful when there is exactly one milestone -- otherwise
        there is no single correct answer, so this is `None` and the
        authoritative values live in `milestones[].submitted_revision`
        (plan §3).
        """
        if len(self.milestones) == 1:
            return self.milestones[0].submitted_revision
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "exercise_name": self.exercise_name,
            "kind": self.kind,
            "academic_year": self.academic_year,
            "exercise_id": self.exercise_id,
            "repository_id": self.repository_id,
            "labts_url": self.labts_url,
            "gitlab_url": self.gitlab_url,
            "clone_urls": self.clone_urls,
            "has_submission": self.has_submission,
            "submitted_revision": self.submitted_revision,
            "milestones": [m.to_dict() for m in self.milestones],
        }
