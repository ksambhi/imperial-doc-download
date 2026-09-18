# imperial-doc-download
Original Claude README.

Download data from Imperial DoC systems before your account gets nuked at graduation.

A `uv`-managed, Typer-based CLI. Python 3.12.

## Project layout

```
imperial-doc-download/
├── pyproject.toml
├── src/
│   └── imperial_doc_download/
│       ├── cli.py              # Typer app + `run` command
│       ├── config.py           # Settings, loaded from env vars
│       ├── logging.py          # coloured console + file logging
│       ├── ssh.py              # shared: throwaway ssh config, keys, agent
│       ├── proxy.py            # shared: ssh -D SOCKS tunnel into DoC
│       ├── doc_api.py          # shared: GET-only async client for the DoC APIs
│       ├── naming.py           # shared: pure, FS-safe names
│       ├── pipeline/           # generic pipeline machinery
│       │   ├── base.py         #   Step, PipelineContext
│       │   └── runner.py       #   Pipeline: runs an ordered list of Steps
│       ├── labts_fetch/        # LabTS: the repo list  (implemented)
│       │   ├── client.py       #   GET-only HTTP client, login + retries
│       │   ├── parsing.py      #   pure HTML parsing, no I/O
│       │   ├── models.py       #   Exercise / Milestone
│       │   └── step.py         #   the pipeline Step
│       ├── gitlab_fetch/       # DoC GitLab: the repos  (implemented)
│       │   ├── ssh.py          #   the GitLab/gitolite Host blocks
│       │   ├── cloning.py      #   what to clone where, and running git
│       │   └── step.py         #   the pipeline Step
│       ├── emarking_fetch/     # eMarking: coursework + marks  (implemented)
│       │   ├── proxy.py        #   ssh -D SOCKS proxy into the DoC network
│       │   ├── client.py       #   GET-only async client, retries, streaming
│       │   ├── models.py       #   Module / Exercise / Submission / Feedback
│       │   ├── naming.py       #   pure: FS-safe names, header parsing
│       │   ├── grading.py      #   pure: grade boundaries, status colours
│       │   ├── marks.py        #   pure: builds the marks record
│       │   ├── results_html.py #   pure: renders the results page
│       │   ├── step.py         #   the download Step
│       │   └── marks_step.py   #   the marks-record Step
│       ├── materials_fetch/    # eMarking materials: lecture notes  (implemented)
│       │   ├── client.py       #   the one route: /resources/zipped
│       │   ├── archive.py      #   pure: safe zip extraction
│       │   └── step.py         #   the pipeline Step
│       └── scientia_fetch/     # placeholder: Scientia (timetable/exams)
└── tests/
    └── fixtures/labts/         # anonymised real markup, for offline tests
```

The pipeline is generic on purpose: `Pipeline` runs a list of `Step`s
against one shared `PipelineContext` (output directory, settings, and a
scratch `state` dict steps can use to pass data along, e.g. an
authenticated session from a login step to the steps that use it).

Each Imperial system to back up is its own self-contained module —
`labts_fetch`, `gitlab_fetch`, `emarking_fetch`, `materials_fetch`, and
so on — each implementing one `Step` subclass. Those four are
implemented; `scientia_fetch` is still a placeholder. What they share —
the SOCKS proxy, the GET-only HTTP client, filename sanitising — lives at
the top level rather than in whichever pipeline needed it first. Each gets its own CLI
subcommand in `cli.py`, so pipelines can be built and run one at a time.
(Module names use underscores, not hyphens — `-` isn't valid in a Python
import name.)

A pipeline can be more than one step: `labts` runs `labts-fetch` and then
`gitlab-fetch`, the second consuming what the first stashed in
`ctx.state`. Both steps reuse what the last run produced unless `--force`
is passed, so re-running is cheap and picks up where it left off.

### `labts_fetch`

Logs in to LabTS, walks every academic year, and writes the list of GitLab
repositories behind every exercise. Cloning those repos is a later,
separate step. Two files are produced:

