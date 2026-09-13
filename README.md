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
│       ├── labts_fetch/        # placeholder: LabTS (coursework/tests)
│       ├── gitlab_fetch/       # placeholder: DoC GitLab
│       └── scientia_fetch/     # placeholder: Scientia (timetable/exams)
└── tests/
```

The pipeline is generic on purpose: `Pipeline` runs a list of `Step`s
against one shared `PipelineContext` (output directory, settings, and a
scratch `state` dict steps can use to pass data along, e.g. an
authenticated session from a login step to the steps that use it).

Each Imperial system to back up is its own self-contained module —
`labts_fetch`, `gitlab_fetch`, `scientia_fetch`, and so on — each
implementing one `Step` subclass. Right now they're all placeholders that
raise `NotImplementedError`; as each gets built out, its `Step` gets added
to the `steps` list in `cli.py`'s `run` command to compose it into the
pipeline. (Module names use underscores, not hyphens — `-` isn't valid in
a Python import name.)

## Usage

```bash
uv sync

uv run imperial-doc-download --help
uv run imperial-doc-download run --output-dir ./imperial-data --dry-run
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
