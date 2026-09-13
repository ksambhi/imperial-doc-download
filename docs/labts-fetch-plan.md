# Implementation plan: `labts_fetch`

Goal: log in to LabTS, walk every academic year, and produce a list of the GitLab
repositories behind every exercise — written to `<output_dir>/labts-list.json` and
printed to the terminal. Cloning those repos is a **separate, later** pipeline step;
this step only produces the list.

Everything below was verified against the live site (`teaching.doc.ic.ac.uk/labts`)
on 2026-09-13 with Kishan's account. Selectors, URL shapes and timings are measured,
not guessed.

---

## 1. Safety rules (non-negotiable)

LabTS is a shared, production teaching system and it is **slow**. It is also full of
destructive POST endpoints. Treat these as hard constraints:

1. **GET only.** The single exception is the one login POST to `/users/sign_in`.
   Never submit any other form. Detail pages contain:
   - `.../commit/<sha>/milestone/<id>/late_submit` — **submits coursework late**
   - `.../repository/<id>/request?commit_id=…` — queues a job on the shared test-VM fleet
   - `.../repository/<id>` (POST) — forces a repo refresh

   A stray POST here is not a bug, it is an academic-integrity incident. The client
   should expose only `get()`; do not add a generic `post()` helper.
2. **Sequential only.** One request in flight at a time. Do not add concurrency, a
   thread pool, or `asyncio.gather` over exercises.
3. **Delay between requests**, default `1.5s` (configurable). Measured run with this
   delay completed comfortably and did not appear to stress the server.
4. **Long timeouts.** Observed page times ranged 0.4s → **22.7s**. Use
   `httpx.Timeout(connect=30, read=120, write=30, pool=120)`.
5. **Retry with exponential backoff** (4 attempts, 2/4/8/16s). Connections do drop
   mid-transfer — this happened during development and is not hypothetical.
6. **Never log credentials or the session cookie.** We now log to `logs/*.log` on
   disk, so a careless `logger.debug(response.request)` would persist a password.
   Log URLs and status codes only.