- `<output_dir>/labts-list.json` — the full record: clone URLs (ssh +
  https), the milestones, and the submitted revision for each milestone
- `<output_dir>/labts-list.txt` — just the ssh clone URLs, grouped by
  academic year, for feeding straight into the clone step

Both land in `--output-dir`. Real output contains personal data, so
output directories (including `results/`, the conventional place to put a
real run) are gitignored and must not be committed.

Requires `IMPERIAL_USERNAME` and `IMPERIAL_PASSWORD` in the environment.

LabTS is a slow, shared teaching server, so the client is strictly
sequential, waits between requests, uses long timeouts and retries with
backoff. It is also **GET-only by construction** — LabTS pages carry forms
that submit coursework late or queue jobs on the shared test-VM fleet, so
there is deliberately no `post()` helper anywhere except the login itself.
A full run is roughly 4–6 minutes.

Pass `--cache-dir DIR` to cache fetched pages there and reuse them next
time. A cache hit costs no request, no delay and no login, so a fully
cached re-run does zero network I/O — measured at **4m22s → 2.1s**, with
byte-identical output. Delete the directory to force a refetch.

See `docs/labts-fetch-plan.md` for how the site was reverse-engineered and
why the output is shaped the way it is.

Re-running reuses an existing `labts-list.json` instead of spending
another 4–6 minutes on LabTS. `--force` refetches.

### `gitlab_fetch`

Clones every repository `labts_fetch` found, one clone per repository —
a multi-milestone exercise like PintOS is a single repo with three
submitted revisions in it, so it's cloned once:

```
<output_dir>/<academic_year>/gitlab/<repo>/     e.g. results/2324/gitlab/pintos_17/
<output_dir>/gitlab-clones.json                 what was cloned, from where, at which commit
```

These are archives of repositories that are about to be deleted, so a
plain `git clone` isn't enough. Each one is finished off with:

- **every ref the server has**, not just the branches and tags a clone
  fetches. DoC's GitLab also keeps `refs/keep-around/*` (commits it pins
  deliberately — force-pushed work, commits referenced from merge
  requests) and `refs/merge-requests/*`, which can be the *only* thing
  reaching a commit. In `pintos_17`, **159 of its 477 keep-around refs
  point at commits no branch or tag reaches**; a plain clone leaves them
  on the server to die. They're fetched into `refs/imperial-mirror/*`.
- **a local branch for every remote branch**, so the repository still
  looks complete once `origin` no longer exists (`pintos_17`: 35).
- **the submitted revision checked out** — on the branch where one points
  at it (the usual case, it's the tip of `master`), detached at the
  revision where none does (an earlier milestone with later work on top).
  With several milestones, HEAD lands on the last one's.

Two remotes are tried per repository:

1. the GitLab ssh URL, straight from `gitlab.doc.ic.ac.uk`;
2. failing that, the same path on `gitolite.doc.ic.ac.uk`.

The fallback matters: GitLab stops serving old years' repositories (every
2223 and 2324 repo tested came back "could not be found"), but gitolite
still has them. gitolite is behind the departmental firewall, so it's
reached by proxy-jumping through a DoC shell server — one of
`shell1-5.doc.ic.ac.uk`, picked at random per run to spread the load.
That needs two different keys: the shell-server key to make the jump, and
the GitLab key to authenticate to gitolite at the other end.

```
IMPERIAL_GITLAB_SSH_KEY   key registered with DoC GitLab (and gitolite)   [required]
IMPERIAL_DOC_SSH_KEY      key for the DoC shell servers, to jump with     [gitolite fallback]
IMPERIAL_USERNAME         DoC username, to jump as                        [gitolite fallback]
```

Without the last two, repositories GitLab still serves clone fine and the
rest fail — the run says so rather than pretending otherwise.

**Your `~/.ssh/config` is never read or written.** All of the above goes
into a throwaway config in a temporary directory, handed to git as
`GIT_SSH_COMMAND="ssh -F …"` and deleted when the run ends. (So the tool
works the same whether or not you happen to have DoC hosts configured.)
Unknown host keys are recorded with `StrictHostKeyChecking=accept-new`,
which still aborts loudly if a *known* host's key ever changes, and
`BatchMode=yes` means a run fails with a message rather than hanging on a
prompt.

