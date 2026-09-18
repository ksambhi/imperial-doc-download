# Implementation plan: `emarking_fetch` marks record

Save the personal record — every marked exercise, with its deadline,
mark, grade and status colour, across every academic year — as one JSON
file at the root of the output directory.

Status: **plan only, nothing implemented.** Everything marked ✅ was
verified against the live API on 2026-09-18. Builds on
`emarking-fetch-plan.md`, whose §1 safety rules apply unchanged.

This plan also carries the **enrolment-based scope change** to the
existing download step (`emarking-fetch-plan.md` §3.3.1), because both
changes are driven by the same new endpoint and touch the same code.

## 1. Two scopes, deliberately different

The colour legend exposed a real gap, and the fix splits in two:

| | Rule | Rows | Why |
|---|---|---|---|
| **Download step** (existing, widening) | every exercise in a module we were **enrolled** in | 146 → **253** ✅ | tutorials and optional courseworks have specs worth keeping |
| **Marks record** (new) | every exercise with a **mark** | **138** ✅ | a record of marks; an unmarked tutorial has nothing to record |

The marks record is therefore a *subset* of what the download step
walks, not a different query. Both come from the same
`/me/{year}/exercises` response.

Decided alongside the widening: **model answers become opt-in and default
off** (`emarking-fetch-plan.md` §9.3.1). Every one is a 403, the wider
scope would take that from 48 to 117 refusals per cold run, and the
refusal is the system working as intended — archiving model answers would
leak future years' answers. Net effect on the cold run: **294 → 402 real
downloads**, rather than 342 → 519.

**Widening the download scope does not change the marks record** ✅ — a
mark only exists where we were involved, and every such module is in the
enrolment list, so the marked count is 138 under either rule:

| Year | marked anywhere | marked within enrolled modules |
|---|---|---|
| 2223 | 64 | 64 |
| 2324 | 44 | 44 |
| 2425 | 15 | 15 |
| 2526 | 15 | 15 |

## 2. The data: no new emarking requests, one new abc-api call ✅

`GET /me/{year}/exercises` — which `emarking-fetch` already makes per
year — **already contains every column of that page**. Confirmed by
listing every `/me/*` route in the live openapi (v2.0.0) ✅; there are
exactly four and none is a marks endpoint:

```
GET /me/{year}/exercises                                        ← we use this
GET /me/{year}/{module_code}/exercises/{exercise_number}
GET /me/{year}/{module_code}/exercises/{exercise_number}/distributions
GET /me/{year}/{module_code}/exercises/{exercise_number}/group
```

The endpoints that *sound* right — `/{year}/consolidated-marks`,
`/{year}/{module_code}/exercises/{n}/marks`, `/{year}/missing-marks`,
`/{year}/{student_username}/exercise-summaries` — are **all staff routes
over other people's data**, and per `emarking-fetch-plan.md` §1 they are
not to be called. We don't need them.

### 2.1 The one new call, and its three safety rules

```
GET https://abc-api.doc.ic.ac.uk/{year}/students?login={IMPERIAL_USERNAME}
```

Authenticated; returns a one-element list — our own record ✅. It solves
**both** problems at once: the `modules` array is the enrolment scope
*and* it carries `title`, so there is no second lookup for module names.

| | |
|---|---|
| `modules[]` | `code`, `title`, `ects`, `terms`, `applicable_cohorts`, `exam_contribution`, `coursework_contribution`, `level` ✅ |
| also in the record | `cid`, `email`, `firstname`, `lastname`, `personal_tutor`, `cohort`, `degree_year`, `studentstatus`, `modules_helped` |

1. **Never unfiltered.** `/{year}/students` with no `?login=` is every
   student in the department. The client method takes **no username
   argument** and always uses the configured one, so requesting someone
   else's enrolment is not an expressible call — the same
   safe-by-construction approach as GET-only.
2. **`modules_helped` is not scope.** It lists modules this user helped
   *teach* (`50007.1`, `50007.3`, `50002` in 2526 ✅) — a teaching role,
   not our coursework. Use `modules` only.
3. **Persist `code` and `title`, nothing else.** `personal_tutor` names a
   member of staff and the rest is our own identity data we have no use
   for. Read the modules, drop the record — never write it to disk
   verbatim.

### 2.2 `/{year}/public/modules` is no longer needed

An earlier draft used abc-api's public catalogue for titles. The
enrolment record supplies them for every module in scope by definition,
so that call is dropped — one endpoint instead of two. (For the record it
does work, unauthenticated, returning exactly `{code, title, ects}` with
100% coverage of our 36 module-years ✅. Worth remembering if the
enrolment route ever stops being available.)

A title must still be allowed to be missing: if an exercise somehow
carries a module code not in the enrolment list, the record keeps the
code and sets `title: null` rather than crashing.

## 3. The page, reproduced exactly ✅

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
submission ✅. So "marked", "submitted" and "has feedback" are three
independent facts and none may be inferred from another.

