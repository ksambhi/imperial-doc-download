# Implementation plan: `emarking_fetch`

Download every coursework submission, spec, model answer, supplementary
file and feedback file from DoC's eMarking system.

Status: **implemented**, in `src/imperial_doc_download/emarking_fetch/`.
Everything marked ✅ was verified against the live API — including all of
§9, which is now answered. Four of those answers changed the design;
where they did, the section above says so and §9 has the evidence.

Two things the implementation added that aren't below:

- **Empty years are remembered** in the manifest and not re-probed,
  because §9.5 turned "an unused year comes back empty" into "an unused
  year is a 500", and 21 deliberate server errors per run is exactly what
  §1 says not to do. `--force`, or naming a year with `--year`, checks
  again — which matters for the current academic year.
- **The `ssh.py` in §7 didn't need writing.** The per-run temp dir, key
  unlocking and private-agent flow moved out of `gitlab_fetch` into a
  shared `imperial_doc_download.ssh.SshSession`, and `proxy.py` supplies
  only the `Host` block and the `-D` tunnel on top of it.

## 1. Safety rules (non-negotiable)

This API is far more dangerous than LabTS. It is not a read-only reports
system: it is the system coursework is *submitted* to and marks are
*recorded* in. Among its 55 paths are

```
POST   /{year}/{module_code}/exercises/{exercise_number}/submissions   (submit coursework)
PUT    /distributions/{distribution_id}/feedback/{feedback_id}         (change feedback)
POST   /{year}/consolidated-marks                                      (write marks)
PATCH  /{year}/{module_code}/exercises/{exercise_number}/spec          (replace a spec)
DELETE ...
```

So, exactly as in `labts_fetch` (plan §1) and for stronger reasons:

- **GET only, by construction.** The client exposes one request method.
  There is no `post()`/`put()`/`patch()`/`delete()` helper anywhere, and
  there must never be one. A bug that submits coursework on the user's
  behalf, or edits a mark, is not recoverable by apologising.