Passphrase-protected keys are unlocked once per run, not once per clone:
if the key isn't already in your own `ssh-agent`, a private agent is
started for the run, the key is unlocked into it, and it's killed at the
end. Supply the passphrase in `IMPERIAL_GITLAB_SSH_KEY_PASSPHRASE` /
`IMPERIAL_DOC_SSH_KEY_PASSPHRASE`, or be prompted for it once. (These two
are environment-only, deliberately — a passphrase passed as a CLI flag
would end up in shell history and `ps` output.)

Cloning is sequential by default. It's I/O-bound, so the runner is built
on `asyncio` subprocesses behind a semaphore: `--concurrency/-j N` clones
N at a time. Be modest with it — the gitolite route puts every clone
through a shared teaching server.

Already-cloned repositories are left alone on the next run (that's what
`gitlab-clones.json` is for), and a repository that failed is retried.
`--force` re-clones everything from scratch, replacing what's there.

### `emarking_fetch`

Downloads every coursework artefact eMarking holds for this account —
specs, submissions, supplementary files and feedback — into

```
<output_dir>/<year>/<module_code>/emarking/
├── exercises.json                     this module's exercises
└── <number>-<title>/
    ├── exercise.json
    ├── <module>_<n>_spec.pdf
    ├── supplementary/<filename>
    ├── submissions/<id>/<filename>
    └── feedback/<id>/{metadata.json,<filename>}
```

with a record of everything in `<output_dir>/emarking-files.json`, plus
the marks record described below.

**Scope is your enrolment, not just what you submitted.** The module list
comes from abc-api's `/{year}/students?login=<you>`, and everything in
those modules is downloaded — including tutorials and optional
courseworks you never attempted, because their specs are worth keeping.
That client method takes no username argument, so it can't be aimed at
another student, and only the module code and title are ever persisted
from a record that also contains your cid, email and personal tutor.

**This one is read-only by construction, and deliberately so.** eMarking
is not a reports system: among its routes are ones that submit coursework,
edit feedback and write marks. So the client exposes exactly one place a
request is issued, with `GET` as a literal, and there is no
`post()`/`put()`/`delete()` helper anywhere — a test asserts both. It also
only ever touches personal endpoints (`/me/*` and our own submissions),
never the staff routes, and is sequential with a delay between requests by
default because it's a shared teaching server.

Both API hosts live behind the DoC firewall, so everything goes through an
SSH SOCKS proxy (`ssh -N -D`) on a randomly chosen `shell1-5`, on a free
ephemeral port picked at runtime. Keys, passphrases and the throwaway ssh
config are handled by the same shared machinery the clone step uses.

Re-runs are cheap and quiet. A file already on disk is left alone, and an
answer the API has settled — a `404`, a `403`, a year you weren't
enrolled in — is not asked about again. `--force` redoes everything, and
`--year 2526` re-checks a single year (worth doing for the current one,
which can gain data after an empty run).

**Model answers are opt-in and off by default.** Every one returns `403`,
and that is the system working rather than something to route around:
releasing them would leak future years' answers. `--model-answers` tries
anyway, if you want the refusals on record.

### The marks record

`emarking-marks` then writes `<output_dir>/emarking-marks.json` — the
personal-record page as JSON. Every exercise with a mark, across every
year, with its deadline, mark, percentage, grade, pass/fail and status
colour, grouped by module:

```json
{
  "number": 1,
  "label": "1 | CW: CPU Microarchitecture design space exploration",
  "category": "individual", "colour": "green",
  "deadline": "2024-11-06T19:00:00+00:00",
  "submitted": true, "mark": 14, "maximum_mark": 20,
  "percentage": 70.0, "grade": "A",
  "pass_mark": 40, "pass_mark_is_percent": true, "passed": true,
  "has_feedback": true
}
```

