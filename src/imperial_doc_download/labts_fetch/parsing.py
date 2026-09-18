"""Pure HTML parsing for LabTS pages -- no network I/O whatsoever.

Every function here takes a page's HTML text and returns plain data. That's
what makes this module testable against the fixtures in
`tests/fixtures/labts/` without touching the network. All I/O (login,
fetching, retries, delay) lives in `client.py`; orchestration lives in
`step.py`.

Selectors, regexes and edge cases here were verified against the live site
-- see `docs/labts-fetch-plan.md` §2 for the reasoning. Don't "fix" the
markup quirks noted inline; they're intentional workarounds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

# Deliberately not `html.parser`: lxml recovers gracefully from the
# occasionally-unclosed `<td>` LabTS emits for the Milestone column
# (plan §2.3).
_PARSER = "lxml"

_ROW_HREF_RE = re.compile(
    r"/labts/lab_exercises/(?P<year>\d+)/exercises/(?P<exercise_id>\d+)"
    r"/repository/(?P<repo_id>\d+)"
)

# Validated against all 65 repos on the live site (plan §2.5) -- matches the
# two-row clone table regardless of the surrounding whitespace.
_CLONE_RE = re.compile(
    r"Clone this repository via (\w+):\s*</td>\s*<td>\s*git clone ([^<]+?)\s*</td>", re.S
)

_SECTION_KIND = {
    "individual assignments": "individual",
    "group assignments": "group",
}


def parse_csrf_token(html: str) -> str:
    """Extract the Rails `authenticity_token` from the sign-in form.

    The token is tied to the session cookie of the response it came from,
    so the caller must scrape it from the same response it's about to post
    back against (plan §2.1).
    """
    soup = BeautifulSoup(html, _PARSER)
    form = soup.find("form", id="new_user")
    if form is None:
        raise ValueError("LabTS sign-in form (#new_user) not found on page.")

    token_input = form.find("input", attrs={"name": "authenticity_token"})
    token = token_input.get("value") if token_input is not None else None
    if not token:
        raise ValueError("LabTS sign-in form has no authenticity_token value.")
    return token


def parse_academic_years(html: str) -> list[str]:
    """Read the full list of academic years from `#switch_academic_year`.

    Every LabTS page carries this dropdown. Years get added each session,
    so this is read from the page rather than hardcoded (plan §2.2).
    """
    soup = BeautifulSoup(html, _PARSER)
    select = soup.find("select", id="switch_academic_year")
    if select is None:
        return []
    return [option["value"] for option in select.find_all("option") if option.get("value")]


@dataclass(frozen=True)
class ExerciseRow:
    """One `<tr>` from a year page -- one milestone of one exercise.

    An exercise with N milestones produces N of these, sharing
    `exercise_id`/`repository_id` but differing in `milestone_id` (plan
    §2.4) -- callers must dedupe by `(exercise_id, repository_id)` and fetch
    one detail page per row, not per exercise.
    """

    exercise_name: str
    kind: str  # "individual" | "group"
    academic_year: str
    exercise_id: str
    repository_id: str
    milestone_id: str
    year_page_label: str
    submission_status: str
    labts_url: str  # the row's link, query string stripped
    detail_url: str  # the row's link as-is, including `?milestone=...`


def _parse_row_href(href: str) -> tuple[str, str, str, str]:
    match = _ROW_HREF_RE.search(href)
    if match is None:
        raise ValueError(f"Unrecognised LabTS exercise row link: {href!r}")

    milestone_ids = parse_qs(urlparse(href).query).get("milestone")
    if not milestone_ids:
        raise ValueError(f"LabTS exercise row link is missing a milestone id: {href!r}")

    return match["year"], match["exercise_id"], match["repo_id"], milestone_ids[0]


def _clean_text(text: str) -> str:
    """Collapse the internal whitespace LabTS pads its cells with."""
    return " ".join(text.split())


def parse_year_page(html: str) -> list[ExerciseRow]:
    """Parse a `/labts/home/index/<year>` page into its exercise rows.

    Both the "Individual Assignments" and "Group Assignments" tables are
    read (identical column layout, plan §2.3). A year where the account
    wasn't enrolled renders the chrome but no tables at all, so this
    naturally returns `[]` for a blank year -- detect blank years by
    calling this, never by page byte size.
    """
    soup = BeautifulSoup(html, _PARSER)
    rows: list[ExerciseRow] = []

    for heading in soup.find_all("h2"):
        kind = _SECTION_KIND.get(_clean_text(heading.get_text()).lower())
        if kind is None:
            continue

        table = heading.find_next("table")
        if table is None:
            continue

        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            link = tr.find("a", href=True)
            if link is None:
                continue

            cells = tr.find_all("td")
            if len(cells) < 4:
                continue

            href = link["href"]
            year, exercise_id, repo_id, milestone_id = _parse_row_href(href)

            rows.append(
                ExerciseRow(
                    exercise_name=link.get_text(strip=True),
                    kind=kind,
                    academic_year=year,
                    exercise_id=exercise_id,
                    repository_id=repo_id,
                    milestone_id=milestone_id,
                    year_page_label=_clean_text(cells[1].get_text(" ")),
                    submission_status=_clean_text(cells[3].get_text(" ")),
                    labts_url=href.split("?", 1)[0],
                    detail_url=href,
                )
            )

    return rows


@dataclass(frozen=True)
class DetailMilestoneOption:
    """One `<option>` of a detail page's `#switch_milestone` dropdown."""

    id: str
    name: str
    selected: bool


