"""Offline tests for `imperial_doc_download.emarking_fetch.grading`.

The grade and the status colour are computed client-side by the page, so
these are the two things we reproduce rather than read. Every example
that appears in the real personal record is checked against the value the
page actually renders.
"""

from __future__ import annotations

import pytest

from imperial_doc_download.emarking_fetch.grading import (
    CATEGORIES,
    GRADE_BOUNDARIES,
    categories_payload,
    category_for,
    grade_boundaries_payload,
    grade_for,
    percentage_of,
)


class TestPercentageOf:
    @pytest.mark.parametrize(
        ("mark", "maximum", "expected"),
        [
            (14, 20, 70.0),
            (17, 20, 85.0),
            (98, 100, 98.0),
            (100, 100, 100.0),
            (87, 100, 87.0),
            (0, 20, 0.0),
        ],
    )
    def test_real_rows_from_the_record(self, mark, maximum, expected) -> None:
        assert percentage_of(mark, maximum) == expected

    def test_a_zero_maximum_is_none_not_an_exception(self) -> None:
        # maximum_mark is 0 on exactly the unmarked exercises, so this is
        # a real division by zero and not a hypothetical one.
        assert percentage_of(5, 0) is None

    def test_missing_values_are_none(self) -> None:
        assert percentage_of(None, 20) is None
        assert percentage_of(14, None) is None
        assert percentage_of(None, None) is None


class TestGradeFor:
    @pytest.mark.parametrize(
        ("percentage", "expected"),
        [
            (100.0, "A*"),
            (98.0, "A*"),
            (87.0, "A*"),
            (85.0, "A*"),
            (80.0, "A*"),
            (79.9, "A"),
            (70.0, "A"),
            (69.9, "B"),
            (60.0, "B"),
            (59.9, "C"),
            (50.0, "C"),
            (49.9, "D"),
            (40.0, "D"),
            (39.9, "F"),
            (0.0, "F"),
        ],
    )
    def test_every_boundary(self, percentage: float, expected: str) -> None:
        assert grade_for(percentage) == expected

    def test_the_one_case_checkable_against_the_rendered_page(self) -> None:
        # 14/20 is exactly 70.0% and the page shows `A`. That fixes the
        # boundary as inclusive at the bottom, and rules out rounding.
        assert grade_for(percentage_of(14, 20)) == "A"

    def test_no_rounding_promotes_a_near_miss(self) -> None:
        assert grade_for(69.5) == "B"
        assert grade_for(79.99) == "A"

    def test_no_percentage_means_no_grade(self) -> None:
        assert grade_for(None) is None

    def test_boundaries_are_ordered_high_to_low(self) -> None:
        minimums = [minimum for _, minimum in GRADE_BOUNDARIES]
        assert minimums == sorted(minimums, reverse=True)
        assert minimums[-1] == 0  # so every percentage lands somewhere


class TestCategoryFor:
    def test_marked_individual_is_green(self) -> None:
        result = category_for(marked=True, has_submission=True, requires_group=False)
        assert (result.category, result.colour) == ("individual", "green")

    def test_marked_group_is_purple(self) -> None:
        result = category_for(marked=True, has_submission=True, requires_group=True)
        assert (result.category, result.colour) == ("group", "purple")

    def test_marked_without_a_submission_still_takes_its_colour(self) -> None:
        # The 60015 row: marked 87/100, never submitted, no feedback.
        result = category_for(marked=True, has_submission=False, requires_group=False)
        assert result.colour == "green"

    def test_unassessed_with_a_submission_is_brown(self) -> None:
        result = category_for(marked=False, has_submission=True, requires_group=False)
        assert (result.category, result.colour) == ("unassessed-with-submission", "brown")

    def test_unassessed_without_a_submission_is_grey(self) -> None:
        result = category_for(marked=False, has_submission=False, requires_group=False)
        assert (result.category, result.colour) == ("unassessed-no-submission", "grey")

    @pytest.mark.parametrize("has_submission", [True, False])
    def test_assessment_beats_group_ness(self, has_submission: bool) -> None:
        """The easy mistake: an unmarked *group* exercise is not purple.

        There is no fifth "unassessed group" swatch in the legend, so
        group-ness only decides the colour once something is marked.
        """
        result = category_for(marked=False, has_submission=has_submission, requires_group=True)
        assert result.colour != "purple"
        assert result.category.startswith("unassessed-")


class TestPayloads:
    def test_grade_boundaries_payload_shape(self) -> None:
        payload = grade_boundaries_payload()
        assert payload[0] == {"grade": "A*", "min_percent": 80}
        assert len(payload) == len(GRADE_BOUNDARIES)

    def test_categories_payload_shape(self) -> None:
        payload = categories_payload()
        assert len(payload) == len(CATEGORIES)
        assert {"category", "colour", "label"} == set(payload[0])
        assert payload[0]["label"] == "Individual Exercise"

    def test_categories_payload_can_be_narrowed(self) -> None:
        only_group = tuple(c for c in CATEGORIES if c.category == "group")
        assert [c["colour"] for c in categories_payload(only_group)] == ["purple"]
