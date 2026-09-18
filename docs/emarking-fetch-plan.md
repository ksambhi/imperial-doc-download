# Implementation plan: `emarking_fetch`

Download every coursework submission, spec, model answer, supplementary
file and feedback file from DoC's eMarking system.

Status: **plan only, nothing implemented.** Everything marked ✅ was
verified against the live API; everything marked ❓ needs a working
password before it can be checked (see §8).

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
  already know will 404. Same for `feedback` being null.
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
- Downloaded filenames come from `Content-Disposition`. Failing that:
  the fixed stem (`model-answer`, `supplementary`, `feedback`) plus an
  extension guessed by running `file --extension`; failing *that*,
  `.bin`. A filename from the header is still sanitised — it is remote
  input and must never escape its directory (`../` and absolute paths).

## 5. Caching, and what a re-run does

Same contract as the other pipelines: re-running is cheap and safe, and
`--force` redoes everything.

- `exercises.json` is reused when present unless `--force`, so a re-run
  costs zero requests per year.
- A file already on disk is not re-downloaded. `file_size` is in the
  metadata for submissions ✅, so a truncated file can be spotted rather
  than trusted; for the others there is no declared size, so presence is
  the test.
- Downloads are written to a temp file and renamed into place, so an
  interrupted run never leaves a half-file that a later run would trust.
- A per-year manifest (`emarking-files.json`) records what was fetched,
  what was skipped and why (no spec, submission is a commit hash, 404,
  …), so "nothing was downloaded for this exercise" is always
  distinguishable from "nothing exists".

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

## 9. Open questions (❓ — still unverified)

Auth, the proxy and the shape of the exercise list are all confirmed
against the live API. These remain, and should be checked against real
responses before the code is trusted:

1. Does `Content-Disposition` actually come back on each of the five
   download endpoints, and in what form (`filename=` vs `filename*=`)?
2. Does a commit-hash submission's `/file` endpoint 404, or return
   something (a text file containing the hash, say)?
3. What does the API do when an artefact the metadata promised is
   missing — 404, 500, or an empty body?
4. Are there response headers hinting at rate limits once authenticated?
   (None are documented ✅, and none were seen on the requests made.)
5. Which years does this account actually have exercises for? LabTS says
   2223-2526; `/years` offers 25 years back to 0203, and asking for all
   of them is 25 cheap requests — but confirm empty years respond
   sensibly rather than erroring.
6. How big can a single submission file be, and does anything need
   streaming to disk rather than buffering? (`file_size` is in the
   metadata ✅, so this is answerable before downloading.)
