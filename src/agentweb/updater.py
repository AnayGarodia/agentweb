"""Self-update: check for and install a newer AgentWeb from the official installer.

``agentweb upgrade`` re-runs the same one-line installer documented in the README
(``curl … | sh``) so an already-installed user upgrades with one command instead
of remembering the URL. Before executing it verifies the fetched installer against
its published ``install.sh.sha256``, matching what a careful user does by hand.

Everything network- or process-facing is injected (``opener``/``runner``) so the
logic is unit-testable without hitting GitHub or spawning a shell.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .sdk import AgentWebError

INSTALL_BASE_URL = "https://github.com/AnayGarodia/agentweb/raw/refs/heads/main"
INSTALL_SCRIPT_URL = f"{INSTALL_BASE_URL}/install.sh"
INSTALL_SCRIPT_SHA256_URL = f"{INSTALL_BASE_URL}/install.sh.sha256"

_VERSION_LINE = re.compile(rb'^VERSION="([^"]+)"', re.MULTILINE)

Opener = Callable[[str], bytes]
Runner = Callable[[list[str]], int]


def _default_opener(url: str) -> bytes:
    try:
        with urlopen(  # noqa: S310 - fixed https GitHub URLs only
            Request(url, headers={"User-Agent": "agentweb-upgrade"}), timeout=60
        ) as response:
            return bytes(response.read())
    except OSError as exc:
        raise AgentWebError(
            f"Could not reach the AgentWeb installer at {url}: {exc}",
            code="upgrade_unreachable",
        ) from exc


def _default_runner(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode  # noqa: S603


def _parse_version(script: bytes) -> str:
    match = _VERSION_LINE.search(script)
    if not match:
        raise AgentWebError(
            "Could not read the version from the downloaded installer.",
            code="upgrade_version_unreadable",
        )
    return match.group(1).decode("ascii")


def _is_newer(latest: str, installed: str) -> bool:
    def parts(value: str) -> tuple[int, ...]:
        return tuple(int(p) for p in re.findall(r"\d+", value))

    try:
        return parts(latest) > parts(installed)
    except ValueError:
        return latest != installed


def check_for_update(installed: str, *, opener: Opener = _default_opener) -> dict[str, Any]:
    """Report the latest published version without changing anything."""
    latest = _parse_version(opener(INSTALL_SCRIPT_URL))
    return {
        "installed": installed,
        "latest": latest,
        "update_available": _is_newer(latest, installed),
    }


def _verify_checksum(script: bytes, checksum_file: bytes) -> None:
    expected = checksum_file.decode("utf-8", "replace").split()
    if not expected:
        raise AgentWebError(
            "The installer checksum file was empty.", code="upgrade_checksum_missing"
        )
    actual = hashlib.sha256(script).hexdigest()
    if actual != expected[0]:
        raise AgentWebError(
            "Downloaded installer failed checksum verification "
            f"(expected {expected[0]}, got {actual}); aborting upgrade.",
            code="upgrade_checksum_mismatch",
        )


def run_upgrade(
    installed: str,
    *,
    opener: Opener = _default_opener,
    runner: Runner = _default_runner,
    check_only: bool = False,
) -> dict[str, Any]:
    """Fetch, checksum-verify, and run the official installer to self-update."""
    status = check_for_update(installed, opener=opener)
    if check_only or not status["update_available"]:
        return {**status, "upgraded": False}

    script = opener(INSTALL_SCRIPT_URL)
    _verify_checksum(script, opener(INSTALL_SCRIPT_SHA256_URL))

    with tempfile.TemporaryDirectory(prefix="agentweb-upgrade-") as tmp:
        script_path = Path(tmp) / "install.sh"
        script_path.write_bytes(script)
        code = runner(["sh", str(script_path)])
    if code != 0:
        raise AgentWebError(
            f"The AgentWeb installer exited with status {code}.",
            code="upgrade_failed",
        )
    return {
        **status,
        "installed": status["latest"],
        "previous": installed,
        "upgraded": True,
    }
