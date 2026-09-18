# Implementation plan: `emarking_fetch` marks record

Save the personal record — every exercise, deadline, mark, grade and
status colour across every academic year — as one JSON file at the root
of the output directory.

Status: **plan only, nothing implemented.** Everything marked ✅ was
verified against the live API on 2026-09-18. Builds on
`emarking-fetch-plan.md`, whose §1 safety rules apply unchanged.

**§10.1 needs a decision before implementing** — the colour legend
revealed that the page shows rows our current keep-rule drops.

## 1. The headline: this costs almost no requests ✅

`GET /me/{year}/exercises` — the one call `emarking-fetch` already makes
per year — **already contains every column of that page**. The marks
record is a reshaping of data we have, not a new harvest.

Confirmed by listing every `/me/*` route in the live openapi (v2.0.0) ✅.
There are exactly four, and none of them is a marks endpoint:

```
GET /me/{year}/exercises                                        ← we use this
GET /me/{year}/{module_code}/exercises/{exercise_number}
GET /me/{year}/{module_code}/exercises/{exercise_number}/distributions
GET /me/{year}/{module_code}/exercises/{exercise_number}/group
```

The endpoints that *sound* right — `/{year}/consolidated-marks`,
`/{year}/{module_code}/exercises/{n}/marks`, `/{year}/missing-marks`,
`/{year}/{student_username}/exercise-summaries`,
`/{year}/students/{username}/exercises` — are **all staff routes over
other people's data**, and per `emarking-fetch-plan.md` §1 they are not to
be called. Not for completeness, not to see if they work. We don't need
them: everything is in `/me/{year}/exercises` already.

**One thing is genuinely missing: the module title.** The exercise
carries `module_code: "60001"` but not `"Advanced Computer
Architecture"`. §2 is about where that comes from.

## 2. Safety: one new endpoint, and it's a public one

`emarking-fetch-plan.md` §1 applies in full — GET only, personal
endpoints only, sequential with a delay, never log credentials.

The one addition is abc-api's module catalogue. Three candidates were
checked ✅ and the choice matters:

| Endpoint | Auth | Returns | Verdict |
|---|---|---|---|
| `/{year}/modules` | **required** | code, title, ects, terms, cohorts, pass marks, **`staff` (lecturers' names and logins)** | ❌ more than we need, including third-party data |
| `/{year}/modules-metadata` | required | — | ❌ `403 You cannot perform this operation` — staff-only |
| **`/{year}/public/modules`** | **none** ✅ | exactly `{code, title, ects}` ✅ | ✅ **use this** |

`/{year}/public/modules` is the right answer on every axis: it is
explicitly public, needs no credentials at all, and returns the three
fields we want and nothing else — no staff, no cohorts, no other
students. It is a course catalogue, the same information a prospectus
carries.

## 3. Where each column comes from (verified)

### 3.1 The page, reproduced exactly ✅

Running the mapping below over the cached 2425 response reproduces the
screenshot row for row — marks, grades **and dot colours**:

```
●  60001 1 | CW: CPU Microarchitecture design…  Nov 6th 7:00pm   ✓  14/20   70.0%  A   View
●  60001 2 | CW: Reading Computer Architectu…   Nov 20th 7:00pm  ✓  17/20   85.0%  A*  View
●  60007 1 | CW: Practice coursework            Nov 1st 5:00pm   ✓  14/20   70.0%  A   View
●  60007 2 | CW: CW: Theory Coursework          Nov 22nd 5:00pm  ✓  17/20   85.0%  A*  View
●  60012 1 | CW: Decision Trees                 Nov 1st 7:00pm   ✓  98/100  98.0%  A*  View
●  60012 2 | CW: Neural Networks                Nov 22nd 7:00pm  ✓ 100/100 100.0%  A*  View
●  60015 1 | CW: Assessed Coursework            Feb 26th 2:00pm  ✗  87/100  87.0%  A*  —
   green = individual, purple = group; 7/7 match the screenshot ✅
```

That last row is the useful one: **a mark with no submission and no
feedback**. 14 exercises across the four years are marked without a
submission ✅, and 8 have a submission with no mark ✅. So "submitted",
"marked" and "has feedback" are three independent facts and none may be
inferred from another.

### 3.2 Column → field

| Page column | Source | Notes |
|---|---|---|
| status dot colour | derived — §5 | `requires_group` + whether it's marked/submitted |
| module heading `60001: Advanced Computer Architecture` | `module_code` + abc-api `/{year}/public/modules` | §2 |
| Exercise `1 \| CW: Decision Trees` | `number`, `type`, `title` | the page renders `<number> \| <type>: <title>` |
| Deadline | `end` (never null ✅) | `extended_end` exists but is null throughout this account ✅ — carry it anyway |
| Submitted ✓ | `submissions` non-empty | |
| Mark `14 / 20` | `mark.mark` / `maximum_mark` | `mark` is a nested object, not a number |
| Grade `A` | **computed client-side** — §4 | not in the API at all |
| Feedback `View` | `feedback` non-null | |
| "Show Unassessed" toggle | `mark is None` | 8 such exercises in our current set ✅ |
| "Show Future" toggle | `end` in the future | |

### 3.3 Module title coverage ✅ 100%

All **36 (year, module) pairs** on this account resolve to a title in
their own year's public catalogue — zero misses:

| Year | modules of ours | missing titles |
|---|---|---|
| 2223 | 8 | 0 |
| 2324 | 12 | 0 |
| 2425 | 7 | 0 |
| 2526 | 9 | 0 |

The catalogue is also served for years emarking itself 502s on (`1819`
returns 156 modules ✅), so it is not restricted to years we attended.
Codes with a suffix resolve fine too — `50007.1` → `Laboratory 2` ✅.

Still, a title must be allowed to be missing: this is a lookup against a
different service, and a module withdrawn from a later catalogue would
otherwise crash the run. Absent title → `null`, and the record still
carries the code.

## 4. The grade, which we compute ourselves

Not in the API — the page computes it in the browser, so we reproduce it:

| Grade | Percentage |
|---|---|
| `A*` | ≥ 80 |
| `A` | ≥ 70 |
| `B` | ≥ 60 |
| `C` | ≥ 50 |
| `D` | ≥ 40 |
| `F` | < 40 |

`percentage = 100 * mark.mark / maximum_mark`.

Three things to get right:

- **The boundaries are inclusive at the bottom.** 14/20 is exactly 70.0%
  and the page shows `A`, not `B` ✅ — so `>=`, and no floating-point
  rounding before the comparison.
- **`maximum_mark` can be 0.** Exactly 8 exercises ✅, and they are
  precisely the 8 unmarked ones. Dividing is a `ZeroDivisionError`
  waiting for a run at 2am, so: no mark, or `maximum_mark` falsy →
  `percentage: null`, `grade: null`, `assessed: false`.
- **Boundaries below `B` are unverified.** The lowest grade on this
  account is `B` (distribution: A* 124, A 11, B 3 ✅), so `C`/`D`/`F`
  can't be confirmed against the live page. They follow the standard
  Imperial scheme and Kishan's "etc.", but the boundary table lives in
  one constant and is written into the output (§6) so it's visible and
  correctable rather than buried.

## 5. The status colour ✅

The legend gives four states, and they are a **derived classification,
not an API field** — like the grade. Verified against all seven
screenshot rows, 7/7 ✅:

| Colour | Legend wording | Rule |
|---|---|---|
| 🟢 green | Individual Exercise | marked, and `requires_group` is false |
| 🟣 purple | Group Exercise | marked, and `requires_group` is true |
| 🟤 brown | Unassessed, with submission | not marked, `submissions` non-empty |
| ⚫ grey | Unassessed, no submission | not marked, no submissions |

In precedence order — **assessment first, then group-ness**. A group
exercise that was never marked is brown or grey, not purple; there is no
fifth "unassessed group" colour in the legend.

```python
def category(exercise):
    if exercise.mark is None:
        return "unassessed-with-submission" if exercise.submissions else "unassessed-no-submission"
    return "group" if exercise.requires_group else "individual"
```

Counts over the current row set ✅: individual 91, group 47,
unassessed-with-submission 8, **unassessed-no-submission 0**.

That zero is not a coincidence, and it is the interesting part — see
§10.1.

## 6. Output schema

One file, `<output_dir>/emarking-marks.json`, all years, as asked:

```json
{
  "generated_at": "2026-09-18T18:55:12+00:00",
  "username": "kss22",
  "grade_boundaries": [
    { "grade": "A*", "min_percent": 80 },
    { "grade": "A",  "min_percent": 70 },
    { "grade": "B",  "min_percent": 60 },
    { "grade": "C",  "min_percent": 50 },
    { "grade": "D",  "min_percent": 40 },
    { "grade": "F",  "min_percent": 0  }
  ],
  "categories": [
    { "category": "individual",                 "colour": "green",  "label": "Individual Exercise" },
    { "category": "group",                      "colour": "purple", "label": "Group Exercise" },
    { "category": "unassessed-no-submission",   "colour": "grey",   "label": "Unassessed, no submission" },
    { "category": "unassessed-with-submission", "colour": "brown",  "label": "Unassessed, with submission" }
  ],
  "summary": { "years": 4, "modules": 36, "exercises": 146, "assessed": 138 },
  "years": {
    "2425": [
      {
        "module_code": "60001",
        "title": "Advanced Computer Architecture",
        "ects": 5.0,
        "exercises": [
          {
            "number": 1,
            "title": "CPU Microarchitecture design space exploration",
            "type": "CW",
            "label": "1 | CW: CPU Microarchitecture design space exploration",
            "category": "individual",
            "colour": "green",
            "category_label": "Individual Exercise",
            "deadline": "2024-11-06T19:00:00+00:00",
            "extended_deadline": null,
            "submitted": true,
            "submitted_at": "2024-11-06T17:22:41.113221+00:00",
            "assessed": true,
            "mark": 14,
            "maximum_mark": 20,
            "percentage": 70.0,
            "grade": "A",
            "pass_mark": 40,
            "passed": true,
            "weight": 50,
            "marks_published": "2024-12-02T11:04:19.442+00:00",
            "cap": null,
            "cap_reason": null,
            "withheld": null,
            "has_feedback": true,
            "requires_group": false
          }
        ]
      }
    ]
  }
}
```

Decisions baked in:

- **The colour is stored three ways**: `category` (the stable slug to
  program against), `colour` (the legend's own vocabulary), and
  `category_label` (its exact wording). `requires_group` stays too, since
  `category` deliberately hides it for unassessed rows.
- **`categories` is written into the file**, the same way
  `grade_boundaries` is, so the record explains its own vocabulary
  without anyone reading our source.
- **Colour names, not hex.** The legend's swatches are the only sample
  we have and they're a screenshot of a dark theme, so a hex in here
  would be a guess presented as fact. See §10.4.
- **`years` is an object keyed by year; modules are an array.** The array
  preserves the page's ordering (module code ascending, then exercise
  number), which a JSON object wouldn't guarantee.
- **`label` is precomputed** (`1 | CW: Decision Trees`) because that
  exact string is what the page shows and what a later HTML/CSV
  rendering would want. The parts are all still there separately.
- **`marker` is deliberately omitted.** `mark.marker`,
  `marks_published_by` and `locked_by` are staff usernames. The page
  doesn't show them, this record doesn't need them, and it's one less
  reason for this file to be sensitive. They remain in the per-exercise
  `exercise.json` the fetch step writes, faithfully.
- **`cap` / `cap_reason` / `withheld` are carried even though all are
  null across this account** ✅ — they're real API fields, and a capped
  mark is exactly the thing you'd want the record to have recorded.
- **Unassessed exercises are kept**, with `assessed: false` and null
  mark/percentage/grade. The page has a toggle for them, so they're part
  of the record, not noise.

### 6.1 Not doing: module-level averages ⚠️

Tempting, and wrong to guess at. `weight` is **not normalised** ✅ —
per-module weight sums across this account are:

```
100 → 28 modules     0 → 2      200 → 1     240 → 1
400 →  1             600 → 1    800 → 2
```

28 of 36 modules sum to 100, and the other 8 don't. So a weighted module
average would be right most of the time and silently wrong the rest —
the worst possible property for a record of your own marks. The raw
`weight` is in the output; deriving a module total is left out until it's
understood. See §10.2.

## 7. Cost, caching and where it sits

**Requests: 4 per cold run, 0 on a re-run.** One
`/{year}/public/modules` per year that has data, cached to
`<output_dir>/<year>/emarking-modules.json`. The exercise data is already
on disk (`<output_dir>/<year>/emarking-exercises.json`) or in
`ctx.state` from the fetch step in the same pipeline.

A consequence worth building for: **when every year's catalogue is
already cached, the step needs no network at all — so it must not open
the SOCKS proxy.** Starting an ssh tunnel to do nothing is both slow and
rude. Check the cache first, open the proxy only if something is missing.

Structurally, a second step in the same pipeline:

```
imperial-doc-download emarking
  ├── emarking-fetch   (existing)  → files + ctx.state["emarking_exercises"]
  └── emarking-marks   (new)       → emarking-marks.json
```

`emarking-fetch` needs a one-line change: it already parses the
exercises, so it should stash them in `ctx.state["emarking_exercises"]`
the way `labts-fetch` stashes its list. Run standalone, `emarking-marks`
reads `<output_dir>/*/emarking-exercises.json` instead — the same
fall-back-to-disk pattern `gitlab-fetch` uses, and it means the marks
record can be rebuilt with **zero requests** from an existing download.

`--force` re-reads the catalogues; the marks file is always rewritten
(it's cheap and derived).

## 8. Files to write

```
src/imperial_doc_download/emarking_fetch/
├── grading.py        pure: GRADE_BOUNDARIES, CATEGORIES, grade_for(), category_for()
├── marks.py          pure: build the record from Exercises + a title lookup
├── marks_step.py     EmarkingMarksStep: catalogue fetch/cache, write, report
├── client.py         + `modules(year)` — abc-api /{year}/public/modules, no auth
└── step.py           + stash ctx.state["emarking_exercises"]
tests/
├── test_emarking_grading.py   boundaries, the exact-70.0 case, max=0, all 4 categories
└── test_emarking_marks.py     record assembly + the step end-to-end
```

`grading.py` and `marks.py` stay free of I/O — that's what makes the
interesting logic testable offline, same split as `labts_fetch/parsing.py`.

## 9. Testing

The fixture is the screenshot: the seven 2425 rows above, plus an
unassessed exercise (`maximum_mark: 0`), an unassessed one with no
submission, and a module absent from the catalogue. Worth asserting:

- 14/20 → `70.0` → `A` (the inclusive-boundary case, and the one real
  example we can check against the rendered page)
- 17/20 → `85.0` → `A*`; 100/100 → `100.0` → `A*`
- `maximum_mark: 0` → percentage and grade both `null`, `assessed: false`,
  and **no exception**
- a mark with no submission → `submitted: false`, mark still present
  (the 60015 row)
- **all four colours**, and specifically that an *unmarked group*
  exercise is brown/grey rather than purple — the precedence rule in §5
  is the easy thing to get backwards
- the seven screenshot rows map to their observed colours exactly
- a module missing from the catalogue → `title: null`, code preserved,
  run completes
- modules sorted by code, exercises by number
- `marker` / `marks_published_by` / `locked_by` appear **nowhere** in the
  serialised output
- the step makes zero requests when the catalogues are cached, and never
  opens a proxy in that case
- every grade boundary maps, including the C/D/F ones we have no live
  data for

## 10. Open questions for Kishan

### 10.1 ⚠️ Grey proves we're dropping rows the page shows — decide first

There are **zero** grey exercises in our output, and there structurally
always will be. `emarking-fetch-plan.md` §3.3's keep-rule is *"keep an
exercise if it has a submission, feedback, or a mark"* — which is exactly
the negation of grey. Yet the legend has a grey swatch, so the page
plainly shows them.

The page must therefore select rows by **module**, not by per-exercise
involvement: your modules, then every exercise in them. Measured ✅:

| Year | we keep | all exercises in those same modules | rows we'd be missing |
|---|---|---|---|
| 2324 | 44 | 66 | **22** |
| 2425 | 16 | 17 | **1** |
| 2526 | 20 | 41 | **21** |

and the missing ones look like real record content — `50007.1` tutorials
(`C Picture Processing`, `Linking and Loading`), group-formation entries,
and **optional courseworks that were available and not done**
(`Scala Recursive-Descent Parser (Optional)`).

Three options:

- **(a) Match the page — recommended.** Row set = every exercise in any
  module we have involvement in. ~190 rows instead of 146, grey becomes
  reachable, and the record shows what was on offer as well as what was
  done. Costs nothing: same API response, wider local filter.
- **(b) Keep the current rule.** 146 rows, all of them things you
  actually did. Grey then never appears and should be dropped from
  `categories` rather than advertised and never used.
- **(c) Include them, flagged.** Option (a) plus an `involved: false`
  marker so both views are one filter away.

My recommendation is **(a)**, because you asked for the page and grey is
on the page. Worth noting it does pull in a little noise — 2526's
`70010/10 "test2627"` looks like a staff test exercise.

**This only affects the marks record.** The download step's narrower rule
should stay exactly as it is — a record row is free, a spec download is a
request against a shared server.

### 10.2 Module totals

§6.1 leaves them out because `weight` isn't normalised. Options: leave it
(safe), compute a weighted average only for the 28 modules whose weights
sum to 100 and null elsewhere, or dig into what the other sums mean
first.

### 10.3 Exams and final module marks

This record covers what eMarking holds, which is coursework. Final module
marks and exam results aren't reachable — the endpoints that would have
them are staff routes.

### 10.4 Exact colours

Do you want the real hex values in the JSON? They'd need sampling from
the site's CSS rather than guessing from the screenshot. Colour *names*
are in there either way, so this is only worth doing if you're planning
to render the record.

### 10.5 `50007.1` / `50007.2` / `50007.3`

They resolve to `Laboratory 2` and friends and appear as three separate
modules. Fine to leave as three rows?

## 11. Privacy note 🔒

`emarking-marks.json` is a complete record of your marks for four years
in one file. It's inside the gitignored output directory and stays there.
Omitting `marker` (§6) keeps other people out of it, so the only personal
data in it is yours.