7. **Do not probe IDs you were not given.** Exercise/repo/milestone IDs come from the
   home page only. Never increment or guess an ID — that reaches other students' data.
   (During investigation only Kishan's own resources were accessed.)

---

## 2. How LabTS works (verified)

### 2.1 Authentication — Devise, form-based

- `GET /labts` → 302 → `GET /labts/users/sign_in`
- The sign-in page contains `<form id="new_user">` with a hidden
  `authenticity_token` (Rails CSRF). The token is tied to the session cookie, so
  **scrape the token from that same response** and post it back.
- `POST /labts/users/sign_in` with form-encoded fields:

  | field | value |
  |---|---|
  | `utf8` | `✓` |
  | `authenticity_token` | from the form |
  | `user[uid]` | `$IMPERIAL_USERNAME` |
  | `user[password]` | `$IMPERIAL_PASSWORD` |
  | `user[remember_me]` | `0` |
  | `commit` | `Sign in` |

- Success → redirect to `/labts`, session held in the `_labts_session_cs` cookie.
  An `httpx.Client` with `follow_redirects=True` keeps it automatically.
- **Failure / expiry detection:** an unauthenticated request to any protected URL
  returns `200` after redirecting to `/labts/users/sign_in`. So do not check the
  status code — check whether `str(response.url)` ends in `/users/sign_in`, and
  raise a clear error ("LabTS session expired or credentials rejected").

### 2.2 Academic years

Every page carries the full year list in `<select id="switch_academic_year">`:

```
2627 2526 2425 2324 2223 2122 2021 1920 1819 1718 1617 1516 1415
```

Year page URL: `/labts/home/index/<year>`.

Read the list from the dropdown rather than hardcoding it — years get added each
session. Years where Kishan wasn't enrolled render the chrome and **no tables at
all** (a 4226-byte page). Detection: parse the page, find zero exercise rows → skip
the year. Do not detect by byte size.

For this account: **2223, 2324, 2425, 2526 have data; the other 9 years are blank.**

### 2.3 Year page → exercise rows

Two sections, each an `<h2>` followed by a `<table>`:
`Individual Assignments` and `Group Assignments` (identical column layout).

Each `tbody tr` has an anchor whose href encodes everything:

```
https://teaching.doc.ic.ac.uk/labts/lab_exercises/<year>/exercises/<exercise_id>/repository/<repo_id>?milestone=<milestone_id>
```

Columns: `Exercise Name | Milestone | Latest Test Result | Submission Status | Result of Submission`.

The row's `<tr class="...">` is `success` / `warning` / `danger` — cosmetic; use the
Submission Status cell text instead. Observed values:
`Submitted`, `Nothing submitted yet`, `Invalid submission`.

> **Markup quirk:** the Milestone `<td>` is never closed (`<td>1` then `<td>…`).
> `lxml` recovers from this correctly; the fixtures preserve it. Don't "fix" it.

### 2.4 ⚠️ Multi-milestone exercises — the key structural finding

**An exercise with N milestones appears as N separate rows on the year page**, all
sharing the same `exercise_id` *and* `repo_id`, differing only in `milestone_id`.

Verified on `pintos` (2324, ex 809, repo 99068, milestones 1121/1122/1123) and
`WACC` (2324, ex 849, repo 108045, milestones 1171/1172/1173).

Consequences:

- **Deduplicate by `(exercise_id, repo_id)`** when building the repo list, or the
  later clone step will clone pintos three times. In 2324 this collapses 24 rows → 20 repos.
- Comparing the three pintos milestone pages:
  - clone URLs — **identical** (one repo per exercise)
  - milestone dropdown — **identical**, lists all 3 (so one fetch gives the full list)
  - submitted revision — **different for every milestone**:
    `bc9dc865…`, `efe2589e…`, `bccab1dc…`

  So "the submitted revision" is a property of **(exercise, milestone)**, not of the
  exercise. See §3 for what this means for the schema.
- Therefore: **fetch one detail page per milestone row.** Fetching once per exercise
  would silently drop the other milestones' revisions. Cost is one extra request per
  extra milestone (4 extra requests total across all years for this account).

### 2.5 Detail page → the fields we want

`GET <the href from the year page>` (keep the `?milestone=` query — it selects which
revision is displayed).

**Milestone dropdown** — `<select id="switch_milestone">`, one `<option value="<id>">Name</option>`
per milestone, the current one carrying `selected`. This is the authoritative
milestone list. Note the dropdown gives a *proper* name ("Make machine learning deep")
where the year-page column only gives an ordinal ("1"); keep both.

**Submission banner** — a `<span class="label label-…">` above the commit table.
**Four states, all observed:**

| State | Markup | Meaning |
|---|---|---|
| `submitted` | `label-success`, text `Submitted revision:` + `<a>` whose text is the 40-char SHA | normal submitted case |
| `unsubmitted` | `label-warning`, `Currently Unsubmitted` | nothing submitted |
| `mismatch` | `label-danger`, `Warning: Submitted Commit Does Not Match This Repo` | e.g. `SED_Ex3` (2324) |
| `not_enabled` | **no banner element at all** | submission was never enabled — exam/test repos (`cfinaltest`, `javainterimtestparta`, …). 9 such repos on this account |

Parse defensively: absence of the banner is a valid state, not an error. Store the
banner's raw text too, so an unseen fifth variant survives into the JSON.

**Clone URLs** — a two-row table near the bottom:

```html
<td> Clone this repository via ssh: </td><td> git clone git@gitlab.doc.ic.ac.uk:lab2526_spring/<repo>.git</td>
<td> Clone this repository via https: </td><td> git clone https://gitlab.doc.ic.ac.uk/lab2526_spring/<repo>.git</td>
```

Validated regex (matches all 65 repos, zero misses):

```python
CLONE_RE = re.compile(
    r"Clone this repository via (\w+):\s*</td>\s*<td>\s*git clone ([^<]+?)\s*</td>", re.S
)
```

Strip the `git clone ` prefix — store the bare URL. Also capture the plain repo link
from the `View this repository on Gitlab` anchor; it's the browser URL and is handy.

> Repo naming: individual repos are `<exercise>_<username>`, group repos are
> `<exercise>_<groupnumber>`, under year/term groups like `lab2526_spring`.

**🔒 Group members table (privacy).** Group exercise pages contain a
`Group Members | Logins` table with teammates' **real names and college usernames**.
We do not need it to clone anything. **Do not extract it, and do not write it to the
JSON** — it is third-party personal data and this output is a file Kishan may share
or back up. (The committed fixtures have it replaced with fake names.)

### 2.6 Measured cost

| | |
|---|---|
| Years advertised / with data | 13 / 4 |
| Home page fetches | 13 |
| Detail fetches (one per milestone row) | 69 |
| Repos after dedup | **65** (all with both clone URLs) |
| Milestones total | 69 (65 single + pintos/WACC ×3) |
| Page sizes | 4 KB → **901 KB** (WACC, 493 commits) |
| Per-request time | 0.4s → 22.7s |
| Full cold run @ 1.5s delay | **≈ 4–6 minutes** |
| Resulting JSON | ~55 KB |

Because a cold run is minutes long, there is an **opt-in page cache**: `--cache-dir DIR`
stores each fetched page under a readable slug of its URL plus a short hash. A cache
hit costs no request, no delay and no login, so a fully cached re-run does zero
network I/O. Measured: **4m22s cold → 2.1s warm (83 hits, 0 fetches)**, byte-identical
output. The sign-in page is deliberately never cached — its CSRF token is tied to the
session cookie of the response it came from, so a cached one would fail the login.

---

## 3. Output schema

`<output_dir>/labts-list.json`, grouped by academic year as requested:

```json
{
  "2324": [
    {
      "exercise_name": "pintos",
      "kind": "group",
      "academic_year": "2324",
      "exercise_id": "809",
      "repository_id": "99068",
      "labts_url": "https://teaching.doc.ic.ac.uk/labts/lab_exercises/2324/exercises/809/repository/99068",
      "gitlab_url": "https://gitlab.doc.ic.ac.uk/lab2324_autumn/pintos_17",
      "clone_urls": {
        "ssh": "git@gitlab.doc.ic.ac.uk:lab2324_autumn/pintos_17.git",
        "https": "https://gitlab.doc.ic.ac.uk/lab2324_autumn/pintos_17.git"
      },
      "has_submission": true,
      "submitted_revision": null,
      "milestones": [
        {
          "id": "1121",
          "name": "PintOS Task 1 - Scheduling",
          "year_page_label": "1: PintOS Task 1 - Scheduling",
          "submission_status": "Submitted",
          "submission_state": "submitted",
          "submitted_revision": "bc9dc8653b3e001125f67cbe07f4e20c447cc6da"
        },
        { "id": "1122", "…": "…", "submitted_revision": "efe2589e4e1345235c2f15ad9e0fc3218510b032" },
        { "id": "1123", "…": "…", "submitted_revision": "bccab1dcfba696b66cf8b02c8224946c223ca206" }
      ]
    }
  ]
}
```

**Two extra fields, per Kishan's decisions:**

- **`submitted_revision` (top level)** is a *convenience mirror*: when the exercise has
  exactly one milestone it repeats that milestone's revision (which may itself be
  `null`); when the exercise has more than one milestone it is **always `null`**,
  because there is no single correct answer. The authoritative values always live in
  `milestones[].submitted_revision`. On this account it is non-null for 43 of 65
  exercises, and `null` for the 2 multi-milestone ones (pintos, WACC).

  ```python
  submitted_revision = milestones[0].submitted_revision if len(milestones) == 1 else None
  ```

