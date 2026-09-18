"""Build the marks record — the personal-record page, as JSON.

Pure: takes parsed `Exercise`s and a module-title lookup, returns the
structure written to `<output_dir>/emarking-marks.json`. No I/O, so the
interesting part is testable offline against the real rows.

Scope is **exercises with a mark** (plan §1). That is narrower than what
the download step walks: an unattempted tutorial has a spec worth keeping
but nothing to record. It also means only the `individual` and `group`
categories can appear here.

Deliberately absent: `mark.marker`, `marks_published_by` and `locked_by`
are staff usernames, and the page doesn't show them. They stay in the
per-exercise `exercise.json` the fetch step writes, faithfully; they do
not go in the one file that is a summary of *your* four years.
"""

from __future__ import annotations

from typing import Any

from imperial_doc_download.emarking_fetch.grading import (
    CATEGORIES,
    categories_payload,
    category_for,
    grade_boundaries_payload,
    grade_for,
    percentage_of,
)
from imperial_doc_download.emarking_fetch.models import Exercise, Module

#: Only these two can occur once unmarked exercises are filtered out, so
#: only these two are advertised in the file's legend.
_REACHABLE = tuple(c for c in CATEGORIES if c.category in ("individual", "group"))


def build_record(
    exercises_by_year: dict[str, list[Exercise]],
    modules_by_year: dict[str, list[Module]],
    *,
    username: str,
    generated_at: str,
) -> dict[str, Any]:
    """The whole `emarking-marks.json` payload."""
    years: dict[str, list[dict[str, Any]]] = {}
    for year in sorted(exercises_by_year):
        titles = {m.code: m.title for m in modules_by_year.get(year, [])}
        modules = _modules_for_year(exercises_by_year[year], titles)
        if modules:
            years[year] = modules

    return {
        "generated_at": generated_at,
        "username": username,
        "grade_boundaries": grade_boundaries_payload(),
        "categories": categories_payload(_REACHABLE),
        "summary": _summary(years),
        "years": years,
    }


def _modules_for_year(
    exercises: list[Exercise], titles: dict[str, str | None]
) -> list[dict[str, Any]]:
    """One year's marked exercises, grouped into modules.

    A module with nothing marked is left out entirely rather than
    appearing with an empty list.
    """
    grouped: dict[str, list[Exercise]] = {}
    for exercise in exercises:
        if exercise.is_marked:
            grouped.setdefault(exercise.module_code, []).append(exercise)

    return [
        {
            "module_code": code,
            # None when an exercise carries a code the enrolment list
            # doesn't. Shouldn't happen, but a missing title is not worth
            # failing a run over.
            "title": titles.get(code),
            "exercises": [_exercise(e) for e in sorted(grouped[code], key=lambda e: e.number)],
        }
        for code in sorted(grouped)
    ]


def _exercise(exercise: Exercise) -> dict[str, Any]:
    mark = exercise.mark or {}
    value = mark.get("mark")
    maximum = exercise.maximum_mark
    percentage = percentage_of(value, maximum)
    pass_mark = exercise.pass_mark
    category = category_for(
        marked=True,
        has_submission=bool(exercise.submissions),
        requires_group=exercise.requires_group,
    )

    return {
        "number": exercise.number,
        "title": exercise.title,
        "type": exercise.type,
        "label": label_for(exercise),
        "category": category.category,
        "colour": category.colour,
        "category_label": category.label,
        "requires_group": exercise.requires_group,
        "deadline": exercise.end,
        "extended_deadline": exercise.extended_end,
        "submitted": bool(exercise.submissions),
        "submitted_at": _submitted_at(exercise),
        "mark": value,
        "maximum_mark": maximum,
        "percentage": percentage,
        "grade": grade_for(percentage),
        "pass_mark": pass_mark,
        "pass_mark_is_percent": True,
        "passed": _passed(percentage, pass_mark),
        "weight": exercise.weight,
        "marks_published": exercise.marks_published,
        "cap": mark.get("cap"),
        "cap_reason": mark.get("cap_reason"),
        "withheld": mark.get("withheld"),
        "has_feedback": exercise.feedback is not None,
    }


def _passed(percentage: float | None, pass_mark: float | None) -> bool | None:
    """Whether the exercise was passed.

    **`pass_mark` is a percentage, not a raw mark**, so it is compared
    against the percentage. It looks like a mark and isn't: 80 of this
    account's 138 marked exercises have a `pass_mark` greater than their
    own `maximum_mark` (40 out of a maximum of 10, say), which a raw mark
    could never be. Comparing raw would report 84 failures on an account
    whose lowest grade is a B.
    """
    if percentage is None or pass_mark is None:
        return None
    return percentage >= pass_mark


def label_for(exercise: Exercise) -> str:
    """`1 | CW: Decision Trees` — exactly what the page's row reads.

    Precomputed because that string is the thing a later HTML or CSV
    rendering wants, and reassembling it from three fields is the kind of
    detail that drifts. The parts are all still in the record separately.
    """
    if exercise.type:
        return f"{exercise.number} | {exercise.type}: {exercise.title}"
    return f"{exercise.number} | {exercise.title}"


def _submitted_at(exercise: Exercise) -> str | None:
    """When we last submitted, of however many submissions there were."""
    stamps = [s.timestamp for s in exercise.submissions if s.timestamp]
    return max(stamps) if stamps else None


def _summary(years: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    modules = sum(len(m) for m in years.values())
    exercises = sum(len(module["exercises"]) for m in years.values() for module in m)
    return {"years": len(years), "modules": modules, "exercises": exercises}
