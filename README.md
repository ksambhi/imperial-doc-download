# imperial-doc-download

Download my data from Imperial DoC systems before my account gets nuked at graduation.

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
│       │   ├── step.py         #   the download Step
│       │   └── marks_step.py   #   the marks-record Step
│       └── scientia_fetch/     # placeholder: Scientia (timetable/exams)
└── tests/
    └── fixtures/labts/         # anonymised real markup, for offline tests
```

The pipeline is generic on purpose: `Pipeline` runs a list of `Step`s
against one shared `PipelineContext` (output directory, settings, and a
scratch `state` dict steps can use to pass data along, e.g. an
authenticated session from a login step to the steps that use it).

Each Imperial system to back up is its own self-contained module —
`labts_fetch`, `gitlab_fetch`, `emarking_fetch`, and so on — each
implementing one `Step` subclass. Those three are implemented;
`scientia_fetch` is still a placeholder. Each gets its own CLI
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

This step costs no requests — it reshapes what the download step already
has, or reads it back off disk. `imperial-doc-download emarking-marks`
rebuilds it on its own without touching the network.

Two things here that look wrong until you check them:

- **`pass_mark` is a percentage, not a mark.** 80 of this account's 138
  marked exercises have a `pass_mark` larger than their own
  `maximum_mark` (40, out of a maximum of 10). Comparing it against the
  raw mark reports failures that never happened.
- **The colour's precedence is assessment before group-ness.** An
  unmarked *group* exercise is brown or grey, not purple — there is no
  fifth "unassessed group" swatch.

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

## Usage

```bash
uv sync

uv run imperial-doc-download --help
uv run imperial-doc-download labts --output-dir ./imperial-data --dry-run

export IMPERIAL_USERNAME=abc123
export IMPERIAL_PASSWORD=...                       # or you'll be told what's missing up front
export IMPERIAL_GITLAB_SSH_KEY=~/.ssh/doc_gitlab   # key registered with DoC GitLab
export IMPERIAL_DOC_SSH_KEY=~/.ssh/doclab          # key for the shell servers

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
```

> **Don't `source` your `.env`.** If your password contains shell
> metacharacters, `set -a; . ./.env` silently truncates it and you get a
> `401` that looks exactly like an expired password. Export the variables,
> or have whatever launches the tool parse the file literally.

There is one subcommand per Imperial system, each running its own
pipeline:

| Command | Status |
|---|---|
| `imperial-doc-download labts` | implemented — fetches the repository list, then clones |
| `imperial-doc-download gitlab` | implemented — the clone half on its own |
| `imperial-doc-download emarking` | implemented — coursework files, then the marks record |
| `imperial-doc-download emarking-marks` | implemented — rebuilds the marks record offline |
| `imperial-doc-download scientia` | placeholder |

Eventually the root command will run them all in parallel. That only
makes sense once each pipeline works on its own, so it isn't wired up
yet — run them individually for now.

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