| Page column | Source |
|---|---|
| status dot colour | derived — §5 |
| module heading `60001: Advanced Computer Architecture` | `module_code` + the enrolment record's `modules[].title` |
| Exercise `1 \| CW: Decision Trees` | `number`, `type`, `title` — rendered `<number> \| <type>: <title>` |
| Deadline | `end` (never null ✅); `extended_end` is null throughout this account ✅ but carried anyway |
| Submitted ✓ | `submissions` non-empty |
| Mark `14 / 20` | `mark.mark` / `maximum_mark` — `mark` is a nested object, not a number |
| Grade `A` | **computed client-side** — §4 |
| Feedback `View` | `feedback` non-null |

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

- **Boundaries are inclusive at the bottom.** 14/20 is exactly 70.0% and
  the page shows `A`, not `B` ✅ — so `>=`, and no rounding before the
  comparison.
- **`maximum_mark` can be 0**, on exactly the 8 unmarked exercises ✅.
  Those are excluded from this record by the has-a-mark filter, so the
  divide is unreachable here — but `percentage_of()` must still return
  `None` rather than raise, because the same helper is the obvious thing
  to reuse and a `ZeroDivisionError` at 2am is a poor way to find out.
- **Boundaries below `B` are unverified.** The lowest grade on this
  account is `B` (A* 124, A 11, B 3 ✅), so `C`/`D`/`F` can't be checked
  against the live page. They follow the standard Imperial scheme and
  Kishan's "etc.", and the table is written into the output (§6) so it's
  visible and correctable rather than buried in source.

## 5. The status colour ✅

Four states in the legend, and they are a **derived classification, not
an API field** — like the grade. Verified against all seven screenshot
rows, 7/7 ✅:

| Colour | Legend wording | Rule | In this record? |
|---|---|---|---|
| 🟢 green | Individual Exercise | marked, `requires_group` false | ✅ 91 |
| 🟣 purple | Group Exercise | marked, `requires_group` true | ✅ 47 |
| 🟤 brown | Unassessed, with submission | not marked, has submissions | ✗ excluded by §1 |
| ⚫ grey | Unassessed, no submission | not marked, no submissions | ✗ excluded by §1 |

Precedence is **assessment first, then group-ness**. A group exercise
that was never marked is brown or grey, not purple — there is no fifth
"unassessed group" colour.

```python
def category(exercise):
    if exercise.mark is None:
        return "unassessed-with-submission" if exercise.submissions else "unassessed-no-submission"
    return "group" if exercise.requires_group else "individual"
```

The classifier stays complete even though the record's filter means only
`individual` and `group` can ever reach the file. Two reasons: the brown
and grey branches are what make the precedence rule testable, and if the
filter is ever relaxed the function is already right.

## 6. Output schema

