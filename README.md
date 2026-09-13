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
│       ├── pipeline/           # generic pipeline machinery
│       │   ├── base.py         #   Step, PipelineContext
│       │   └── runner.py       #   Pipeline: runs an ordered list of Steps
│       ├── labts_fetch/        # LabTS: the repo list  (implemented)
│       │   ├── client.py       #   GET-only HTTP client, login + retries
│       │   ├── parsing.py      #   pure HTML parsing, no I/O
│       │   ├── models.py       #   Exercise / Milestone
│       │   └── step.py         #   the pipeline Step
│       ├── gitlab_fetch/       # placeholder: DoC GitLab
│       └── scientia_fetch/     # placeholder: Scientia (timetable/exams)
└── tests/
    └── fixtures/labts/         # anonymised real markup, for offline tests
```

The pipeline is generic on purpose: `Pipeline` runs a list of `Step`s
against one shared `PipelineContext` (output directory, settings, and a
scratch `state` dict steps can use to pass data along, e.g. an
authenticated session from a login step to the steps that use it).

Each Imperial system to back up is its own self-contained module —
`labts_fetch`, `gitlab_fetch`, `scientia_fetch`, and so on — each
implementing one `Step` subclass. `labts_fetch` is implemented;
the rest are still placeholders. As each gets built out, its `Step` gets
added to the `steps` list in `cli.py`'s `run` command to compose it into
the pipeline. (Module names use underscores, not hyphens — `-` isn't valid
in a Python import name.)

### `labts_fetch`

Logs in to LabTS, walks every academic year, and writes the list of GitLab
repositories behind every exercise to `<output_dir>/labts-list.json` —
clone URLs (ssh + https), the milestones, and the submitted revision for
each milestone. Cloning those repos is a later, separate step.

Requires `IMPERIAL_USERNAME` and `IMPERIAL_PASSWORD` in the environment.

LabTS is a slow, shared teaching server, so the client is strictly
sequential, waits between requests, uses long timeouts and retries with
backoff. It is also **GET-only by construction** — LabTS pages carry forms
that submit coursework late or queue jobs on the shared test-VM fleet, so
there is deliberately no `post()` helper anywhere except the login itself.
A full run is roughly 4–6 minutes.

See `docs/labts-fetch-plan.md` for how the site was reverse-engineered and
why the output is shaped the way it is.

## Usage

```bash
uv sync

uv run imperial-doc-download --help
uv run imperial-doc-download run --output-dir ./imperial-data --dry-run

export IMPERIAL_USERNAME=abc123
export IMPERIAL_PASSWORD=...        # or you'll be told what's missing up front
uv run imperial-doc-download run --output-dir ./imperial-data
```

`--dry-run` lists the steps that would run without downloading anything.
`-v/--verbose` enables debug logging.

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
