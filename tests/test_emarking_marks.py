"""Offline tests for the marks record — `marks.py` and `marks_step.py`.

The fixture is the personal-record page from the screenshot: the seven
2425 rows, with their real marks, deadlines and dot colours, plus the
awkward cases around them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imperial_doc_download.config import Settings
from imperial_doc_download.emarking_fetch.marks import build_record, label_for
from imperial_doc_download.emarking_fetch.marks_step import (
    MARKS_FILENAME,
    EmarkingMarksStep,
)
from imperial_doc_download.emarking_fetch.models import Exercise, Module
from imperial_doc_download.pipeline import PipelineContext


def _exercise(
    *,
    module_code: str = "60001",
    number: int = 1,
    title: str = "CPU Microarchitecture design space exploration",
    type_: str | None = "CW",
    mark: int | None = 14,
    maximum: int | None = 20,
    group: bool = False,
    submitted: bool = True,
    feedback: bool = True,
    **overrides: object,
) -> Exercise:
    payload = {
        "year": "2425",
        "module_code": module_code,
        "number": number,
        "title": title,
        "type": type_,
        "requires_group": group,
        "spec": "2024-10-01T00:00:00+00:00",
        "supplementary_file": None,
        "model_answer": None,
        "maximum_mark": maximum,
        "pass_mark": 40,
        "weight": 50,
        "end": "2024-11-06T19:00:00+00:00",
        "extended_end": None,
        "marks_published": "2024-12-02T11:04:19.442+00:00",
        "mark": None
        if mark is None
        else {
            "mark": mark,
            "marker": "a-marker",
            "cap": None,
            "cap_reason": None,
            "withheld": None,
            "student_username": "jbloggs",
        },
        "submissions": [
            {
                "id": 5901,
                "exercise_id": 1,
                "username": "jbloggs",
                "timestamp": "2024-11-06T17:22:41.113221+00:00",
                "file_path": "x/y.pdf",
                "file_size": 1,
                "gitlab_hash": None,
                "target_submission_file_name": "cw1.pdf",
            }
        ]
        if submitted
        else [],
        "feedback": {
            "id": 88127,
            "distribution_id": 2611,
            "timestamp": "2024-12-01T00:00:00+00:00",
            "marker": "a-marker",
        }
        if feedback
        else None,
        **overrides,
    }
    return Exercise.from_api(payload)


#: The seven rows of the screenshot, with their observed dot colours.
SCREENSHOT = [
    _exercise(module_code="60001", number=1, mark=14, group=False),
    _exercise(
        module_code="60001",
        number=2,
        title="Reading Computer Architecture Research Literature",
        mark=17,
        group=True,
    ),
    _exercise(module_code="60007", number=1, title="Practice coursework", mark=14, group=True),
    _exercise(module_code="60007", number=2, title="CW: Theory Coursework", mark=17, group=True),
    _exercise(
        module_code="60012", number=1, title="Decision Trees", mark=98, maximum=100, group=True
    ),
    _exercise(
        module_code="60012", number=2, title="Neural Networks", mark=100, maximum=100, group=True
    ),
    _exercise(
        module_code="60015",
        number=1,
        title="Assessed Coursework",
        mark=87,
        maximum=100,
        group=False,
        submitted=False,
        feedback=False,
    ),
]

MODULES = [
    Module("60001", "Advanced Computer Architecture"),
    Module("60007", "The Theory and Practice of Concurrent Programming"),
    Module("60012", "Introduction to Machine Learning"),
    Module("60015", "Network and Web Security"),
]


def _record(exercises=None, modules=None):
    return build_record(
        {"2425": exercises if exercises is not None else SCREENSHOT},
        {"2425": modules if modules is not None else MODULES},
        username="jbloggs",
        generated_at="2026-09-18T19:00:00+00:00",
    )


def _rows(record) -> list[dict]:
    return [e for year in record["years"].values() for m in year for e in m["exercises"]]


class TestTheScreenshot:
    def test_every_row_reproduces(self) -> None:
        rows = _rows(_record())
        actual = [(r["label"], r["mark"], r["percentage"], r["grade"], r["colour"]) for r in rows]
        assert actual == [
            ("1 | CW: CPU Microarchitecture design space exploration", 14, 70.0, "A", "green"),
            ("2 | CW: Reading Computer Architecture Research Literature", 17, 85.0, "A*", "purple"),
            ("1 | CW: Practice coursework", 14, 70.0, "A", "purple"),
            ("2 | CW: CW: Theory Coursework", 17, 85.0, "A*", "purple"),
            ("1 | CW: Decision Trees", 98, 98.0, "A*", "purple"),
            ("2 | CW: Neural Networks", 100, 100.0, "A*", "purple"),
            ("1 | CW: Assessed Coursework", 87, 87.0, "A*", "green"),
        ]

    def test_module_titles_come_through(self) -> None:
        record = _record()
        assert [m["title"] for m in record["years"]["2425"]] == [
            "Advanced Computer Architecture",
            "The Theory and Practice of Concurrent Programming",
            "Introduction to Machine Learning",
            "Network and Web Security",
        ]

    def test_the_marked_but_unsubmitted_row(self) -> None:
        # 60015: a mark, no submission, no feedback. Three independent
        # facts, and none may be inferred from another.
        row = [r for r in _rows(_record()) if r["label"].endswith("Assessed Coursework")][0]
        assert row["submitted"] is False
        assert row["submitted_at"] is None
        assert row["has_feedback"] is False
        assert row["mark"] == 87

    def test_summary_counts(self) -> None:
        assert _record()["summary"] == {"years": 1, "modules": 4, "exercises": 7}


class TestFiltering:
    def test_unmarked_exercises_are_excluded(self) -> None:
        exercises = [*SCREENSHOT, _exercise(module_code="60001", number=9, mark=None, maximum=0)]
        assert len(_rows(_record(exercises))) == len(SCREENSHOT)

    def test_a_module_with_nothing_marked_is_omitted_entirely(self) -> None:
        exercises = [_exercise(module_code="60099", number=1, mark=None, maximum=0)]
        record = _record(exercises, [Module("60099", "Nothing marked here")])
        assert record["years"] == {}
        assert record["summary"]["modules"] == 0

    def test_an_unmarked_group_exercise_is_simply_absent(self) -> None:
        # Not "purple" — not anywhere. The precedence rule is tested in
        # test_emarking_grading.py; here it just must not leak in.
        exercises = [*SCREENSHOT, _exercise(module_code="60001", number=9, mark=None, group=True)]
        assert len(_rows(_record(exercises))) == len(SCREENSHOT)


class TestOrderingAndShape:
    def test_modules_sorted_by_code_and_exercises_by_number(self) -> None:
        shuffled = list(reversed(SCREENSHOT))
        record = _record(shuffled)
        assert [m["module_code"] for m in record["years"]["2425"]] == [
            "60001",
            "60007",
            "60012",
            "60015",
        ]
        first = record["years"]["2425"][0]["exercises"]
        assert [e["number"] for e in first] == [1, 2]

    def test_a_module_missing_from_the_enrolment_keeps_its_code(self) -> None:
        record = _record(SCREENSHOT, [Module("60001", "Advanced Computer Architecture")])
        by_code = {m["module_code"]: m for m in record["years"]["2425"]}
        assert by_code["60007"]["title"] is None
        assert by_code["60001"]["title"] == "Advanced Computer Architecture"

    def test_the_legend_and_boundaries_are_written_into_the_file(self) -> None:
        record = _record()
        assert record["grade_boundaries"][0] == {"grade": "A*", "min_percent": 80}
        # Only the categories this record actually uses are advertised.
        assert [c["category"] for c in record["categories"]] == ["individual", "group"]

    def test_a_marked_but_zero_weighted_exercise_is_brown_and_kept(self) -> None:
        """The 40008 progress tests: marked 10/10, A*, and still brown.

        They belong in the record -- they have marks -- but they are
        unassessed, because they contribute nothing to the module.
        """
        exercises = [
            *SCREENSHOT,
            _exercise(
                module_code="60001", number=9, title="PMT: Graphs", mark=10, maximum=10, weight=0
            ),
        ]
        row = [r for r in _rows(_record(exercises)) if r["number"] == 9][0]
        assert row["colour"] == "brown"
        assert row["assessed"] is False
        assert (row["mark"], row["grade"]) == (10, "A*")
        # ...and the legend grows to mention it.
        record = _record(exercises)
        assert "unassessed-with-submission" in [c["category"] for c in record["categories"]]

    def test_pass_mark_is_a_percentage_not_a_raw_mark(self) -> None:
        """The trap: `pass_mark` looks like a mark and isn't.

        80 of this account's 138 marked exercises have a `pass_mark`
        larger than their own `maximum_mark`, so comparing it against the
        raw mark would report failures that never happened.
        """
        row = _rows(_record())[0]
        assert row["pass_mark"] == 40
        assert row["pass_mark_is_percent"] is True
        # 14/20 is 70%, comfortably past a 40% pass mark -- even though
        # the raw mark, 14, is below 40.
        assert row["mark"] < row["pass_mark"]
        assert row["passed"] is True

    def test_pass_mark_above_the_maximum_mark_still_works(self) -> None:
        # 9/10 with a pass mark of 40: nonsense as a raw comparison,
        # 90% >= 40% as a percentage.
        exercises = [_exercise(mark=9, maximum=10, pass_mark=40)]
        row = _rows(_record(exercises))[0]
        assert row["percentage"] == 90.0
        assert row["passed"] is True

    def test_an_actual_failure_is_reported_as_one(self) -> None:
        exercises = [_exercise(mark=3, maximum=10, pass_mark=40)]
        row = _rows(_record(exercises))[0]
        assert row["percentage"] == 30.0
        assert row["passed"] is False
        assert row["grade"] == "F"

    def test_cap_fields(self) -> None:
        row = _rows(_record())[0]
        assert row["cap"] is None and row["withheld"] is None

    def test_label_without_a_type(self) -> None:
        assert label_for(_exercise(type_=None, number=3, title="Thing")) == "3 | Thing"


class TestPrivacy:
    def test_no_staff_usernames_anywhere_in_the_record(self) -> None:
        blob = json.dumps(_record())
        for leak in ("a-marker", "marker", "marks_published_by", "locked_by"):
            assert leak not in blob

    def test_the_students_own_username_appears_once_at_the_top(self) -> None:
        record = _record()
        assert record["username"] == "jbloggs"
        assert json.dumps(record["years"]).count("jbloggs") == 0


class TestStep:
    """`EmarkingMarksStep` end to end, entirely from disk."""

    def _context(self, tmp_path: Path, **state: object) -> PipelineContext:
        ctx = PipelineContext(
            output_dir=tmp_path,
            settings=Settings(username="jbloggs", password="pw", doc_ssh_key="/dev/null"),
        )
        ctx.state.update(state)
        return ctx

    def test_writes_the_record_from_pipeline_state(self, tmp_path: Path) -> None:
        ctx = self._context(
            tmp_path,
            emarking_exercises={"2425": SCREENSHOT},
            emarking_modules={"2425": MODULES},
        )
        EmarkingMarksStep().run(ctx)

        record = json.loads((tmp_path / MARKS_FILENAME).read_text())
        assert record["summary"]["exercises"] == 7
        assert ctx.state["emarking_marks"]["summary"]["exercises"] == 7

    def test_rebuilds_from_disk_with_no_requests(self, tmp_path: Path) -> None:
        # What `emarking-fetch` leaves behind.
        year = tmp_path / "2425"
        year.mkdir()
        (year / "emarking-exercises.json").write_text(
            json.dumps({"scope": "enrolled-modules", "exercises": [e.raw for e in SCREENSHOT]})
        )
        (year / "emarking-enrolment.json").write_text(
            json.dumps([{"code": m.code, "title": m.title} for m in MODULES])
        )

        # No proxy is stubbed: if the step tried to reach the network it
        # would fail, which is the assertion.
        EmarkingMarksStep().run(self._context(tmp_path))

        record = json.loads((tmp_path / MARKS_FILENAME).read_text())
        assert record["summary"]["exercises"] == 7
        assert record["years"]["2425"][0]["title"] == "Advanced Computer Architecture"

    def test_a_stale_scope_cache_is_not_used(self, tmp_path: Path) -> None:
        year = tmp_path / "2425"
        year.mkdir()
        # The pre-scope-change format: a bare list.
        (year / "emarking-exercises.json").write_text(json.dumps([e.raw for e in SCREENSHOT]))
        with pytest.raises(RuntimeError, match="no exercise data"):
            EmarkingMarksStep().run(self._context(tmp_path))

    def test_complains_clearly_when_there_is_nothing_to_work_from(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="Run the emarking step first"):
            EmarkingMarksStep().run(self._context(tmp_path))
