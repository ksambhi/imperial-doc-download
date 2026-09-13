import logging
import re
from io import StringIO

from rich.console import Console

from imperial_doc_download.logging import PrettyHandler


def _emit(logger_name: str, message: str, *, color: bool) -> str:
    buffer = StringIO()
    console = Console(file=buffer, force_terminal=True, no_color=not color, width=200)
    handler = PrettyHandler(console=console)

    logger = logging.getLogger(logger_name)
    record = logger.makeRecord(logger.name, logging.INFO, __file__, 1, message, (), None)
    handler.emit(record)

    return buffer.getvalue()


def test_format_is_timestamp_module_level_message() -> None:
    output = _emit("imperial_doc_download.labts_fetch", "hello world", color=False)

    match = re.match(
        r"^\[\d{2}:\d{2}:\d{2}\] imperial_doc_download\.labts_fetch INFO hello world$",
        output.strip(),
    )
    assert match, output


def test_output_is_coloured() -> None:
    output = _emit("imperial_doc_download.gitlab_fetch", "hi", color=True)
    assert "\x1b[" in output
