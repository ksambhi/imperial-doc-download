"""Logging setup for the CLI.

Two sinks, configured once from the Typer app callback:

- the terminal, coloured, formatted as `[HH:MM:SS] module LEVEL message`
- a plain-text file under `logs/`, named after when the run started, so a
  full run's output survives even if the terminal scrollback doesn't.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.text import Text

# "Usual" logging colours; not rich's own defaults, which don't colour INFO.
_LEVEL_STYLES: dict[int, str] = {
    logging.DEBUG: "cyan",
    logging.INFO: "green",
    logging.WARNING: "yellow",
    logging.ERROR: "red",
    logging.CRITICAL: "bold red",
}

_TIMESTAMP_STYLE = "bright_black"
_MODULE_STYLE = "magenta"

_FILE_LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


class PrettyHandler(logging.Handler):
    """Renders a record as `[HH:MM:SS] module LEVEL message`.

    The timestamp is grey, `module` (the logger's `__name__`) is magenta,
    the level is coloured by severity, and the message is left in the
    terminal's normal colour.
    """

    def __init__(self, console: Console | None = None) -> None:
        super().__init__()
        self.console = console or Console(stderr=True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            level_style = _LEVEL_STYLES.get(record.levelno, "white")

            line = Text()
            line.append(f"[{timestamp}]", style=_TIMESTAMP_STYLE)
            line.append(" ")
            line.append(record.name, style=_MODULE_STYLE)
            line.append(" ")
            line.append(record.levelname, style=level_style)
            line.append(" ")
            line.append(message)

            self.console.print(line, highlight=False, soft_wrap=True)
        except Exception:
            self.handleError(record)


def setup_logging(verbose: bool = False, log_dir: Path | str = "logs") -> Path:
    """Configure root logging: coloured console output, plus a log file.

    Safe to call more than once (e.g. once per Typer invocation in tests) —
    it replaces its own previous handlers rather than stacking them, and
    leaves any handlers it doesn't own (e.g. pytest's caplog) alone.

    Returns the path of the log file for this run.
    """
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        if isinstance(handler, (PrettyHandler, logging.FileHandler)):
            root.removeHandler(handler)
            handler.close()

    console_handler = PrettyHandler()
    console_handler.setLevel(level)
    root.addHandler(console_handler)

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    # Microsecond precision avoids two runs started in the same second
    # clobbering/appending to each other's log file.
    log_file = log_dir / f"{datetime.now():%Y%m%d-%H%M%S-%f}.log"

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(_FILE_LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(file_handler)

    return log_file
