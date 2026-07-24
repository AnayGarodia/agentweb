from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from agentweb.browser_readback import BrowserSession, browser_execute
from agentweb.cli import main as cli_main
from agentweb.oracle import build_response_oracle
from agentweb.runtime import Runtime
from agentweb.sdk import AgentWebError
from agentweb.storage import StatePaths


class _FakeSocket:
    def gettimeout(self) -> float:
        return 5.0

    def settimeout(self, value: float) -> None:
        self._timeout = value


class _FakeCDP:
    """Minimal stand-in for the CDP client used by BrowserSession."""

    def __init__(
        self,
        *,
        evaluate_value: dict[str, Any] | None = None,
        exception: dict[str, Any] | None = None,
        cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        self.socket = _FakeSocket()
        self._evaluate_value = evaluate_value or {}
        self._exception = exception
        self._cookies = cookies or []
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params))
        if method == "Network.getAllCookies":
            return {"cookies": self._cookies}
        if method == "Runtime.evaluate":
            if self._exception is not None:
                return {"exceptionDetails": self._exception}
            return {"result": {"value": self._evaluate_value}}
        return {}


def _session(tmp_path: Path, cdp: _FakeCDP, domains=("linkedin.com",)) -> BrowserSession:
    return BrowserSession(
        StatePaths(tmp_path),
        "linkedin",
        "default",
        client=cdp,
        allowed_domains=domains,
    )


def _ok_value(body: bytes, status: int = 200) -> dict[str, Any]:
    return {
        "status": status,
        "url": "https://www.linkedin.com/voyager/api/me",
        "headers": {"content-type": "application/json"},
        "body_b64": base64.b64encode(body).decode("ascii"),
    }


def _perform(session: BrowserSession, url: str, headers: dict[str, str] | None = None):
    return session._perform_request(
        method="GET",
        url=url,
        data=None,
        content_type=None,
        headers=headers,
        referer=None,
        impersonate="chrome146",
        allowed_redirect_domains=None,
        timeout_seconds=10.0,
    )


def test_perform_request_decodes_browser_response(tmp_path: Path) -> None:
    cdp = _FakeCDP(evaluate_value=_ok_value(b'{"ok":1}'))
    session = _session(tmp_path, cdp)
    resp = _perform(session, "https://www.linkedin.com/voyager/api/me")
    assert resp.status == 200
    assert resp.body == b'{"ok":1}'
    assert resp.transport == "cdp_browser"


def test_perform_request_strips_managed_headers_keeps_adapter_headers(
    tmp_path: Path,
) -> None:
    cdp = _FakeCDP(evaluate_value=_ok_value(b"{}"))
    session = _session(tmp_path, cdp)
    _perform(
        session,
        "https://www.linkedin.com/voyager/api/me",
        headers={
            "Accept": "application/json",
            "Csrf-Token": "tok-123",
            "Cookie": "li_at=SECRET",
            "User-Agent": "should-not-forward",
        },
    )
    _method, params = cdp.calls[-1]
    expression = params["expression"]
    assert "Csrf-Token" in expression
    assert "Accept" in expression
    assert "SECRET" not in expression
    assert "should-not-forward" not in expression


def test_perform_request_rejects_non_https(tmp_path: Path) -> None:
    session = _session(tmp_path, _FakeCDP(evaluate_value=_ok_value(b"{}")))
    with pytest.raises(AgentWebError, match="non-HTTPS"):
        _perform(session, "http://www.linkedin.com/voyager/api/me")


def test_perform_request_enforces_host_allowlist(tmp_path: Path) -> None:
    session = _session(tmp_path, _FakeCDP(evaluate_value=_ok_value(b"{}")))
    with pytest.raises(AgentWebError, match="allowlist"):
        _perform(session, "https://evil.example.com/voyager/api/me")


def test_perform_request_surfaces_fetch_error(tmp_path: Path) -> None:
    cdp = _FakeCDP(evaluate_value={"error": "TypeError: Failed to fetch"})
    session = _session(tmp_path, cdp)
    with pytest.raises(AgentWebError, match="failed"):
        _perform(session, "https://www.linkedin.com/voyager/api/me")


def test_perform_request_surfaces_evaluate_exception(tmp_path: Path) -> None:
    cdp = _FakeCDP(exception={"exception": {"description": "boom"}})
    session = _session(tmp_path, cdp)
    with pytest.raises(AgentWebError, match="boom"):
        _perform(session, "https://www.linkedin.com/voyager/api/me")