The grade and the colour are **computed** — neither is in the API, since
the page derives both in the browser. The boundaries (`A*` ≥ 80, `A` ≥ 70,
…) and the colour legend are written into the file itself, so the record
explains its own vocabulary. Colours follow the page: green is an
individual exercise, purple a group one.

It also writes **`emarking-results.html`**: a local reconstruction of the
eMarking coursework-results page, self-contained so it still opens once
the account is gone. Three differences from the original — a dark/light
toggle (the real page is dark only), tabs per academic year rather than
per section, and **Feedback links that download the PDF saved next to the
page** instead of calling an API you may no longer be able to reach. The
`download` attribute renames them on the way out, because feedback files
are named after a person rather than the exercise — yours arrive as
`<username>.pdf`, and a group exercise's after whichever teammate it was
distributed to, so otherwise you'd collect a pile of identically-named
files.

Both steps cost no requests — they reshape what the download step already
has, or read it back off disk. `imperial-doc-download emarking-marks`
rebuilds both files on its own without touching the network.

Two things here that look wrong until you check them:

- **`pass_mark` is a percentage, not a mark.** 80 of this account's 138
  marked exercises have a `pass_mark` larger than their own
  `maximum_mark` (40, out of a maximum of 10). Comparing it against the
  raw mark reports failures that never happened.
- **"Unassessed" means zero-weighted, not unmarked.** A progress test can
  be submitted, marked 10/10 and graded `A*` and still be unassessed,
  because it contributes nothing to the module — the page colours those
  brown. The rule is `weight > 0`, and precedence is assessment before
  group-ness, so an unassessed *group* exercise is brown or grey rather
  than purple.

Some things the live API does that the code is built around, rather than
assuming otherwise — the evidence is in `docs/emarking-fetch-plan.md` §9:

- `file_size` in the metadata is **not** reliable (some submissions claim
  `0` and serve real content), so a transfer is verified against the
  response's `Content-Length` instead
- every feedback file is served as `<username>.pdf`, whatever the module,
  hence the per-feedback-id directory
- a `403` is a normal answer, not just for model answers: supplementary
  files in modules you enrolled in but didn't take are refused too
- a submission can be a git commit rather than a file; those are recorded
  and never requested (that endpoint `500`s for them — `gitlab_fetch` is
  what has the code)
- the largest artefact seen is 48 MB, so downloads stream to a temp file
  and are renamed into place

### `materials_fetch`

Downloads one zip of teaching materials per enrolled module — lecture
notes, slides, handouts — and extracts it into
`<output_dir>/<year>/<module_code>/materials/`, beside that module's
`emarking/` directory:

```
2425/60019/materials/
├── .materials.json                     what was extracted, and from which zip
├── Lectures/(0) Lecture 1-Introduction.pdf
└── Practicals/(1) Practical 2-Sensors and Feedback Control.pdf
```

Scope is the same enrolment list `emarking` uses and caches, so running
that first means this costs no extra abc-api requests.

**The zip is deleted once it has extracted cleanly** — the full set is
~1.75 GB and keeping both halves doubles that. `--keep-zip` retains them.
A failed extraction keeps its zip, so a retry doesn't re-download 189 MB.
The `.materials.json` marker is written last and is what makes a re-run
free: modules already extracted cost no request at all.

**Extraction is the careful part.** A zip names its own output paths and
these come off the network, so `archive.py` plans the extraction before
writing anything: members resolving outside the target are dropped and
the result re-checked, symlink entries are skipped rather than inherited
as files, and declared uncompressed size and member count are capped
before a byte is written. It stages in a temp directory and moves into
place, so a failure leaves the previous tree intact.

Every member of these zips is prefixed with the module code, which would
nest it twice. That prefix is stripped when — and only when — every
member shares it, since stripping otherwise could merge two trees.

Measured on this account: **46 zips, 1.75 GB, ~21 min sequential**; four
modules publish no materials and answer `404`, which is recorded rather
than retried as an error.

## Usage

```bash
uv sync
uv run imperial-doc-download --help
```

Credentials come from the environment. Put them in a `.env` in the
working directory and they're picked up automatically:

