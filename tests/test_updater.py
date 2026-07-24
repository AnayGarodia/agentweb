from __future__ import annotations

import hashlib
import json

import pytest

from agentweb.cli import main as cli_main
from agentweb.sdk import AgentWebError
from agentweb.updater import (
    INSTALL_SCRIPT_SHA256_URL,
    INSTALL_SCRIPT_URL,
    check_for_update,
    run_upgrade,
)


def _script(version: str) -> bytes:
    return f'#!/bin/sh\nVERSION="{version}"\necho hi\n'.encode()


def _opener_for(version: str):
    script = _script(version)
    checksum = f"{hashlib.sha256(script).hexdigest()}  install.sh\n".encode()

    def opener(url: str) -> bytes:
        if url == INSTALL_SCRIPT_URL:
            return script
        if url == INSTALL_SCRIPT_SHA256_URL:
            return checksum
        raise AssertionError(f"unexpected url {url}")

    return opener


def test_check_for_update_reports_newer() -> None:
    status = check_for_update("0.19.3", opener=_opener_for("0.20.0"))
    assert status == {
        "installed": "0.19.3",
        "latest": "0.20.0",
        "update_available": True,
    }


def test_check_for_update_up_to_date() -> None:
    status = check_for_update("0.20.0", opener=_opener_for("0.20.0"))
    assert status["update_available"] is False


def test_run_upgrade_runs_installer_when_newer() -> None:
    calls: list[list[str]] = []

    result = run_upgrade(
        "0.19.3",
        opener=_opener_for("0.20.0"),
        runner=lambda cmd: calls.append(cmd) or 0,
    )
    assert result["upgraded"] is True
    assert result["installed"] == "0.20.0"
    assert result["previous"] == "0.19.3"
    assert len(calls) == 1 and calls[0][0] == "sh"


def test_run_upgrade_noop_when_current() -> None:
    def runner(cmd: list[str]) -> int:
        raise AssertionError("installer must not run when already current")

    result = run_upgrade("0.20.0", opener=_opener_for("0.20.0"), runner=runner)
    assert result["upgraded"] is False


def test_run_upgrade_rejects_checksum_mismatch() -> None:
    def opener(url: str) -> bytes:
        if url == INSTALL_SCRIPT_URL:
            return _script("0.20.0")
        return b"deadbeef  install.sh\n"

    with pytest.raises(AgentWebError) as excinfo:
        run_upgrade("0.19.3", opener=opener, runner=lambda cmd: 0)
    assert excinfo.value.code == "upgrade_checksum_mismatch"


def test_run_upgrade_raises_when_installer_fails() -> None:
    with pytest.raises(AgentWebError) as excinfo:
        run_upgrade(
            "0.19.3", opener=_opener_for("0.20.0"), runner=lambda cmd: 3
        )
    assert excinfo.value.code == "upgrade_failed"


def test_upgrade_check_cli(monkeypatch, capsys) -> None:
    import agentweb.updater as updater

    def fake_check(installed: str, **_: object) -> dict[str, object]:
        return check_for_update(installed, opener=_opener_for("99.0.0"))

    # The handler imports check_for_update from the module at call time.
    monkeypatch.setattr(updater, "check_for_update", fake_check, raising=True)
    assert cli_main(["upgrade", "--check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["update_available"] is True and out["latest"] == "99.0.0"