- **Personal endpoints only.** Use `/me/...` and the user's own
  submissions. The API also exposes `/{year}/students/{username}/exercises`,
  `/submissions/staff`, `/marks`, `/missing-marks`,
  `/{year}/{module_code}/flat-zero-marks` and similar. Those are other
  people's data and other people's business. They are not to be called —
  not to "see if they work", not for completeness. If one is reachable
  when it shouldn't be, that is a bug to report to DoC, not to use
  (see the repo's standing instruction on this).
- **Sequential, delayed, retried.** One request at a time by default,
  a delay between requests, long timeouts, exponential backoff. The API
  advertises no rate limits ✅ and no pagination ✅, which means nothing
  stops us hammering it — restraint has to come from our side.
- **Never log the password, the `Authorization` header, or the response
  body of an auth failure.** URLs and status codes only.

## 2. Getting there: the SOCKS proxy

`emarking-api.doc.ic.ac.uk` and `abc-api.doc.ic.ac.uk` are both inside
the DoC firewall. Access is via an SSH SOCKS proxy through a shell
server:

```
ssh -N -D 127.0.0.1:<port> -i $IMPERIAL_DOC_SSH_KEY $IMPERIAL_USERNAME@shell{1..5}.doc.ic.ac.uk
```

Design (mirrors `gitlab_fetch/ssh.py`, which already solves most of this):

- One of `shell1-5` picked at random per run, `--jump-host` to override —
  same rationale as the clone step: don't always load the same box.
- **A free ephemeral port chosen at runtime**, not a hardcoded 1080.
  The user may already have a proxy on the usual port, and two runs
  must not collide.
- Key handling is reused wholesale from `gitlab_fetch.ssh`: throwaway
  config in a temp dir (never `~/.ssh/config`), `accept-new` host keys,
  `BatchMode=yes`, and the private-agent passphrase flow.
- The proxy is a context manager: it starts, waits for the port to
  actually accept a connection (an ssh that fails *after* forking would
  otherwise show up as a confusing connection error on the first
  request), yields, then is torn down — including on exceptions.
- httpx reaches it with `proxy="socks5://127.0.0.1:<port>"`, which needs
  the `socksio` package: **new dependency, `httpx[socks]`.**

## 3. How the API works (verified against openapi.json, v2.0.0)

### 3.1 Two hosts, two auth models

| Host | Auth | Used for |
|---|---|---|
| `abc-api.doc.ic.ac.uk` | **none** ✅ | `/years` — the list of academic years |
| `emarking-api.doc.ic.ac.uk` | HTTP Basic ✅ | everything else |

Every emarking request also carries `x-proxied-user: $IMPERIAL_USERNAME`
(declared as an optional header parameter on every operation ✅).

`/years` returns a flat list of 25 year strings, `"0203"` … ✅. It is
every year the *system* knows about, not the user's — years with no
exercises of ours produce no output at all.

### 3.2 The one endpoint that carries almost everything

`GET /me/{year}/exercises` → `ExerciseStudentRead[]` ✅. One request per
year yields the year's exercises *with* their submissions and feedback
embedded, so the per-exercise metadata needs no further calls:

```
ExerciseStudentRead
  id, year, module_code, number, title, type (CW|TUT|PPT|PMT|MMT|T|GF)
  spec               : str|null     ← non-null means a spec exists
  supplementary_file : str|null     ← non-null means a supplementary file exists
  model_answer       : str|null     ← non-null means a model answer exists
  submissions        : SubmissionRead[]
  feedback           : StudentFeedbackRead|null
  deliverables, mark, marks_published, weight, start, end, ...

SubmissionRead:  id, exercise_id, username, timestamp,
                 file_path: str|null, gitlab_hash: str|null,
                 target_submission_file_name, file_size
StudentFeedbackRead: id, distribution_id, timestamp, marker
```

Two consequences worth designing around:

- **The `spec`/`model_answer`/`supplementary_file` fields say up front
  whether the artefact exists**, so we never fire a request that we
  already know will 404. Same for `feedback` being null. (They are
  timestamps, not paths — non-null is the whole signal.) **Caveat:
  "exists" is not "we may have it" — every `model_answer` is a 403, see
  §9.3.**
- **A submission is either a file or a git commit.** `file_path` and
  `gitlab_hash` are both nullable; a LabTS-backed exercise records a
  commit hash and has no file to download. Those get metadata only —
  the code itself is what `gitlab_fetch` already clones. Confirmed in
  2324: of 31 own submissions, 15 carry `file_path` and 16 a
  `gitlab_hash` ✅.

### 3.3 ⚠️ "Personal" means personal *fields*, not a personal *list*

`/me/{year}/exercises` returns **every exercise in the department for
that year**, not the user's. Measured on 2324 ✅:

| | count |
|---|---|
| exercises returned | 473 across 110 modules |
| with a submission of mine | 26 |
| with any submission at all | 35 |
| with feedback for me | 30 |
| with a mark for me | 44 |
| modules I actually did | 12 |

What makes it "personal" is that `submissions`, `feedback` and `mark` are
populated **only where they relate to the requesting user**. So the
selection rule is local and needs no extra call:

> Keep an exercise if it has any `submissions`, a `feedback`, or a
> `mark`. Everything else is another cohort's coursework that happens to
> be in the same response.

The 9-exercise gap between "any submission" and "my submission" is
entirely group coursework ✅ — every one of those exercises has
`requires_group: true`, a mark for us, and usually feedback; the
submission is simply the one a teammate uploaded on the group's behalf.
That is our work and **is** downloaded. (Checked explicitly, because a
"personal" endpoint returning another username deserves a second look
before it's written off. Nothing in the response relates to anyone
outside our own groups.)

### 3.4 Do we need the `module_code` filter? No.

`module_code` is a **repeatable array** query param, defaulting to `[]`
= no filter ✅. Filtering server-side would need the user's module list
up front, and there is no endpoint that gives one:

- abc-api `/{year}/students/{username}` returns cohort, degree and
  personal details — **no modules** ✅
- abc-api `/{year}/modules` lists a year's modules with
  `applicable_cohorts` ✅ — that's what a cohort *could* take, not what
  this user did, so electives would be wrong in both directions
- the rest of abc-api is staff-facing (all-students-list, enrolled,
  careers, profile images, tutoring allocations) and out of bounds per §1

So: **one unfiltered request per year, filtered locally.** 421 KB for
2324, and at most 25 years, which is a few MB of JSON for the whole
history — cheaper and politer than a request per module, and it can't
miss a module we forgot to ask about. `module_code` stays available if a
year ever turns out to be unmanageable.

### 3.5 The download endpoints

All five declare **no response content type** ✅ — they're FastAPI
`FileResponse`s — so the filename has to come from `Content-Disposition`:

```
GET /{year}/{module_code}/exercises/{number}/spec
GET /{year}/{module_code}/exercises/{number}/model-answer
GET /{year}/{module_code}/exercises/{number}/supplementary
GET /{year}/{module_code}/exercises/{number}/submissions/{submission_id}/file
GET /distributions/{distribution_id}/feedback/{feedback_id}/file
```

## 4. Output layout

Output root per exercise, `{output_dir}/{year}/{module_code}/emarking`:

```
<output_dir>/<year>/<module_code>/emarking/
├── exercises.json                     the year's full response, per module
└── <number>-<title>/                  exercise output root (FS-safe)
    ├── exercise.json                  this exercise, extracted
    ├── spec.pdf
    ├── model-answer/<filename>
    ├── supplementary/<filename>
    ├── submissions/<id>/<target_submission_file_name>
    └── feedback/<id>/
        ├── metadata.json
        └── <filename>
```

Naming rules:

- `<number>-<title>` normalised for the filesystem: path separators and
  control characters replaced, trailing dots/spaces stripped (Windows),
  length-capped, and a non-empty fallback if a title normalises to
  nothing. Worth doing properly — titles are free text written by
  lecturers.
- Downloaded filenames come from `Content-Disposition` (§9.1) — except
  for submissions, where `target_submission_file_name` is preferred
  because the header's name carries an opaque uuid prefix. Failing both:
  the fixed stem (`model-answer`, `supplementary`, `feedback`) plus an
  extension guessed by running `file --extension`; failing *that*,
  `.bin`. A filename from the header is still sanitised — it is remote
  input and must never escape its directory (`../` and absolute paths).
- The per-feedback directory is load-bearing: every feedback file is
  served as `<username>.pdf`, so 69 of them would otherwise collide
  (§9.1).

## 5. Caching, and what a re-run does

Same contract as the other pipelines: re-running is cheap and safe, and
`--force` redoes everything.

- `exercises.json` is reused when present unless `--force`, so a re-run
  costs zero requests per year.
- A file already on disk is not re-downloaded. Truncation is caught
  against the response's `Content-Length` as it streams, **not** against
  the metadata `file_size`, which lies (§9.6).
- Downloads are streamed to a temp file and renamed into place, so an
  interrupted run never leaves a half-file that a later run would trust,
  and a 48 MB artefact never sits in memory (§9.6).
- A manifest (`emarking-files.json`) records what was fetched, what was
  skipped and why (no spec, submission is a commit hash, 404, 403, …),
  so "nothing was downloaded for this exercise" is always
  distinguishable from "nothing exists". Terminal outcomes — `403`,
  `absent` — are **not retried** on a re-run without `--force`; that is
  what stops every run spending 45 requests collecting the same refusals
  (§9.3).

## 6. Concurrency

Sequential by default, `--concurrency/-j` to fan out, exactly as
`gitlab_fetch` does: `asyncio` + `httpx.AsyncClient` behind a semaphore.
It's I/O-bound. The default stays 1 — this is a shared teaching server
and the whole point of §1 is not to hammer it.

## 7. Files to write

```
src/imperial_doc_download/emarking_fetch/
├── __init__.py       exports EmarkingFetchStep
├── proxy.py          the SSH SOCKS proxy context manager
├── client.py         GET-only async client: auth, headers, delay, retry, 404-as-absent
├── models.py         Exercise / Submission / Feedback, from_dict only
├── naming.py         pure: FS-safe names, Content-Disposition parsing, extension guessing
└── step.py           orchestration: years → exercises → artefacts
tests/
├── test_emarking_naming.py     pure functions, nasty titles and headers
├── test_emarking_client.py     httpx.MockTransport: retries, 404, never-POST
└── test_emarking_step.py       end-to-end against a mocked API, output layout + caching
```

Plus: `httpx[socks]` in `pyproject.toml`, an `emarking` CLI subcommand,
and a README section.

## 8. ⚠️ Reading `.env`: do not source it from a shell

Recon initially hit a `401 Could not validate credentials`, and LabTS
login failed with the same credentials — which looked exactly like an
expired password. It wasn't. The credentials are fine; the *loader* was
wrong:

```sh
set -a; . ./.env; set +a      # WRONG
```

The password contains shell metacharacters, so sourcing it expanded
part of it away: 17 characters in the file, 15 in the environment.
Verified by comparing lengths and hashes of the literal file value
against the sourced one — never print the value itself to check this.

Parse `.env` literally (`split("=", 1)`, no shell) — or better, export
the variables in the shell that launches the tool and let the tool read
`os.environ`, which is what the existing pipelines do. Credentials
should also be handed to subprocesses via stdin or an env dict, never
in argv, where they show up in `ps`.

## 9. Open questions — all answered ✅

Probed against the live API on 2026-09-18, GET-only, personal endpoints
only. Four of the six answers change what the code has to do, so they are
written up rather than just ticked.

### 9.1 `Content-Disposition` ✅ always present, always the simple form

All five endpoints return it, on every response observed, as

```
Content-Disposition: inline; filename="40001_1_spec.pdf"
```

`inline`, never `attachment`; `filename=` with a quoted value, **never
`filename*=`**. A real `Content-Type` comes back too
(`application/pdf`, `text/csv; charset=utf-8`, `application/zip`), which
the openapi schema didn't promise. So the parser must accept `inline` as
well as `attachment`, and the `file --extension` fallback in §4 is a
belt-and-braces path that in practice never fires.

Three filename shapes, and two of them are traps:

| Endpoint | Example | Note |
|---|---|---|
| spec / supplementary | `40001_2_supplementary.csv` | unique per exercise |
| submission file | `973f26563a484ef0a875f75faa8c67b0_cw1.pdf` | opaque uuid prefix — **prefer `target_submission_file_name`** (`cw1.pdf`) and keep the header name only as a fallback |
| feedback file | `kss22.pdf` | **it is the username, identical for every feedback file in every module** — so the per-feedback directory in §4 isn't tidiness, it's the only thing preventing 69 files from overwriting each other |

### 9.2 A commit-hash submission's `/file` → **500**, not 404 ✅

```
GET .../submissions/8483/file  →  500 Internal Server Error
```

Not a 404, and not a text file containing the hash. So this is not a
case to handle — it is a case to **never request**: `file_path is None`
means skip, record `skipped: submission is a git commit`, and let
`gitlab_fetch` be the thing that has the code. Requesting it anyway
would mean deliberately causing 52 server errors on a shared teaching
box per run.

### 9.3 Missing/forbidden artefacts ✅ — and model answers are never ours

A clean, distinguishable set of responses:

| Case | Response |
|---|---|
| artefact genuinely absent | `404 {"detail":"File not found."}` |
| exercise number doesn't exist | `404 {"detail":"Exercise not found."}` |
| no credentials | `401` |
| **model answer** | `403 {"detail":"You are not allowed to access this resource."}` |

**Every single model answer is 403.** All 45 exercises whose metadata
carries a non-null `model_answer` were probed and all 45 were refused,
including the 25 whose `model_answer_visible_on` is a date years in the
past. So `model_answer: <timestamp>` means *"a marker uploaded one"*,
not *"you may have it"* — and §3.2's "non-null means we never fire a
request that we already know will 404" does not hold for this field.

Consequences:

- 403 is a **terminal, expected outcome**, not an error and not
  retryable. It gets recorded in the manifest as `forbidden` and
  reported as a count, not as 45 warnings.
- A recorded terminal outcome is **not retried on the next run** unless
  `--force`. Otherwise every re-run spends 45 requests being told no.

### 9.4 Rate limits ✅ none, in the documentation or the headers

No `Retry-After`, no `X-RateLimit-*`, no `RateLimit-*` on any response,
authenticated or not. The full header set is
`server: openresty`, `date`, `content-type`, `content-length`,
`connection`, `x-frame-options: DENY`, `x-served-by`. Nothing throttles
us, which is exactly why §1's sequential-with-a-delay default stays.

### 9.5 Years ✅ 2223–2526, and the other 21 fail rather than come back empty

`/years` advertises 25 years; only four have anything. An unused year
does **not** return `[]`:

| Years | Response |
|---|---|
| 0203–1617 (15 years) | `500 Internal Server Error` |
| 1718–2122, 2627 (6 years) | `502 {"detail":"ABC API call returned a 404: Student not found."}` |
| **2223, 2324, 2425, 2526** | `200` |

So "this year has nothing for me" is indistinguishable from "the server
broke" by status code alone, and a year that errors **must not fail the
run** — it is the normal case for 21 years out of 25. Log it at debug,
record it in the manifest, move on. (Matches LabTS, which shows
2223–2526 for this account.)

Per-year totals, after §3.3's keep-rule:

| Year | exercises returned | ours |
|---|---|---|
| 2223 | 471 | 66 |
| 2324 | 473 | 44 |
| 2425 | 438 | 16 |
| 2526 | 500 | 20 |
| | | **146** |

### 9.6 Sizes ✅ stream everything, and don't trust `file_size`

| | |
|---|---|
| submissions with a file | 106 (of 158; the other 52 are commit hashes) |
| submission bytes total | **178.7 MB** |
| largest submission | 13.2 MB |
| largest artefact of any kind | **47.9 MB** (a supplementary zip) |
| specs / supplementary / feedback | 110 / 9 / 69 |

Two things follow:

- 48 MB in one response is enough to stream to disk rather than buffer,
  so downloads use `client.stream()` and write as they go. There is no
  declared size for spec/supplementary/feedback, so this isn't optional.
- **`file_size` is unreliable.** Six submissions report `file_size: 0`
  and then serve real content — one reports `0` and returns 39,167
  bytes. So §5's "a truncated file can be spotted" must use the
  response's `Content-Length`, which was correct on every response
  observed, and treat the metadata `file_size` as a hint only.

### 9.7 Not a question, but worth flagging 🔒

Group submissions (§3.3) carry the uploader's username, and 10 distinct
teammate usernames appear across our 158 submissions. `mark.marker`,
`marks_published_by` and `locked_by` likewise name staff. These are
embedded in the API response, and the submission id is needed to
download our own group's work, so the metadata is kept — but it does
mean `exercises.json` contains other people's logins, and the output
directory is not something to share casually. See `labts-fetch-plan.md`
§2.5 for the same concern handled the other way (that data was
droppable; this isn't).
