"""Grade and status colour — the two things the page computes in the browser.

Neither is in the API. The eMarking personal-record page derives both
client-side, so reproducing the record means reproducing the arithmetic.

Pure, no I/O. Both tables are exported and written into the output file
(`marks.py`), so the record explains its own vocabulary rather than
leaving a reader to guess what `A` meant.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Grade boundaries, highest first. Inclusive at the bottom: 14/20 is
#: exactly 70.0% and the page shows `A`, not `B` ✅ (plan §4).
#:
#: A*/A/B are confirmed against the live page; C/D/F are the standard
#: Imperial scheme and are *unverified* — the lowest grade on this
#: account is a B, so there is nothing to check them against. They live
#: here, in one place, and are written into the output.
GRADE_BOUNDARIES: tuple[tuple[str, int], ...] = (
    ("A*", 80),
    ("A", 70),
    ("B", 60),
    ("C", 50),
    ("D", 40),
    ("F", 0),
)


@dataclass(frozen=True)
class Category:
    """One row of the page's colour legend."""

    category: str
    colour: str
    label: str


#: The four states of the status dot, verified against the rendered page ✅.
#:
#: Note that `unassessed-with-submission` (brown) is perfectly capable of
#: carrying a mark and a grade -- see `is_assessed`.
CATEGORIES: tuple[Category, ...] = (
    Category("individual", "green", "Individual Exercise"),
    Category("group", "purple", "Group Exercise"),
    Category("unassessed-with-submission", "brown", "Unassessed, with submission"),
    Category("unassessed-no-submission", "grey", "Unassessed, no submission"),
)

_BY_CATEGORY = {c.category: c for c in CATEGORIES}


def percentage_of(mark: float | None, maximum: float | None) -> float | None:
    """`mark` as a percentage of `maximum`, or None if that's meaningless.

    `maximum_mark` is 0 on exactly the unmarked exercises ✅, so this is a
    real division by zero and not a hypothetical one. Returning None beats
    raising: the caller always has somewhere sensible to put "no
    percentage", and a `ZeroDivisionError` at 2am is a poor way to find
    out the data has a shape you didn't expect.
    """
    if mark is None or not maximum:
        return None
    return 100.0 * mark / maximum


def grade_for(percentage: float | None) -> str | None:
    """The letter grade for a percentage, or None if there isn't one.

    Compares the percentage as-is — no rounding first. Rounding would
    promote 69.5% to an `A`, which is not what the page does.
    """
    if percentage is None:
        return None
    for grade, minimum in GRADE_BOUNDARIES:
        if percentage >= minimum:
            return grade
    return GRADE_BOUNDARIES[-1][0]  # pragma: no cover - the last row is `>= 0`


def is_assessed(weight: float | None) -> bool:
    """Whether an exercise counts toward the module result.

    **"Unassessed" means zero-weighted, not unmarked** — the trap here,
    and one the data will happily let you get wrong. A progress test can
    be submitted, marked 10/10 and graded `A*` and still be unassessed,
    because it contributes nothing to the module. The page colours those
    brown, and every observed row agrees with `weight > 0` ✅.
    """
    return bool(weight)


def category_for(*, assessed: bool, has_submission: bool, requires_group: bool) -> Category:
    """Which legend row this exercise falls under.

    **Assessment first, then group-ness.** An unassessed group exercise
    is brown or grey, not purple — there is no fifth "unassessed group"
    colour in the legend, and getting this precedence backwards is the
    easy mistake.
    """
    if not assessed:
        return _BY_CATEGORY[
            "unassessed-with-submission" if has_submission else "unassessed-no-submission"
        ]
    return _BY_CATEGORY["group" if requires_group else "individual"]


def grade_boundaries_payload() -> list[dict[str, object]]:
    """`GRADE_BOUNDARIES` in the shape the output file carries."""
    return [{"grade": grade, "min_percent": minimum} for grade, minimum in GRADE_BOUNDARIES]


def categories_payload(categories: tuple[Category, ...] = CATEGORIES) -> list[dict[str, str]]:
    """`categories` in the shape the output file carries."""
    return [{"category": c.category, "colour": c.colour, "label": c.label} for c in categories]