@dataclass(frozen=True)
class DetailPage:
    """Everything worth keeping from one exercise detail page.

    Deliberately excludes the Group Members table (real names + college
    logins of teammates) -- see plan §2.5. Nothing in this module ever
    reads that table.
    """

    milestones: list[DetailMilestoneOption]
    selected_milestone_id: str | None
    selected_milestone_name: str | None
    submission_state: str
    submission_state_raw: str | None
    submitted_revision: str | None
    gitlab_url: str | None
    clone_urls: dict[str, str] = field(default_factory=dict)


def _parse_milestone_dropdown(
    soup: BeautifulSoup,
) -> tuple[list[DetailMilestoneOption], str | None, str | None]:
    select = soup.find("select", id="switch_milestone")
    if select is None:
        return [], None, None

    options = [
        DetailMilestoneOption(
            id=option.get("value", ""),
            name=option.get_text(strip=True),
            selected=option.has_attr("selected"),
        )
        for option in select.find_all("option")
    ]

    # LabTS always marks the current milestone `selected`; fall back to the
    # first option (a plain <select>'s implicit default) if that's ever
    # missing so this never silently returns nothing for a real page.
    selected = next((o for o in options if o.selected), options[0] if options else None)
    if selected is None:
        return options, None, None
    return options, selected.id, selected.name


def _parse_submission_banner(soup: BeautifulSoup) -> tuple[str, str | None, str | None]:
    """Classify the submission banner into one of the four known states.

    The banner is a `<span class="label label-...">` that is a *direct*
    child of the `text-center` div above the commit table -- as opposed to
    the similarly-classed `<span class="label label-success">Submitted</span>`
    that appears *inside* the commit table's own rows. Absence of this
    element entirely is a valid, observed state ("not_enabled": submission
    was never turned on for this repo), not an error (plan §2.5).
    """
    banner = soup.select_one("div.text-center > span.label")
    if banner is None:
        return "not_enabled", None, None

    raw_text = _clean_text(banner.get_text(" "))
    classes = banner.get("class", [])

    if "label-success" in classes:
        link = banner.find("a")
        revision = link.get_text(strip=True) if link is not None else None
        return "submitted", raw_text, revision
    if "label-warning" in classes:
        return "unsubmitted", raw_text, None
    if "label-danger" in classes:
        return "mismatch", raw_text, None

    # An unseen fifth variant: the raw text still makes it into the JSON.
    return "unknown", raw_text, None


def _parse_gitlab_url(soup: BeautifulSoup) -> str | None:
    for anchor in soup.find_all("a", href=True):
        if "View this repository on Gitlab" in anchor.get_text():
            return anchor["href"]
    return None


def parse_detail_page(html: str) -> DetailPage:
    """Parse one exercise detail page (`.../repository/<id>?milestone=<id>`).

    Returns the milestone dropdown (the authoritative milestone list for
    this exercise), the submission state/revision for the milestone this
    particular page was loaded for, and the clone URLs/GitLab link (which
    are identical across all of an exercise's milestone pages).
    """
    soup = BeautifulSoup(html, _PARSER)

    milestones, selected_id, selected_name = _parse_milestone_dropdown(soup)
    submission_state, submission_state_raw, submitted_revision = _parse_submission_banner(soup)
    gitlab_url = _parse_gitlab_url(soup)
    clone_urls = {method: url.strip() for method, url in _CLONE_RE.findall(html)}

    return DetailPage(
        milestones=milestones,
        selected_milestone_id=selected_id,
        selected_milestone_name=selected_name,
        submission_state=submission_state,
        submission_state_raw=submission_state_raw,
        submitted_revision=submitted_revision,
        gitlab_url=gitlab_url,
        clone_urls=clone_urls,
    )
