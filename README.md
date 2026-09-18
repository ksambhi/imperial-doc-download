# imperial-doc-download

Download your data from Imperial College's Department of Computing before
your account is deleted. Created using Claude Code in about a day (included in the background whilst I was abroad).

This system will use the Scientia APIs to download the following:
1. Your coursework submissions and feedback from Scientia
2. Your marks from Scientia, presented in a single HTML page for offline viewing, and JSON copies as well
3. Your LabTS submissions
4. Your GitLab repositories _that were submitted on LabTS_, including all branches and tags. Note that some of these would have been archived from GitLab to the deparmtner gitolite, so this tool will also attempt to clone from gitolite if the GitLab clone fails.

Note that it do not download your complete GitLab record, as once repos were archived there was no apparent way of listing them; the best we can do is get their old URLs from LabTS, rewrite them for the archive gitolite server, and clone from there. 

A `uv`-managed CLI. Python 3.12.

## Pre-requisites
1. Python 3.12
2. `uv` (https://docs.astral.sh/uv/)
3. Your Imperial shortcode and password
4. An SSH key setup for the DoC shell server (see https://www.imperial.ac.uk/computing/people/csg/guides/remote-access/ssh/)
5. An SSH key setup for DoC GitLab.

The Scientia APIs used live behind the departmental firewall, so the tool opens an SSH SOCKS tunnel through a shell server for you. You don't need to be on the VPN, but you do need that shell-server key to work.

## Quick Start
### Installation
```bash
git clone https://github.com/ksambhi/imperial-doc-download
cd imperial-doc-download
uv sync
```

## Run the download
```bash
export IMPERIAL_USERNAME=abc123
export IMPERIAL_PASSWORD=your-college-password

# Private keys for the DoC shell server and GitLab, respectively
# (If you used my SSH script from https://github.com/ksambhi/some-tools/blob/master/ssh.sh, your SSH key will be at ~/.ssh/doclab_ecdsa)
# (if your keys have passphrases, see the note after)
export IMPERIAL_GITLAB_SSH_KEY=/home/you/.ssh/id_rsa
export IMPERIAL_DOC_SSH_KEY=/home/you/.ssh/id_rsa

uv run imperial-doc-download --help
uv run imperial-doc-download all \
  --output-dir ./results \
  --cache-dir ./results/.cache
```

**Re-running is safe and cheap.** Every step reuses what's already on
disk, so if a run is interrupted, run it again and it will pick up where it stopped. Nothing already downloaded is fetched twice.

Note that this process may take a while, especially if your GitLab repositories are large. The `--cache-dir` option will cache the LabTS pages (used to work out what GitLab repos to fetch), so re-running the tool will be much faster.

The environment variables can also be set in a `.env` file in the working directory, which is automatically picked up by the tool.

> ### ⚠️ Don't `source` your `.env`
>
> The tool parses that file literally, and deliberately so. If your
> password contains shell metacharacters, `set -a; . ./.env` silently
> mangles it. On the account this was built against that meant **17
> characters in the file and 15 in the environment**, producing a `401`
> that looks exactly like an expired password. Let the tool read the
> file, or export the variables yourself.

### Note on SSH keys with passphrases
If your keys have passphrases, either load them into your `ssh-agent` first or set `IMPERIAL_GITLAB_SSH_KEY_PASSPHRASE` / `IMPERIAL_DOC_SSH_KEY_PASSPHRASE`. Those two are environment-only on purpose - a passphrase passed as a command-line flag ends up in your shell history and in `ps` output.

---

## What you get

| System | What it saves |
|---|---|
| **LabTS** | the list of every exercise and the GitLab repository behind it |
| **DoC GitLab / gitolite** | a full clone of every repository, including refs a normal clone leaves behind |
| **emarking** | every coursework spec, your submissions, supplementary files and marker feedback |
| **emarking marks** | your complete marks record as JSON, plus an offline web page that reproduces the Scientia results page for viewing |

---


## What lands on disk

```
imperial-data/
├── labts-list.json              every exercise, its repo, and its submitted revisions
├── labts-list.txt               just the clone URLs
├── gitlab-clones.json           what was cloned, from where, at which commit
├── emarking-files.json          every coursework file, and what happened to it
├── emarking-marks.json          your complete marks record
├── emarking-results.html        ← open this one
└── 2425/
    ├── emarking-enrolment.json  the modules you took that year
    ├── gitlab/
    │   └── pintos_17/           a full clone
    └── 60019/                   one directory per module
        ├── emarking/
        │   ├── exercises.json
        │   └── 1-Practicals/
        │       ├── exercise.json
        │       ├── 60019_1_spec.pdf
        │       ├── submissions/133660/coursework.pdf
        │       └── feedback/304338/abc123.pdf
```

**Open `emarking-results.html` first.** It's a self-contained
reconstruction of the Scientia coursework-results page, with a dark/light toggle and tabs per academic year. The Feedback links download the PDF saved next to the page, so it
keeps working offline and after your account is gone.

Everything here is personal data. The output directory is gitignored;
keep it that way.

---

## The systems
Documentation by Claude Code.

### LabTS and GitLab

`labts` logs in, walks every academic year, and records every exercise
with the GitLab repository behind it. Then `gitlab` clones each one.

These are archives of repositories about to be deleted, so a plain `git
clone` isn't enough. Each clone also gets:

- **every ref the server has.** DoC's GitLab keeps `refs/keep-around/*`
  (force-pushed work, commits referenced from merge requests) and
  `refs/merge-requests/*`, which can be the *only* thing reaching a
  commit. In one real repository, **159 of 477 keep-around refs pointed
  at commits no branch or tag reached** — a normal clone leaves those on
  the server to die. They're fetched into `refs/imperial-mirror/*`.
- **a local branch for every remote branch**, so the repository still
  looks complete once `origin` no longer exists.
- **your submitted revision checked out** — on a branch where one points
  at it, detached at the revision where none does.

Old years' repositories are no longer served by GitLab, so the tool falls
back to `gitolite.doc.ic.ac.uk`, which still has them. That's behind the
firewall, so it proxy-jumps through the shell servers - hence the need for your SSH key. Without it, repositories GitLab still serves clone
fine and the rest fail, and the run tells you which.

**Your `~/.ssh/config` is never read or modified.** Everything goes into
a throwaway config in a temporary directory that's deleted when the run
ends, so the tool behaves the same whether or not you have DoC hosts
configured.

A full LabTS walk takes 4–6 minutes. `--cache-dir DIR` caches the pages,
which makes a re-run near-instant (measured: **4m22s → 2.1s**, with
identical output).

### eMarking

Downloads every coursework artefact: specs, your submissions,
supplementary files and marker feedback.

**Scope is your enrolment, not just what you submitted.** Everything in
the modules you took is downloaded, including tutorials and optional
courseworks you never attempted — their specs are worth keeping too.

Some things worth knowing:

- **Model answers are skipped by default.** Every one returned `403` during testing, and
  the author decided to avoid investigating further as releasing them would leak future years' answers. `--model-answers` asks anyway, if you want the refusals on record.
- **Submissions that are git commits aren't downloaded here** — that's
  what the GitLab clones are for. They're recorded so you can see the
  link.
- **Some artefacts are refused** (`403`) or were never uploaded (`404`).
  Both are recorded in `emarking-files.json` rather than silently
  skipped, so "nothing was downloaded" is always distinguishable from
  "nothing exists".

### Your marks record

`emarking-marks.json` is your complete record: every marked exercise,
across every year, with its deadline, mark, percentage, grade, pass/fail, grouped by module.

The grade are **computed** — it is not in the API,
because the real page works them out in the browser. The grade
boundaries (`A*` ≥ 80, `A` ≥ 70, …) is written into the file itself, so the record explains its own vocabulary.

Two things that look wrong until you check them:

- **`pass_mark` is a percentage, not a mark.** On the test account, 80 of
  138 marked exercises had a `pass_mark` larger than their own
  `maximum_mark` — 40, on something marked out of 10. Comparing it
  against the raw mark reports failures that never happened.
- Colours are based on the same ones used by Scientia itself

This step makes no network requests at all — it reshapes what's already
been downloaded. `imperial-doc-download emarking-marks` rebuilds both the
JSON and the web page offline.

---

## Commands

| Command | What it does |
|---|---|
| `all` | every pipeline, in one go (materials opt-in) |
| `labts` | fetch the repository list, then clone everything |
| `gitlab` | the clone half on its own, from an earlier run's list |
| `emarking` | coursework files, then the marks record and web page |
| `emarking-marks` | rebuild the marks record and page offline |
| `scientia` | not implemented yet |

### Common options

| Option | | Where |
|---|---|---|
| `-o, --output-dir` | where to put everything (default `./imperial-data`) | all commands |
| `--dry-run` | list the steps without downloading anything | all commands |
| `--force` | ignore what a previous run produced and redo it | most commands |
| `-j, --concurrency` | how many clones/downloads at once (default: your CPU count) | most commands |
| `--delay` | seconds to wait after each API request (default 1.0) | `all`, `emarking`, `materials` |
| `--year` | limit to specific years, e.g. `--year 2425 --year 2526` | `emarking`, `materials` |
| `--cache-dir` | cache LabTS pages, making re-runs instant | `all`, `labts` |
| `--keep-zip` | keep materials zips after extracting | `all`, `materials` |
| `--jump-host` | pin the shell server instead of picking one at random | all commands |

Two options belong to the tool rather than a subcommand, so they go
*before* the command name:

```bash
uv run imperial-doc-download --verbose --env-file ~/creds.env all
```

| Option | |
|---|---|
| `-v, --verbose` | debug logging |
| `--env-file` | read credentials from somewhere other than `./.env` |

In `all`, each pipeline has a switch: `--skip-labts`, `--skip-gitlab`,
`--skip-emarking`, `--skip-materials`. **Materials is the only one
skipped by default** - at ~1.75 GB it's an order of magnitude more than
everything else combined, so `--materials` opts in. Plus I am not sure if it would violate
acceptable use of the API to download all the materials, so I have left it out by default.

`-j/--concurrency` defaults to your machine's CPU count. The HTTP
pipelines still wait `--delay` after each request, so raising it raises
the request rate rather than removing the throttle. These are shared
teaching servers — if something looks strained, lower `-j` or raise
`--delay`.

### Recipes

```bash
# see what would happen, without doing any of it
uv run imperial-doc-download all --dry-run

# everything, including materials, somewhere specific
uv run imperial-doc-download all --materials -o ~/imperial-backup

# just this year's coursework, gently
uv run imperial-doc-download emarking --year 2526 -j 1 --delay 2

# retry failed clones without going near LabTS again
uv run imperial-doc-download gitlab

# LabTS with a page cache, so re-runs are instant
uv run imperial-doc-download labts --cache-dir ./cache

# rebuild the marks page after an update — no network
uv run imperial-doc-download emarking-marks

# materials only, keeping the zips
uv run imperial-doc-download materials --keep-zip
```

---

## How it behaves

### Re-runs

Every step records what it did and skips it next time. A file already on
disk isn't downloaded again; a repository already cloned is left alone; a
module already extracted costs no request. Settled answers — an artefact
that doesn't exist, a year you weren't enrolled in — aren't asked about
twice either.

So the normal way to recover from anything is simply to run it again.
`--force` is there for when you want a genuinely fresh copy.

### When something fails

`all` **keeps going.** One system being down is no reason to lose the
others. Failures are collected, listed at the end, and the exit code is
non-zero so a script notices. Re-running retries only what failed.

The individual commands stop at the first failure instead, because their
later steps consume the earlier ones' output.

### Safety

Scientia is not a read-only systems. Between them, the APIs this tool talks
to can submit coursework, edit feedback, write marks, and replace a
lecturer's teaching files. So:

- **GET only, by construction.** There is exactly one place in the
  codebase a request is issued and the HTTP verb is a literal. There is
  no `post()`/`put()`/`delete()` helper anywhere, and tests assert both.
  The one exception is the single login POST to LabTS.
- **Your data only.** The tool uses personal endpoints and your own
  enrolment. Staff routes — other students' submissions, marks lists,
  cohort data — are never called.
- **Polite by default.** A delay between requests, long timeouts, and
  retries with backoff, because these are shared teaching servers.
- **Credentials are never logged.** Not the password, not the
  `Authorization` header, not the body of an auth failure.

Downloaded archives are treated as hostile input: zip members that would
write outside their target are dropped, symlink entries are skipped, and
size and member counts are capped before anything is written.

### Logging

Every run logs to the terminal in colour and to a timestamped file under
`logs/`, so a long run's output survives your scrollback.

---

## Troubleshooting

**`401` / "credentials rejected", but the password is right.**
You probably `source`d your `.env`. See the warning in
[Setup](#credentials).

**"needs a shell-server key" / SSH failures.**
`IMPERIAL_DOC_SSH_KEY` must point at a private key that works against
`shell1-5.doc.ic.ac.uk`. Test it with
`ssh -i <key> <username>@shell1.doc.ic.ac.uk`. If the key has a
passphrase, load it into your agent first or set the matching
`…_PASSPHRASE` variable.

**Some repositories fail to clone.**
Old years are no longer on GitLab and need the gitolite fallback, which
requires both `IMPERIAL_DOC_SSH_KEY` and `IMPERIAL_USERNAME`. Without
them, those clones fail and the run says so.

**Materials says it has no modules to fetch.**
It needs the enrolment list. Run `emarking` (or `all`) first, or pass
`--year`.

**A run is slow, or a server seems unhappy.**
Lower `-j` and raise `--delay`. `-j 1 --delay 2` is about as gentle as it
gets.

**It stopped halfway through.**
Run it again. Nothing already downloaded is fetched twice.

---

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

CI runs ruff (lint + format check) and pytest on every push and PR. The
test suite is entirely offline — the APIs are faked with
`httpx.MockTransport` and SSH is stubbed, so nothing in it ever touches a
real Imperial server.

### Layout

```
src/imperial_doc_download/
├── cli.py              Typer app: one subcommand per system
├── config.py           settings and .env parsing
├── logging.py          coloured console + file logging
├── ssh.py              shared: throwaway ssh config, keys, agent
├── proxy.py            shared: ssh -D SOCKS tunnel into DoC
├── doc_api.py          shared: GET-only async client for the DoC APIs
├── naming.py           shared: pure, filesystem-safe names
├── pipeline/           Step, PipelineContext, and the runner
├── labts_fetch/        LabTS: the repository list
├── gitlab_fetch/       DoC GitLab and gitolite: the clones
├── emarking_fetch/     coursework, the marks record, the results page
├── materials_fetch/    teaching materials, and safe zip extraction
└── scientia_fetch/     placeholder
```

`Pipeline` runs an ordered list of `Step`s against one shared
`PipelineContext` (output directory, settings, and a scratch `state` dict
steps use to hand data to each other). Each system is a self-contained
module implementing one or more `Step`s, and gets its own CLI subcommand.
Whatever two pipelines end up sharing moves to the top level rather than
staying in whichever one needed it first.

### Design notes

Each pipeline has a document in [`docs/`](docs/) recording how the system
was reverse-engineered, what was measured against the live API, and why
the output is shaped the way it is — including the things that turned out
to be traps:

- [`labts-fetch-plan.md`](docs/labts-fetch-plan.md)
- [`gitlab-fetch-notes.md`](docs/gitlab-fetch-notes.md)
- [`emarking-fetch-plan.md`](docs/emarking-fetch-plan.md)
- [`emarking-marks-plan.md`](docs/emarking-marks-plan.md)
- [`materials-fetch-plan.md`](docs/materials-fetch-plan.md)

---

## Licence

See [LICENSE](LICENSE).
