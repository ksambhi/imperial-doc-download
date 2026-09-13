# imperial-doc-download

Download my data from Imperial DoC systems before my account gets nuked at graduation.

A `uv`-managed, Typer-based CLI. Python 3.12.

## Project layout

```
imperial-doc-download/
├── pyproject.toml
├── src/
│   └── imperial_doc_download/
│       ├── cli.py            # Typer app + `run` command
│       ├── config.py         # Settings, loaded from env vars
│       ├── logging.py        # logging setup
│       ├── pipeline/         # generic pipeline machinery
│       │   ├── base.py       #   Step, PipelineContext
│       │   └── runner.py     #   Pipeline: runs an ordered list of Steps
│       └── sources/          # one module per Imperial system, each
│                              # implementing Step subclasses (empty for now)
└── tests/
```

The pipeline is deliberately generic: `Pipeline` runs a list of `Step`s
against one shared `PipelineContext` (output directory, settings, and a
scratch `state` dict steps can use to pass data along, e.g. an
authenticated session from a login step to the steps that use it).
Each Imperial system to back up (Labs, GitLab, Panopto, personal web
space, ...) will get its own module under `sources/` with one or more
`Step` implementations, registered in `cli.py`'s `run` command.

## Usage

```bash
uv sync

uv run imperial-doc-download --help
uv run imperial-doc-download run --output-dir ./imperial-data --dry-run
```

`--dry-run` lists the steps that would run without downloading anything.
`-v/--verbose` enables debug logging.

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```
