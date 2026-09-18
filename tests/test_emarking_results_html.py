"""Offline tests for `emarking_fetch.results_html`.

The page is the last copy of this data that will still open once the
account is gone, so the things worth testing are the ones that would make
it quietly wrong: an unescaped title, a link that doesn't resolve, a dot
with no colour.
"""

from __future__ import annotations

import re

import pytest

from imperial_doc_download.emarking_fetch.results_html import (
    feedback_links_from_manifest,
    render_results,
)


def _exercise(**overrides) -> dict:
    base = {
        "number": 1,
        "title": "Decision Trees",
        "type": "CW",
        "label": "1 | CW: Decision Trees",
        "category": "individual",
        "colour": "green",
        "category_label": "Individual Exercise",
        "assessed": True,
        "requires_group": False,
        "deadline": "2024-11-06T19:00:00+00:00",
        "submitted": True,
        "mark": 14,
        "maximum_mark": 20,
        "percentage": 70.0,
        "grade": "A",
        "has_feedback": True,
    }
    return {**base, **overrides}


def _record(exercises=None, categories=None, years=None) -> dict:
    return {
        "generated_at": "2026-09-18T19:00:00+00:00",
        "username": "jbloggs",
        "grade_boundaries": [{"grade": "A*", "min_percent": 80}],
        "categories": categories
        if categories is not None
        else [{"category": "individual", "colour": "green", "label": "Individual Exercise"}],
        "summary": {"years": 1, "modules": 1, "exercises": 1},
        "years": {
            "2425": [
                {
                    "module_code": "60012",
                    "title": "Introduction to Machine Learning",
                    "exercises": exercises or [_exercise()],
                }
            ]
        }
        if years is None
        else years,
    }


class TestStructure:
    def test_renders_a_complete_document(self) -> None:
        page = render_results(_record())
        assert page.startswith("<!doctype html>")
        assert page.rstrip().endswith("</html>")
        assert "Introduction to Machine Learning" in page
        assert "1 | CW: Decision Trees" in page

    def test_is_self_contained(self) -> None:
        # It will only ever be opened from file://, so anything remote
        # would simply not load.
        page = render_results(_record())
        assert "<style>" in page and "<script>" in page
        assert "http://" not in page
        assert "https://" not in page

    def test_one_tab_per_year_newest_first(self) -> None:
        record = _record(
            years={
                "2223": [{"module_code": "40001", "title": "A", "exercises": [_exercise()]}],
                "2425": [{"module_code": "60012", "title": "B", "exercises": [_exercise()]}],
            }
        )
        page = render_results(record)
        tabs = re.findall(r'<button class="tab[^"]*" data-year="(\d+)"', page)
        assert tabs == ["2425", "2223"]
        # The newest year is the one showing on open.
        assert '<section class="panel" data-year="2425">' in page
        assert '<section class="panel" data-year="2223" hidden>' in page

    def test_year_labels_are_human(self) -> None:
        assert "2024–2025" in render_results(_record())

    def test_both_themes_are_defined_and_toggleable(self) -> None:
        page = render_results(_record())
        assert 'data-theme="dark"' in page
        assert ':root[data-theme="light"]' in page
        assert 'id="theme"' in page
        assert "localStorage" in page

    def test_every_dot_colour_has_a_rule(self) -> None:
        # A dot whose class has no background renders invisible, which is
        # the sort of thing nobody notices until it's the only copy left.
        page = render_results(_record())
        for colour in ("green", "purple", "brown", "grey"):
            assert f".dot.{colour} {{ background: var(--dot-{colour}); }}" in page
            assert f"--dot-{colour}:" in page

    def test_the_legend_lists_the_records_categories(self) -> None:
        page = render_results(_record())
        assert '<span class="dot green"></span>Individual Exercise' in page

    def test_an_empty_record_says_so_rather_than_rendering_nothing(self) -> None:
        page = render_results(_record(years={}))
        assert "No marked coursework found." in page


class TestEscaping:
    def test_a_title_with_markup_is_escaped(self) -> None:
        nasty = '<script>alert("x")</script>'
        page = render_results(_record([_exercise(label=nasty, title=nasty)]))
        assert nasty not in page
        assert "&lt;script&gt;" in page

    def test_an_ampersand_in_a_module_title_is_escaped(self) -> None:
        record = _record()
        record["years"]["2425"][0]["title"] = "Logic & Reasoning"
        page = render_results(record)
        assert "Logic &amp; Reasoning" in page


