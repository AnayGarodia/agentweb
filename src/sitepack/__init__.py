"""Compatibility namespace for the former Sitepack package name.

AgentWeb is the canonical package. Existing integrations can keep importing
``sitepack.*`` while they migrate; every module resolves to AgentWeb.
"""

from __future__ import annotations

import importlib
import sys

from agentweb import __version__

_MODULES = (
    "auth",
    "capture",
    "cli",
    "connector",
    "mcp",
    "registry",
    "runtime",
    "scaffold",
    "sdk",
    "storage",
    "web_runtime",
)

for _name in _MODULES:
    sys.modules[f"sitepack.{_name}"] = importlib.import_module(f"agentweb.{_name}")

__all__ = ["__version__"]
