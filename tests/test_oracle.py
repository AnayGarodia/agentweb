from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentweb.cli import main as cli_main
from agentweb.oracle import (
    build_response_oracle,
    classify_oracle_replay,
    discover_oracles,
    oracle_age_days,
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


def _write_oracle(directory: Path, name: str, **overrides: Any) -> Path:
    oracle = build_response_oracle(
        "npm",
        "get_version",
        {"package": "react", "version": "18.2.0"},
        _envelope({"name": "react", "version": "18.2.0"}),
        assert_paths=["$.data.name"],
    )
    oracle.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(oracle))
    return path


def test_classify_oracle_replay_modes() -> None:
    assert classify_oracle_replay({}, via_browser=False) == "browserless"
    assert classify_oracle_replay({"mutating": True}, via_browser=True) == "mutating"
    browser = {"execution": "browser_assisted"}
    assert classify_oracle_replay(browser, via_browser=False) == "browser_required"
    assert classify_oracle_replay(browser, via_browser=True) == "browser"


def test_oracle_age_days_handles_missing_timestamp() -> None:
    assert oracle_age_days({}) is None
    assert oracle_age_days({"captured_at_unix": 0}, now=86400) == 1


def test_discover_oracles_recursive(tmp_path: Path) -> None:
    _write_oracle(tmp_path, "a.oracle.json")
    _write_oracle(tmp_path / "sub", "b.oracle.json")
    (tmp_path / "not-an-oracle.json").write_text("{}")
    found = discover_oracles(tmp_path)
    assert [p.name for p in found] == ["a.oracle.json", "b.oracle.json"]


def test_verify_oracles_directory_capture_verified(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    _write_oracle(tmp_path, "npm.oracle.json")
    _fake_runtime(
        monkeypatch,
        {"npm.get_version": _envelope({"name": "react", "version": "18.2.0"})},
        writes=set(),
    )
    assert cli_main(["verify-oracles", "--dir", str(tmp_path), "--strict"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["healthy"] is True
    assert result["verified"] == 1
    assert result["records"][0]["status"] == "capture_verified"


def test_verify_oracles_strict_exit_on_drift(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    _write_oracle(tmp_path, "npm.oracle.json")
    _fake_runtime(
        monkeypatch,
        {"npm.get_version": _envelope({"version": "18.2.0"})},  # name dropped
        writes=set(),
    )
    assert cli_main(["verify-oracles", "--dir", str(tmp_path), "--strict"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["healthy"] is False
    assert result["drift"] and result["drift"][0]["status"] == "drift"


def test_verify_oracles_offline_makes_no_requests(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    _write_oracle(tmp_path, "npm.oracle.json")

    def boom(self: Runtime, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("offline mode must not execute")

    monkeypatch.setattr(Runtime, "execute", boom)
    assert cli_main(["verify-oracles", "--dir", str(tmp_path), "--offline"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["records"][0]["status"] == "structural_only"


def test_verify_oracles_skips_mutating_and_browser(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    _write_oracle(tmp_path, "mut.oracle.json", mutating=True)
    _write_oracle(tmp_path, "browser.oracle.json", execution="browser_assisted")

    def boom(self: Runtime, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("skipped oracles must not execute")

    monkeypatch.setattr(Runtime, "execute", boom)
    assert cli_main(["verify-oracles", "--dir", str(tmp_path), "--strict"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["skipped"] == 2
    assert {r["status"] for r in result["records"]} == {"skipped"}


def test_verify_oracles_inconclusive_on_transient_error(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    _write_oracle(tmp_path, "npm.oracle.json")

    def flaky(self: Runtime, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AgentWebError("network unreachable")

    monkeypatch.setattr(Runtime, "execute", flaky)
    # A transient error is advisory: inconclusive, still healthy, exit 0.
    assert cli_main(["verify-oracles", "--dir", str(tmp_path), "--strict"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["inconclusive"] == 1
    assert result["healthy"] is True


def test_verify_oracles_missing_directory_errors(tmp_path: Path, capsys) -> None:
    assert cli_main(["verify-oracles", "--dir", str(tmp_path / "nope")]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "oracle_dir_missing"