class TestFeedbackLinks:
    def test_links_to_the_file_on_disk_and_renames_it(self) -> None:
        links = {("2425", "60012", 1): "2425/60012/emarking/1-Decision Trees/feedback/9/kss22.pdf"}
        page = render_results(_record(), links)
        assert 'download="60012-1-feedback.pdf"' in page
        assert "kss22.pdf" in page

    def test_spaces_and_hashes_in_the_path_are_encoded(self) -> None:
        # Directory names come from lecturer-written titles. An unencoded
        # `#` truncates the URL at the fragment and the link dies.
        links = {("2425", "60012", 1): "2425/60012/emarking/1-Task #1 (draft)/feedback/9/a.pdf"}
        page = render_results(_record(), links)
        href = re.search(r'<a href="([^"]+)"', page).group(1)
        assert "%23" in href  # the #
        assert "%20" in href  # the spaces
        assert " " not in href
        # ...but it is still a path, not one escaped segment.
        assert href.startswith("2425/60012/emarking/")

    def test_no_link_when_the_exercise_has_no_feedback(self) -> None:
        page = render_results(_record([_exercise(has_feedback=False)]), {})
        assert "<a href=" not in page

    def test_a_dash_when_feedback_exists_but_was_not_downloaded(self) -> None:
        # Better than a link that 404s from file://.
        page = render_results(_record([_exercise(has_feedback=True)]), {})
        assert "Not downloaded" in page
        assert "<a href=" not in page


class TestRows:
    def test_an_unassessed_row_is_marked_for_the_filter(self) -> None:
        page = render_results(_record([_exercise(assessed=False, colour="brown")]))
        assert 'class="row unassessed"' in page
        assert "hide-unassessed" in page  # the CSS that acts on it

    def test_an_assessed_row_is_not(self) -> None:
        page = render_results(_record([_exercise(assessed=True)]))
        assert 'class="row"' in page

    def test_an_unsubmitted_row_has_no_tick(self) -> None:
        page = render_results(_record([_exercise(submitted=False)]))
        assert "&#10003;" not in page

    def test_mark_and_grade(self) -> None:
        page = render_results(_record())
        assert "14 / 20" in page
        assert '<td class="right grade">A</td>' in page

    def test_a_row_with_no_mark_renders_blank_rather_than_none(self) -> None:
        page = render_results(_record([_exercise(mark=None, grade=None, percentage=None)]))
        assert "None" not in page


class TestDeadlineFormatting:
    @pytest.mark.parametrize(
        ("iso", "expected"),
        [
            ("2024-11-06T19:00:00+00:00", "Nov 6th, 7:00pm"),
            ("2024-11-03T19:00:00+00:00", "Nov 3rd, 7:00pm"),
            ("2024-11-01T17:00:00+00:00", "Nov 1st, 5:00pm"),
            ("2024-11-22T17:30:00+00:00", "Nov 22nd, 5:30pm"),
            ("2024-11-10T12:00:00+00:00", "Nov 10th, 12:00noon"),
            ("2024-11-10T00:00:00+00:00", "Nov 10th, 12:00midnight"),
            # 11th/12th/13th are what a naive ordinal rule gets wrong.
            ("2024-11-11T09:00:00+00:00", "Nov 11th, 9:00am"),
            ("2024-11-12T09:00:00+00:00", "Nov 12th, 9:00am"),
            ("2024-11-13T09:00:00+00:00", "Nov 13th, 9:00am"),
        ],
    )
    def test_matches_the_pages_format(self, iso: str, expected: str) -> None:
        page = render_results(_record([_exercise(deadline=iso)]))
        assert expected in page

    def test_an_unparseable_deadline_is_passed_through(self) -> None:
        page = render_results(_record([_exercise(deadline="sometime")]))
        assert "sometime" in page

    def test_a_missing_deadline_is_blank(self) -> None:
        page = render_results(_record([_exercise(deadline=None)]))
        assert "None" not in page


class TestFeedbackLinksFromManifest:
    def test_picks_downloaded_and_cached_feedback_only(self) -> None:
        manifest = {
            "years": {
                "2425": [
                    {
                        "kind": "feedback",
                        "year": "2425",
                        "module_code": "60012",
                        "number": 1,
                        "status": "downloaded",
                        "path": "a/b.pdf",
                    },
                    {
                        "kind": "feedback",
                        "year": "2425",
                        "module_code": "60012",
                        "number": 2,
                        "status": "cached",
                        "path": "c/d.pdf",
                    },
                    {
                        "kind": "feedback",
                        "year": "2425",
                        "module_code": "60012",
                        "number": 3,
                        "status": "failed",
                        "path": None,
                    },
                    {
                        "kind": "feedback",
                        "year": "2425",
                        "module_code": "60012",
                        "number": 4,
                        "status": "forbidden",
                        "path": None,
                    },
                    {
                        "kind": "spec",
                        "year": "2425",
                        "module_code": "60012",
                        "number": 1,
                        "status": "downloaded",
                        "path": "spec.pdf",
                    },
                ]
            }
        }
        links = feedback_links_from_manifest(manifest)
        assert links == {
            ("2425", "60012", 1): "a/b.pdf",
            ("2425", "60012", 2): "c/d.pdf",
        }

    def test_windows_separators_become_url_separators(self) -> None:
        manifest = {
            "years": {
                "2425": [
                    {
                        "kind": "feedback",
                        "year": "2425",
                        "module_code": "60012",
                        "number": 1,
                        "status": "downloaded",
                        "path": "2425\\60012\\fb.pdf",
                    }
                ]
            }
        }
        assert feedback_links_from_manifest(manifest)[("2425", "60012", 1)] == "2425/60012/fb.pdf"

    def test_an_empty_manifest_is_no_links_not_an_error(self) -> None:
        assert feedback_links_from_manifest({}) == {}
