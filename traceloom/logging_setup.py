"""Make Traceloom's own log lines visible when the host process configured no logging.

uvicorn only configures its own loggers, so without this the console mail fallback (reset and
invite links in dev) and the ingestion logs would be dropped at the default WARNING level.
"""

from __future__ import annotations

import logging
import sys

HANDLER_NAME = "traceloom-console"


def ensure_console_logging(level: int = logging.INFO) -> None:
    """Send `traceloom.*` records at INFO and above to stderr, once.

    Does nothing when the application or test runner already set up root handlers, so it never
    duplicates lines or overrides a deliberate logging configuration.
    """
    logger = logging.getLogger("traceloom")
    if logging.getLogger().handlers or any(h.get_name() == HANDLER_NAME for h in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
