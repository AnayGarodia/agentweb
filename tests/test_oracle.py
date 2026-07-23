from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentweb.cli import main as cli_main
from agentweb.oracle import (
    build_response_oracle,
    resolve_json_path,
    verify_response_oracle,
)
from agentweb.runtime import Runtime
from agentweb.sdk import AgentWebError


def _envelope(data: Any, *, ok: bool = True) -> dict[str, Any]:
    return {"ok": ok, "operation": "npm.get_version", "data": data}


def test_resolve_json_path_bracket_and_dotted_index() -> None:
    payload = {"data": {"items": [{"title": "A"}]}}
    assert resolve_json_path(payload, "$.data.items[0].title") == (True, "A")
    assert resolve_json_path(payload, "$.data.items.0.title") == (True, "A")
    assert resolve_json_path(payload, "$.data.items[3].title") == (False, None)
    assert resolve_json_path(payload, "$.data.missing") == (False, None)


def test_build_oracle_redacts_input_and_records_shape() -> None:
    oracle = build_response_oracle(
        "npm",
        "get_version",
        {"package": "react", "token": "secret-value"},
        _envelope({"name": "react", "version": "1.2.3"}),
        assert_paths=["$.data.name"],
    )
    assert oracle["kind"] == "agentweb_response_oracle"
    assert oracle["input"]["token"] == "[redacted]"
    assert oracle["input"]["package"] == "react"
    paths = {field["path"] for field in oracle["observed"]["data_shape"]}
    assert {"name", "version"} <= paths
    assert oracle["observed"]["assertions"][0]["type"] == "str"


def test_build_oracle_rejects_failed_execution_and_bad_assertion() -> None:
    with pytest.raises(AgentWebError):
        build_response_oracle("npm", "x", {}, _envelope({}, ok=False))
    with pytest.raises(AgentWebError):
        build_response_oracle(
            "npm", "x", {}, _envelope({"name": "react"}), assert_paths=["$.data.gone"]
        )


def test_verify_oracle_matches_and_detects_shape_drift() -> None:
    oracle = build_response_oracle(
        "npm",
        "get_version",
        {"package": "react"},
        _envelope({"name": "react", "version": "1.2.3"}),
        assert_paths=["$.data.name"],
    )
    ok = verify_response_oracle(oracle, _envelope({"name": "left-pad", "version": "9"}))
    assert ok["status"] == "capture_verified"
    assert ok["passed"] is True

    lost = verify_response_oracle(oracle, _envelope({"name": "react"}))
    assert lost["status"] == "drift"
    assert any("response shape lost" in m for m in lost["mismatches"])

    failed = verify_response_oracle(
        oracle, _envelope({"name": "react", "version": "9"}, ok=False)
    )
    assert failed["status"] == "drift"
    assert "operation no longer succeeds" in failed["mismatches"]


def test_verify_oracle_detects_assertion_type_change() -> None:
    oracle = build_response_oracle(
        "npm", "x", {}, _envelope({"count": 5}), assert_paths=["$.data.count"]
    )
    drifted = verify_response_oracle(oracle, _envelope({"count": "five"}))
    assert drifted["status"] == "drift"
    assert any("type changed" in m for m in drifted["mismatches"])


def test_verify_oracle_tolerates_empty_captured_collection() -> None:
    oracle = build_response_oracle(
        "wikipedia", "search", {}, _envelope({"items": [{"title": "A"}]})
    )
    ok = verify_response_oracle(oracle, _envelope({"items": []}))
    assert ok["status"] == "capture_verified"


def test_verify_oracle_structural_only_without_envelope() -> None:
    oracle = build_response_oracle("npm", "x", {}, _envelope({"a": 1}))
    result = verify_response_oracle(oracle)
    assert result["status"] == "structural_only"
    assert result["passed"] is True

    result = verify_response_oracle({"kind": "wrong"})
    assert result["passed"] is False
    assert "unsupported response oracle" in result["errors"]


def _fake_runtime(
    monkeypatch,
    envelopes: dict[str, dict[str, Any]],
    *,
    writes: set[str],
) -> None:
    class _Resolved:
        def __init__(self, site: str) -> None:
            self.site = site

    def fake_resolve(self: Runtime, target: str) -> _Resolved:
        return _Resolved(target.split(".")[0])

    def fake_resolve_action(self: Runtime, site: str, action: str) -> str:
        return action

    def fake_describe(self: Runtime, site: str) -> dict[str, Any]:
        return {
            "commands": {
                action.split(".")[1]: {
                    "risk": {
                        "level": "write" if action.split(".")[1] in writes else "read",
                        "confirmation": "always"
                        if action.split(".")[1] in writes
                        else "never",
                    }
                }
                for action in envelopes
            }
        }

    def fake_execute(
        self: Runtime,
        target: str,
        action: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        site = target.split(".")[0]
        return envelopes[f"{site}.{action}"]

    monkeypatch.setattr(Runtime, "resolve", fake_resolve)
    monkeypatch.setattr(Runtime, "resolve_action", fake_resolve_action)
    monkeypatch.setattr(Runtime, "describe", fake_describe)
    monkeypatch.setattr(Runtime, "execute", fake_execute)


def test_capture_oracle_and_verify_capture_cli_roundtrip(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    _fake_runtime(
        monkeypatch,
        {"npm.get_version": _envelope({"name": "react", "version": "1.2.3"})},
        writes=set(),
    )
    out = tmp_path / "npm.oracle.json"
    assert (
        cli_main(
            [
                "capture-oracle",
                "npm.com",
                "get_version",
                "--input",
                json.dumps({"package": "react"}),
                "--assert",
                "$.data.name",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    captured = json.loads(capsys.readouterr().out)
    assert captured["written_to"] == str(out)
    assert out.is_file()

    assert cli_main(["verify-capture", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "capture_verified"


def test_verify_capture_cli_strict_exit_on_drift(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    oracle = build_response_oracle(
        "npm",
        "get_version",
        {"package": "react"},
        _envelope({"name": "react", "version": "9"}),
    )
    path = tmp_path / "o.json"
    path.write_text(json.dumps(oracle))
    _fake_runtime(
        monkeypatch, {"npm.get_version": _envelope({"name": "react"})}, writes=set()
    )
    assert cli_main(["verify-capture", str(path), "--strict"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "drift"


def test_capture_oracle_cli_refuses_mutating_without_confirm(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    _fake_runtime(
        monkeypatch,
        {"wikipedia.edit": _envelope({"result": {"ok": True}})},
        writes={"edit"},
    )
    assert (
        cli_main(
            ["capture-oracle", "wikipedia.org", "edit", "--input", json.dumps({"title": "X"})]
        )
        == 2
    )
    err = json.loads(capsys.readouterr().err)
    assert err["error"] == "oracle_capture_mutating"