One file, `<output_dir>/emarking-marks.json`, all years:

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
    { "category": "individual", "colour": "green",  "label": "Individual Exercise" },
    { "category": "group",      "colour": "purple", "label": "Group Exercise" }
  ],
  "summary": { "years": 4, "modules": 36, "exercises": 138 },
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
            "requires_group": false,
            "deadline": "2024-11-06T19:00:00+00:00",
            "extended_deadline": null,
            "submitted": true,
            "submitted_at": "2024-11-06T17:22:41.113221+00:00",
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
            "has_feedback": true
          }
        ]
      }
    ]
  }
}
```

Decisions baked in:

- **The colour is stored three ways**: `category` (stable slug to program
  against), `colour` (the legend's vocabulary), `category_label` (its
  exact wording). `requires_group` stays too, since `category` folds it
  away for the unassessed states.
- **Colour names, not hex.** The legend's swatches are a screenshot of a
  dark theme, so a hex here would be a guess presented as fact. §10.3.
- **`grade_boundaries` and `categories` are written into the file**, so
  the record explains its own vocabulary without anyone reading our
  source.
- **`years` is an object keyed by year; modules are an array**, so the
  page's ordering (module code ascending, then exercise number) survives.
- **`label` is precomputed** — that exact string is what the page shows
  and what a later HTML/CSV rendering wants. The parts remain separate.
- **`marker` is deliberately omitted.** `mark.marker`,
  `marks_published_by` and `locked_by` are staff usernames. The page
  doesn't show them and this record doesn't need them. They stay in the
  per-exercise `exercise.json` the fetch step writes, faithfully.
- **`cap` / `cap_reason` / `withheld` are carried** even though all are
  null across this account ✅ — they're real fields, and a capped mark is
  exactly what you'd want a record to have caught.
- **A module with no marked exercises is omitted entirely**, rather than
  appearing with an empty list.

### 6.1 Not doing: module-level averages ⚠️

`weight` is **not normalised** ✅ — per-module sums across this account:

```
100 → 28 modules     0 → 2      200 → 1     240 → 1
400 →  1             600 → 1    800 → 2
```

28 of 36 sum to 100 and the rest don't, so a weighted average would be
right most of the time and silently wrong otherwise — the worst property
for a record of your own marks. Raw `weight` is in the output; see §10.1.

## 7. Cost and caching

| | requests |
|---|---|
| enrolment, cold | **1 per year with data (4)** |
| enrolment, re-run | **0** — cached to `<output_dir>/<year>/emarking-enrolment.json` (modules only, §2.1) |
| exercise data | **0** — already on disk or in `ctx.state` |
| marks file itself | **0** — pure derivation, always rewritten |

The download step pays the widened scope: **342 → 519 downloads** on a
cold run (`emarking-fetch-plan.md` §3.3.1). Re-runs are unaffected —
everything already fetched stays cached.

Because the enrolment cache makes a re-run need no network at all, the
marks step **must not open the SOCKS proxy when every year is cached**.
Starting an ssh tunnel to do nothing is slow and rude. Check the cache
first; open the proxy only if something is missing.

## 8. Code changes

```
src/imperial_doc_download/emarking_fetch/
├── client.py       + enrolment(year) -> abc-api /{year}/students?login=<configured user>
│                     NO username parameter — see §2.1 rule 1
├── models.py       + Enrolment (code+title only, drops the personal record)
│                   ~ Exercise.is_ours -> replaced by module-scope filtering
├── step.py         ~ scope exercises by enrolled module, not involvement
│                   + stash ctx.state["emarking_exercises"] and ["emarking_enrolment"]
│                   + model answers only when asked; otherwise record
│                     `skipped`, with the reason, like commit submissions
├── grading.py      NEW pure: GRADE_BOUNDARIES, CATEGORIES, grade_for(), category_for()
├── marks.py        NEW pure: build the record from Exercises + a title lookup
└── marks_step.py   NEW EmarkingMarksStep: enrolment fetch/cache, write, report
tests/
├── test_emarking_grading.py   NEW boundaries, exact-70.0, max=0, all four categories
└── test_emarking_marks.py     NEW record assembly + the step end-to-end
```

Plus `cli.py` (the `--model-answers` flag, with §9.3.1's help text
verbatim) and the README's eMarking section, which currently says model
answers are attempted and recorded as forbidden.

Pipeline: `emarking-fetch` then `emarking-marks`, in the existing
`emarking` subcommand. Run standalone, the marks step reads
`<output_dir>/*/emarking-exercises.json` and the cached enrolment — the
same fall-back-to-disk pattern `gitlab-fetch` uses — so the record can be
rebuilt from an existing download with **zero requests**.

`grading.py` and `marks.py` stay free of I/O, same split as
`labts_fetch/parsing.py`.

### 8.1 One migration wrinkle ⚠️

`<output_dir>/<year>/emarking-exercises.json` currently holds the
*narrow* row set, and is reused unless `--force`. After the scope change
a plain re-run would keep reading the old 146-row cache and never notice
the 107 new exercises.

So the year cache needs to record which rule produced it — a `"scope":
"enrolled"` marker alongside the exercises — and refetch when the marker
is absent or stale. Silently honouring a stale cache is exactly the kind
of thing that looks like it worked.

## 9. Testing

Fixture is the screenshot: the seven 2425 rows, plus an unmarked
exercise, an unmarked one with no submission, an exercise in an enrolled
module we never touched, and a module code absent from the enrolment.

- 14/20 → `70.0` → `A` — the inclusive-boundary case, and the one example
  checkable against the rendered page
- 17/20 → `85.0` → `A*`; 100/100 → `100.0` → `A*`
- `maximum_mark: 0` → `percentage_of()` returns `None`, **no exception**
- a mark with no submission → `submitted: false`, mark present (60015)
- unmarked exercises are **absent from the marks record** but **present
  in the download scope**
- **all four categories**, including that an *unmarked group* exercise is
  brown/grey not purple — §5's precedence is the easy thing to reverse
- the seven screenshot rows map to their observed colours exactly
- a module absent from the enrolment → `title: null`, code preserved
- modules sorted by code, exercises by number
- `marker` / `marks_published_by` / `locked_by` / `cid` / `email` /
  `personal_tutor` appear **nowhere** in any serialised output
- `modules_helped` never contributes to scope
- `enrolment()` has no username parameter and cannot be aimed elsewhere
- **no model-answer request is issued by default**, and each is recorded
  as `skipped` with a reason; `--model-answers` restores the attempt and
  the 403-is-terminal handling
- zero requests and **no proxy** when the enrolment cache is warm
- a year cache without the `scope` marker is refetched (§8.1)

## 10. Open questions for Kishan

1. **Module totals** — §6.1 leaves them out because `weight` isn't
   normalised. Leave it, compute only for the 28 modules that sum to 100,
   or investigate the other sums first?
2. **Exact colours** — hex values would need sampling from the site's
   CSS rather than guessing from the screenshot. Only worth it if you
   plan to render the record.
3. **Exams and final module marks** aren't reachable; the endpoints that
   would have them are staff routes. The enrolment record does carry
   `exam_contribution` / `coursework_contribution` per module, which is
   the weighting, not the result — include it?
4. **`50007.1` / `50007.2` / `50007.3`** resolve to `Laboratory 2` and
   friends and appear as three modules. Leave as three?

## 11. Privacy note 🔒

`emarking-marks.json` is a complete record of your marks for four years
in one file. It lives in the gitignored output directory. Omitting
`marker` (§6) and everything but `code`/`title` from the enrolment record
(§2.1) keeps other people out of it entirely — the only personal data in
it is yours.