```
IMPERIAL_USERNAME=abc123
IMPERIAL_PASSWORD=...
IMPERIAL_GITLAB_SSH_KEY=/home/you/.ssh/doc_gitlab   # key registered with DoC GitLab
IMPERIAL_DOC_SSH_KEY=/home/you/.ssh/doclab          # key for the shell servers
```

> **Don't `source` it.** That file is parsed literally, on purpose. If
> your password contains shell metacharacters, `set -a; . ./.env` mangles
> it — measured on this account: **17 characters in the file, 15 in the
> environment**, and a `401` that looks exactly like an expired password.
> Exporting the variables yourself works fine too, and anything already
> exported wins over the file.

**One command for everything:**

```bash
# LabTS list → clone every repo → eMarking files → marks record + web page
uv run imperial-doc-download all

# ...and the ~1.75 GB of teaching materials too
uv run imperial-doc-download all --materials
```

`all` runs every pipeline against one shared context and **keeps going if
one fails** — LabTS being down is no reason to lose the eMarking half.
What failed is listed at the end and the exit code is non-zero. Re-run to
retry just the failures; everything already downloaded is left alone.

Each pipeline has a switch: `--skip-labts`, `--skip-gitlab`,
`--skip-emarking`, and `--skip-materials`. **Materials is the only one
skipped by default** — at ~1.75 GB it's an order of magnitude more than
everything else combined, so `--materials` opts in.

`-j/--concurrency` defaults to this machine's CPU count. The HTTP
pipelines still wait `--delay` after each request, so raising it raises
the request rate rather than removing the throttle — lower it, or raise
`--delay`, against anything that looks strained.

Or run the pipelines separately:

```bash
# fetch the repository list, then clone every repository
uv run imperial-doc-download labts --output-dir ./imperial-data

# re-runs are cheap: the list and any finished clones are reused
uv run imperial-doc-download labts --output-dir ./imperial-data --cache-dir ./cache

# clone only, from an earlier run's list — e.g. to retry failures
uv run imperial-doc-download gitlab --output-dir ./imperial-data

# start over, four clones at a time
uv run imperial-doc-download labts --output-dir ./imperial-data --force -j 4

# download every coursework file from eMarking (needs IMPERIAL_DOC_SSH_KEY
# for the SOCKS proxy). A re-run costs no requests for what it already has.
uv run imperial-doc-download emarking --output-dir ./imperial-data

# just one year, at a gentler rate
uv run imperial-doc-download emarking --year 2526 --delay 2

# rebuild emarking-marks.json from an earlier run, no network
uv run imperial-doc-download emarking-marks

# teaching materials; needs emarking's enrolment cache, or --year
uv run imperial-doc-download materials --keep-zip
```

There is one subcommand per Imperial system, each running its own
pipeline:

| Command | Status |
|---|---|
| `imperial-doc-download all` | implemented — **everything**, in one go |
| `imperial-doc-download labts` | implemented — fetches the repository list, then clones |
| `imperial-doc-download gitlab` | implemented — the clone half on its own |
| `imperial-doc-download emarking` | implemented — coursework files, then the marks record |
| `imperial-doc-download emarking-marks` | implemented — rebuilds the marks record offline |
| `imperial-doc-download materials` | implemented — lecture notes and handouts (~1.75 GB) |
| `imperial-doc-download scientia` | placeholder |

`--dry-run` lists the steps that would run without downloading anything.
`-v/--verbose` enables debug logging. `--force` ignores everything a
previous run produced and does it all again.

### Logging

Every run logs to two places:

- the terminal, coloured: `[HH:MM:SS] module LEVEL message` — grey
  timestamp, magenta module (the logger's `__name__`, i.e. which
  `*_fetch` stage it came from), level coloured by severity, message in
  the normal terminal colour
- a plain-text file under `logs/`, named after when the run started, so
  a full run's output survives independent of terminal scrollback

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

CI (`.github/workflows/ci.yml`) runs ruff (lint + format check) and pytest
on every push/PR.