def test_sync_from_browser_imports_only_allowlisted_cookies(tmp_path: Path) -> None:
    cdp = _FakeCDP(
        cookies=[
            {
                "name": "li_at",
                "value": "x",
                "domain": ".linkedin.com",
                "path": "/",
                "expires": 9999999999,
                "secure": True,
            },
            {
                "name": "tracker",
                "value": "y",
                "domain": ".ads.example.com",
                "path": "/",
            },
        ]
    )
    session = _session(tmp_path, cdp)
    imported = session.sync_from_browser()
    names = {cookie.name for cookie in session.cookies}
    assert imported == 1
    assert "li_at" in names
    assert "tracker" not in names


def test_browser_execute_requires_saved_session(
    tmp_path: Path, monkeypatch
) -> None:
    # A resolvable site with a base_url but no captured browser profile must fail
    # with browser_session_missing (and never launch Chrome).
    class _Resolved:
        site = "linkedin"

    monkeypatch.setattr(Runtime, "resolve", lambda self, target: _Resolved())
    monkeypatch.setattr(
        Runtime,
        "describe",
        lambda self, site: {
            "base_url": "https://www.linkedin.com",
            "allowed_domains": ["linkedin.com"],
        },
    )
    runtime = Runtime(StatePaths(tmp_path))
    with pytest.raises(AgentWebError) as excinfo:
        browser_execute(runtime, "linkedin.com", "account_status", {})
    assert excinfo.value.code == "browser_session_missing"


def test_session_override_defaults_none_and_binds_by_site_name(tmp_path: Path) -> None:
    runtime = Runtime(StatePaths(tmp_path))
    assert runtime._session_override is None
    sentinel = object()
    seen: list[str] = []

    def factory(site: str) -> Any:
        seen.append(site)
        return sentinel

    runtime._session_override = factory
    adapter = runtime.adapter("npm")
    assert adapter._session is sentinel
    assert seen == ["npm"]


def _envelope(data: Any, *, ok: bool = True) -> dict[str, Any]:
    return {"ok": ok, "operation": "linkedin.account_status", "data": data}


def _fake_runtime(monkeypatch, *, writes: set[str]) -> None:
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
                "account_status": {
                    "risk": {
                        "level": "write" if "account_status" in writes else "read",
                        "confirmation": "always"
                        if "account_status" in writes
                        else "never",
                    }
                }
            }
        }

    monkeypatch.setattr(Runtime, "resolve", fake_resolve)
    monkeypatch.setattr(Runtime, "resolve_action", fake_resolve_action)
    monkeypatch.setattr(Runtime, "describe", fake_describe)


def test_capture_oracle_via_browser_marks_execution(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    _fake_runtime(monkeypatch, writes=set())
    import agentweb.browser_readback as br

    seen: dict[str, Any] = {}

    def fake_browser_execute(runtime, site, operation, arguments):
        seen["site"] = site
        seen["operation"] = operation
        return _envelope({"signed_in": True, "account": {"name": "x"}})

    monkeypatch.setattr(br, "browser_execute", fake_browser_execute)
    out = tmp_path / "linkedin.oracle.json"
    assert (
        cli_main(
            [
                "capture-oracle",
                "linkedin.com",
                "account_status",
                "--via-browser",
                "--assert",
                "$.data.signed_in",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    oracle = json.loads(out.read_text())
    assert oracle["execution"] == "browser_assisted"
    assert seen == {"site": "linkedin.com", "operation": "account_status"}


def test_verify_capture_auto_uses_browser_and_reports_evidence(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    oracle = build_response_oracle(
        "linkedin",
        "account_status",
        {},
        _envelope({"signed_in": True, "account": {"name": "x"}}),
        assert_paths=["$.data.signed_in", "$.data.account.name"],
    )
    oracle["execution"] = "browser_assisted"
    path = tmp_path / "o.json"
    path.write_text(json.dumps(oracle))

    import agentweb.browser_readback as br

    monkeypatch.setattr(
        br,
        "browser_execute",
        lambda runtime, site, operation, arguments: _envelope(
            {"signed_in": True, "account": {"name": "y"}}
        ),
    )
    assert cli_main(["verify-capture", str(path), "--strict"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "capture_verified"
    assert result["execution"] == "browser_assisted"
    assert result["evidence"] == "browser_capture_verified"


def test_verify_capture_browser_drift_exits_nonzero(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    oracle = build_response_oracle(
        "linkedin",
        "account_status",
        {},
        _envelope({"signed_in": True, "account": {"name": "x"}}),
        assert_paths=["$.data.account.name"],
    )
    oracle["execution"] = "browser_assisted"
    path = tmp_path / "o.json"
    path.write_text(json.dumps(oracle))

    import agentweb.browser_readback as br

    monkeypatch.setattr(
        br,
        "browser_execute",
        lambda runtime, site, operation, arguments: _envelope({"signed_in": True}),
    )
    assert cli_main(["verify-capture", str(path), "--strict"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "drift"