- **`has_submission` (bool)** = `any(m.submitted_revision for m in milestones)`. Lets
  the later clone step trivially skip repos that were never submitted to. On this
  account: **45 true / 20 false**.

Every exercise is kept in the output regardless of `has_submission` — the 9
submission-never-enabled exam repos and the never-started ones still contain clonable
skeleton or partial work, so nothing is dropped at this stage.

The brief asked for a single `submitted_revision` per exercise. That alone can't
represent pintos/WACC, which have a distinct submitted SHA per milestone (§2.4),
hence the per-milestone values plus the mirror above.

Wrap the year map in a small envelope if convenient (`{"generated_at":…, "username":…, "years":{…}}`),
but the brief said "object grouped by academic year", so **keep `years` as the top level**
unless Kishan says otherwise.

---

## 4. Files to write

```
src/imperial_doc_download/labts_fetch/
├── __init__.py      # exports LabtsFetchStep (keep the existing public name)
├── client.py        # LabtsClient: login, GET-with-retry, session-expiry detection
├── parsing.py       # PURE functions, no I/O: parse_academic_years / parse_year_page / parse_detail_page
├── models.py        # dataclasses: Milestone, Exercise (+ .to_dict())
└── step.py          # LabtsFetchStep(Step): orchestration, JSON write, rich output
```

Keep `parsing.py` free of network code — that's what makes it testable against the
committed fixtures. `client.py` owns all I/O, delay and retry logic.

**Config** (`config.py`): add `password` (from `IMPERIAL_PASSWORD`) next to the
existing `username`. Validate both are present at the *start* of the step and fail
with a clear message rather than 4 minutes in. Optionally fall back to
`typer.prompt("LabTS password", hide_input=True)` when unset.

**Dependencies** to add to `pyproject.toml`: `httpx`, `beautifulsoup4`, `lxml`.
(`rich` is already a dependency.) `httpx` + `bs4`/`lxml` are sufficient — the site is
server-rendered with no JS-driven content, so **no Playwright/Selenium is needed**.
Parsing all 69 pages cost ~1.7s of CPU total, so `bs4` is not a bottleneck.

