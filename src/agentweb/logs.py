"""Central logging for AgentWeb.

The library stays silent by default (a :class:`logging.NullHandler` absorbs
records) so importing AgentWeb never spams a host application. Entry points call
:func:`configure_logging`, which promotes the logger to stderr at DEBUG level
when ``AGENTWEB_DEBUG`` is set to a truthy value. This lets otherwise-swallowed
best-effort failures be inspected without changing default behaviour.
"""

from __future__ import annotations

import logging
import os

LOGGER_NAME = "agentweb"
_DEBUG_HANDLER_NAME = "agentweb-debug-stderr"

logger = logging.getLogger(LOGGER_NAME)
logger.addHandler(logging.NullHandler())

_TRUTHY = {"1", "true", "yes", "on", "debug"}


def debug_enabled() -> bool:
    """Return whether AGENTWEB_DEBUG requests verbose diagnostics."""
    return os.environ.get("AGENTWEB_DEBUG", "").strip().lower() in _TRUTHY


def configure_logging() -> None:
    """Route AgentWeb debug logs to stderr when AGENTWEB_DEBUG is truthy.

    Idempotent: repeated calls never attach duplicate handlers.
    """
    if not debug_enabled():
        return
    if any(getattr(h, "name", None) == _DEBUG_HANDLER_NAME for h in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.name = _DEBUG_HANDLER_NAME
    handler.setFormatter(logging.Formatter("agentweb %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
