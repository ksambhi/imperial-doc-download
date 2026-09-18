"""Offline tests for `imperial_doc_download.labts_fetch.parsing`.

Every fixture here is real (anonymised) LabTS markup -- see
`docs/labts-fetch-plan.md` §6. Nothing in this file touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imperial_doc_download.labts_fetch import parsing
from imperial_doc_download.labts_fetch.models import Exercise, Milestone

FIXTURES = Path(__file__).parent / "fixtures" / "labts"

EXPECTED_YEARS = [
    "2627",
    "2526",
    "2425",
    "2324",
    "2223",
    "2122",
    "2021",
    "1920",
    "1819",
    "1718",
    "1617",
    "1516",
    "1415",
]


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- academic years ---------------------------------------------------------


def test_parse_academic_years_returns_all_thirteen_years() -> None:
    years = parsing.parse_academic_years(_fixture("home_year_blank.html"))
    assert years == EXPECTED_YEARS


def test_parse_academic_years_present_on_a_year_with_data_too() -> None:
    years = parsing.parse_academic_years(_fixture("home_year_multi_milestone.html"))
    assert years == EXPECTED_YEARS


def test_parse_academic_years_missing_dropdown_returns_empty_list() -> None:
    assert parsing.parse_academic_years("<html><body>nothing here</body></html>") == []


# --- sign-in / csrf token -----------------------------------------------------


def test_parse_csrf_token_extracts_the_hidden_form_field() -> None:
    token = parsing.parse_csrf_token(_fixture("sign_in.html"))
    assert token == (
        "1eOK8sRCl9qEHwu05CSN/iADvcojtx9V9DiWcVQc4uLoqId4sYbMCy5lTREfOn8Q4vnakpEK5tU+WP+WWEy1Sg=="
    )


def test_parse_csrf_token_missing_form_raises() -> None:
    with pytest.raises(ValueError):
        parsing.parse_csrf_token("<html><body>no form here</body></html>")


# --- year pages: blank vs. populated -----------------------------------------


def test_blank_year_page_has_zero_rows() -> None:
    rows = parsing.parse_year_page(_fixture("home_year_blank.html"))
    assert rows == []


def test_year_page_has_24_rows_across_both_sections() -> None:
    rows = parsing.parse_year_page(_fixture("home_year_multi_milestone.html"))
    assert len(rows) == 24
    assert sum(1 for r in rows if r.kind == "individual") == 18
    assert sum(1 for r in rows if r.kind == "group") == 6


def test_year_page_rows_dedupe_to_20_exercises() -> None:
    rows = parsing.parse_year_page(_fixture("home_year_multi_milestone.html"))
    keys = {(r.exercise_id, r.repository_id) for r in rows}
    assert len(keys) == 20


def test_pintos_has_exactly_three_rows_with_three_distinct_milestone_ids() -> None:
    rows = parsing.parse_year_page(_fixture("home_year_multi_milestone.html"))
    pintos_rows = [r for r in rows if r.exercise_name == "pintos"]

    assert len(pintos_rows) == 3
    assert {r.exercise_id for r in pintos_rows} == {"809"}
    assert {r.repository_id for r in pintos_rows} == {"99068"}
    assert [r.milestone_id for r in pintos_rows] == ["1121", "1122", "1123"]


def test_year_page_row_fields_for_a_single_milestone_exercise() -> None:
    rows = parsing.parse_year_page(_fixture("home_year_multi_milestone.html"))
    sed3 = next(r for r in rows if r.exercise_name == "SED_Ex3")

    assert sed3.kind == "individual"
    assert sed3.academic_year == "2324"
    assert sed3.exercise_id == "784"
    assert sed3.repository_id == "100851"
    assert sed3.milestone_id == "1095"
    assert sed3.year_page_label == "1"
    assert sed3.submission_status == "Invalid submission"
    assert sed3.labts_url == (
        "https://teaching.doc.ic.ac.uk/labts/lab_exercises/2324/exercises/784/repository/100851"
    )
    assert sed3.detail_url == sed3.labts_url + "?milestone=1095"


def test_year_page_row_label_carries_full_name_for_group_exercises() -> None:
    rows = parsing.parse_year_page(_fixture("home_year_multi_milestone.html"))
    pintos_first = next(r for r in rows if r.exercise_name == "pintos")
    assert pintos_first.year_page_label == "1: PintOS Task 1 - Scheduling"


# --- detail pages: the four submission states --------------------------------


def test_detail_submitted_state_has_a_revision() -> None:
    detail = parsing.parse_detail_page(_fixture("detail_submitted.html"))

    assert detail.submission_state == "submitted"
    assert detail.submitted_revision == "0cd1be554b400831e14d2327c890d236fa964d53"
    assert detail.submission_state_raw is not None
    assert "Submitted revision" in detail.submission_state_raw
    assert detail.selected_milestone_id == "1426"
    assert detail.selected_milestone_name == "Make machine learning deep"


def test_detail_unsubmitted_state_has_no_revision() -> None:
    detail = parsing.parse_detail_page(_fixture("detail_unsubmitted.html"))

    assert detail.submission_state == "unsubmitted"
    assert detail.submitted_revision is None
    assert detail.submission_state_raw == "Currently Unsubmitted"


def test_detail_mismatch_state_has_no_revision() -> None:
    detail = parsing.parse_detail_page(_fixture("detail_mismatch.html"))

    assert detail.submission_state == "mismatch"
    assert detail.submitted_revision is None
    assert detail.submission_state_raw == "Warning: Submitted Commit Does Not Match This Repo"


def test_detail_submission_not_enabled_has_no_banner_element_at_all() -> None:
    detail = parsing.parse_detail_page(_fixture("detail_submission_not_enabled.html"))

    assert detail.submission_state == "not_enabled"
    assert detail.submission_state_raw is None
    assert detail.submitted_revision is None
    # It's still a fully clonable repo -- not_enabled is a valid state, not
    # an error, and shouldn't stop us from getting the clone URLs.
    assert detail.clone_urls


# --- detail pages: multi-milestone dropdown ----------------------------------


def test_detail_multi_milestone_dropdown_lists_all_three_options() -> None:
    detail = parsing.parse_detail_page(_fixture("detail_multi_milestone.html"))

    assert [m.id for m in detail.milestones] == ["1121", "1122", "1123"]
    assert [m.name for m in detail.milestones] == [
        "PintOS Task 1 - Scheduling",
        "PintOS Task 2 - User Programs",
        "PintOS Task 3 - Virtual Memory",
    ]
    assert detail.selected_milestone_id == "1122"
    assert detail.selected_milestone_name == "PintOS Task 2 - User Programs"


def test_detail_single_milestone_dropdown_has_one_option() -> None:
    detail = parsing.parse_detail_page(_fixture("detail_submitted.html"))
    assert len(detail.milestones) == 1
    assert detail.milestones[0].selected is True


# --- clone URLs ---------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name",
    [
        "detail_submitted.html",
        "detail_multi_milestone.html",
        "detail_unsubmitted.html",
        "detail_mismatch.html",
        "detail_submission_not_enabled.html",
    ],
)
def test_clone_urls_parsed_for_both_ssh_and_https(fixture_name: str) -> None:
    detail = parsing.parse_detail_page(_fixture(fixture_name))

    assert set(detail.clone_urls) == {"ssh", "https"}
    assert detail.clone_urls["ssh"].startswith("git@gitlab.doc.ic.ac.uk:")
    assert detail.clone_urls["ssh"].endswith(".git")
    assert detail.clone_urls["https"].startswith("https://gitlab.doc.ic.ac.uk/")
    assert detail.clone_urls["https"].endswith(".git")
    # The `git clone ` prefix must be stripped from both.
    assert "git clone" not in detail.clone_urls["ssh"]
    assert "git clone" not in detail.clone_urls["https"]


def test_gitlab_repo_link_is_extracted() -> None:
    detail = parsing.parse_detail_page(_fixture("detail_submitted.html"))
    assert detail.gitlab_url == "https://gitlab.doc.ic.ac.uk/lab2526_spring/70010_DL_CW_1_abc123"


# --- privacy: group members must never leak ----------------------------------


def test_group_members_never_appear_in_a_serialised_exercise() -> None:
    html = _fixture("detail_multi_milestone.html")
    fake_names_and_logins = [
        "Alex Example",
        "Blake Sample",
        "Casey Placeholder",
        "aex21",
        "bsa21",
        "cpl21",
    ]
    # Sanity check the fixture really does contain a group members table,
    # so this test would actually catch a regression.
    assert all(needle in html for needle in fake_names_and_logins)

    detail = parsing.parse_detail_page(html)
    exercise = Exercise(
        exercise_name="pintos",
        kind="group",
        academic_year="2324",
        exercise_id="809",
        repository_id="99068",
        labts_url=(
            "https://teaching.doc.ic.ac.uk/labts/lab_exercises/2324/exercises/809/repository/99068"
        ),
        gitlab_url=detail.gitlab_url,
        clone_urls=detail.clone_urls,
        milestones=[
            Milestone(
                id="1122",
                name=detail.selected_milestone_name or "",
                year_page_label="2: PintOS Task 2 - User Programs",
                submission_status="Submitted",
                submission_state=detail.submission_state,
                submission_state_raw=detail.submission_state_raw,
                submitted_revision=detail.submitted_revision,
            )
        ],
    )

    serialised = json.dumps(exercise.to_dict())
    for needle in fake_names_and_logins:
        assert needle not in serialised


def test_parse_detail_page_result_has_no_group_members_field() -> None:
    detail = parsing.parse_detail_page(_fixture("detail_multi_milestone.html"))
    field_names = {f for f in vars(detail)}
    assert not any("member" in name.lower() for name in field_names)


# --- Exercise/Milestone: submitted_revision mirror + has_submission ---------


def _exercise_with_milestones(milestones: list[Milestone]) -> Exercise:
    return Exercise(
        exercise_name="some_exercise",
        kind="individual",
        academic_year="2324",
        exercise_id="1",
        repository_id="2",
        labts_url="https://teaching.doc.ic.ac.uk/labts/lab_exercises/2324/exercises/1/repository/2",
        gitlab_url=None,
        clone_urls={},
        milestones=milestones,
    )


def test_submitted_revision_mirror_set_for_a_single_milestone_exercise() -> None:
    exercise = _exercise_with_milestones(
        [
            Milestone(
                id="1",
                name="Only milestone",
                year_page_label="1",
                submission_status="Submitted",
                submission_state="submitted",
                submission_state_raw="Submitted revision: abc123",
                submitted_revision="abc123",
            )
        ]
    )
    assert exercise.submitted_revision == "abc123"


def test_submitted_revision_mirror_is_none_for_pintos_style_multi_milestone() -> None:
    exercise = _exercise_with_milestones(
        [
            Milestone("1121", "Task 1", "1", "Submitted", "submitted", None, "bc9dc865"),
            Milestone("1122", "Task 2", "2", "Submitted", "submitted", None, "efe2589e"),
            Milestone("1123", "Task 3", "3", "Submitted", "submitted", None, "bccab1dc"),
        ]
    )
    assert exercise.submitted_revision is None


def test_has_submission_true_when_any_milestone_has_a_revision() -> None:
    exercise = _exercise_with_milestones(
        [
            Milestone("1", "One", "1", "Nothing submitted yet", "unsubmitted", None, None),
            Milestone("2", "Two", "2", "Submitted", "submitted", None, "abc123"),
        ]
    )
    assert exercise.has_submission is True


def test_has_submission_false_when_no_milestone_has_a_revision() -> None:
    exercise = _exercise_with_milestones(
        [
            Milestone("1", "One", "1", "Nothing submitted yet", "unsubmitted", None, None),
        ]
    )
    assert exercise.has_submission is False


def test_has_submission_false_for_not_enabled_exercise() -> None:
    exercise = _exercise_with_milestones(
        [
            Milestone("1", "cfinaltest", "1", "", "not_enabled", None, None),
        ]
    )
    assert exercise.has_submission is False


def test_to_dict_matches_the_documented_schema_shape() -> None:
    exercise = _exercise_with_milestones(
        [
            Milestone("1", "Only", "1", "Submitted", "submitted", "Submitted revision: abc", "abc"),
        ]
    )
    data = exercise.to_dict()

    assert set(data) == {
        "exercise_name",
        "kind",
        "academic_year",
        "exercise_id",
        "repository_id",
        "labts_url",
        "gitlab_url",
        "clone_urls",
        "has_submission",
        "submitted_revision",
        "milestones",
    }
    assert data["has_submission"] is True
    assert data["submitted_revision"] == "abc"
    assert len(data["milestones"]) == 1
    assert set(data["milestones"][0]) == {
        "id",
        "name",
        "year_page_label",
        "submission_status",
        "submission_state",
        "submission_state_raw",
        "submitted_revision",
    }