**Pipeline wiring** (`cli.py`): register `LabtsFetchStep()` in the `steps` list. Also
stash the parsed result in `ctx.state["labts_exercises"]` so the future clone step can
consume it in-process instead of re-reading the JSON.

**Honour `ctx.dry_run`**: the runner already skips steps entirely on dry-run, so no
extra work — just don't do network I/O in `__init__`.

---

## 5. Implementation order

1. `models.py` + `parsing.py` against the committed fixtures (no network). Get the
   tests green first — this is the bulk of the logic and needs no LabTS access.
2. `client.py`: login, `get()` with retry/backoff/delay, session-expiry detection.
3. `step.py`: iterate years → rows → dedupe by `(exercise_id, repo_id)` → fetch each
   milestone's detail page → assemble → write JSON → render table.
4. Wire into `cli.py`, add deps, update README.
5. Run it for real once, end to end, and eyeball the output.

---

## 6. Testing

Fixtures are committed at `tests/fixtures/labts/` — real markup, trimmed to ~2 commit
rows, with usernames/SHAs/CSRF tokens/group members anonymised:

| Fixture | Covers |
|---|---|
| `sign_in.html` | login form + `authenticity_token` extraction |
| `home_year_multi_milestone.html` | 24 rows, both tables, pintos ×3 + WACC ×3, `Invalid submission` |
| `home_year_blank.html` | year with no data (+ the 13-entry year dropdown) |
| `detail_submitted.html` | `label-success` + SHA, single milestone |
| `detail_multi_milestone.html` | 3-option dropdown, group repo |
| `detail_unsubmitted.html` | `label-warning` |
| `detail_mismatch.html` | `label-danger` |
| `detail_submission_not_enabled.html` | no banner at all |

Tests worth writing (all offline):

- year dropdown parses to the 13 expected years
- blank year → zero rows
- year page → 24 rows; dedupe → 20 exercises; pintos has exactly 3 milestones
- each of the 4 submission states maps to the right `submission_state`/`submitted_revision`
- clone URLs parse for ssh **and** https, with the `git clone ` prefix stripped
- `submitted_revision` mirror: set for a single-milestone exercise, `null` for pintos
- `has_submission`: true for `detail_submitted`, false for unsubmitted/mismatch/not-enabled
- group members never appear anywhere in the serialised output
- session-expiry detection: a response whose URL is `/users/sign_in` raises

Mock the network with `httpx.MockTransport` if you want a client-level test; do not
hit LabTS from the test suite (CI has no credentials and shouldn't hammer it anyway).

---

## 7. CLI output

After writing the JSON, print a `rich` table **per academic year** (skip blank years),
then a summary line. Suggested columns:

```
2324 — 20 repositories
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ Exercise           ┃ Kind       ┃ Milestone                   ┃ Submitted  ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ pintos             │ group      │ PintOS Task 1 - Scheduling  │ bc9dc865   │
│                    │            │ PintOS Task 2 - User Progr… │ efe2589e   │
│                    │            │ PintOS Task 3 - Virtual Mem │ bccab1dc   │
│ SED_Ex3            │ individual │ Reuse                       │ ⚠ mismatch │
│ c_pic_proc         │ individual │ 1                           │ — unsubmit │
└────────────────────┴────────────┴─────────────────────────────┴────────────┘
```

Show short (8-char) SHAs in the table; full SHAs go in the JSON. Finish with
`65 repositories across 4 academic years → <output_dir>/labts-list.json`.

Log progress per year/exercise at INFO through the existing logger, so the run is
followable in both the terminal and `logs/*.log` — a 5-minute silent step feels hung.

---

## 8. Decisions taken (previously open)

1. **Per-milestone revisions + convenience mirror** — settled, see §3. Milestones own
   the authoritative `submitted_revision`; the exercise carries a top-level mirror
   that is `null` whenever there is more than one milestone.
2. The brief's *"For submitted assignments, extract only the clone URL and list of
   milestones"* read inverted (unsubmitted exercises are the ones with no revision).
   Resolved as: **extract everything available for every exercise**, with
   `submitted_revision: null` where there isn't one.
3. **Unused repos are kept and flagged** via `has_submission` — settled, see §3.
4. **Year 2627** is advertised and currently blank. Harmless, just skipped.

## 9. Security note

Nothing exploitable was found, and nothing was probed beyond Kishan's own resources.
Two things are worth being *aware* of rather than reporting as bugs:
the destructive POST endpoints sitting unguarded-by-confirmation in the page (§1 —
normal for the app, dangerous for a scraper), and the group members table exposing
teammates' names and logins to any group member (§2.5 — presumably intentional).
