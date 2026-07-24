"""Session lifecycle: expiry metadata capture, health reporting, and the
structured session_expired enrichment on authentication errors."""

from __future__ import annotations

import time
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace

from agentweb import connector
from agentweb.runtime import Runtime
from agentweb.sdk import AuthenticationRequired
from agentweb.storage import StatePaths, read_json


class _FakeSession:
    def __init__(self) -> None:
        self.cookies = CookieJar()
        self.saved = False

    def save_cookies(self) -> None:
        self.saved = True


def _fake_runtime(tmp_path: Path, manifest: dict) -> SimpleNamespace:
    session = _FakeSession()
    return SimpleNamespace(
        paths=StatePaths(tmp_path),
        profile="default",
        describe=lambda site: manifest,
        adapter=lambda site: SimpleNamespace(session=lambda: session),
        _session=session,
    )


def _cdp_cookie(name: str, expires: float | None) -> dict:
    return {
        "name": name,
        "value": "v",
        "domain": ".example.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "expires": expires,
    }


def test_import_cdp_cookies_records_session_expiry(tmp_path: Path) -> None:
    manifest = {
        "cookie_domain": ".example.com",
        "auth_cookie_names": ["session", "auth_token"],
    }
    runtime = _fake_runtime(tmp_path, manifest)
    soon = int(time.time()) + 3600
    later = int(time.time()) + 86400
    count = connector.import_cdp_cookies(
        runtime,
        "example",
        [
            _cdp_cookie("session", soon),
            _cdp_cookie("auth_token", later),
            _cdp_cookie("preferences", later),
            _cdp_cookie("transient", None),
            {**_cdp_cookie("other", later), "domain": ".unrelated.com"},
        ],
    )

    assert count == 4
    assert runtime._session.saved
    meta = read_json(
        connector.session_meta_path(runtime.paths, "example", "default"), None
    )
    assert meta["cookie_count"] == 4
    assert meta["auth_cookies_captured"] == ["auth_token", "session"]
    # The session dies with its earliest-expiring authenticating cookie.
    assert meta["session_expires_at_unix"] == soon


def test_session_health_reports_remaining_lifetime(tmp_path: Path) -> None:
    manifest = {"cookie_domain": ".example.com", "auth_cookie_names": ["session"]}
    runtime = _fake_runtime(tmp_path, manifest)
    connector.import_cdp_cookies(
        runtime, "example", [_cdp_cookie("session", int(time.time()) + 3600)]
    )

    health = connector.session_health(runtime.paths, "example", "default")
    assert health is not None
    assert health["expired"] is False
    assert 0 < health["expires_in_seconds"] <= 3600

    assert connector.session_health(runtime.paths, "missing", "default") is None


def test_authentication_error_enriched_with_reconnect_and_expiry(
    tmp_path: Path,
) -> None:
    manifest = {"cookie_domain": ".example.com", "auth_cookie_names": ["session"]}
    runtime = _fake_runtime(tmp_path, manifest)
    connector.import_cdp_cookies(
        runtime, "example", [_cdp_cookie("session", int(time.time()) - 60)]
    )

    exc = AuthenticationRequired("example requires a signed-in session")
    Runtime._enrich_authentication_error(runtime, exc, "example")

    payload = exc.as_dict()
    assert payload["details"]["reconnect_command"] == [
        "agentweb",
        "--profile",
        "default",
        "connect",
        "example",
    ]
    assert payload["details"]["session_expired"] is True
    assert "refresh the session" in payload["user_action"]


def test_adapter_declared_next_action_wins_over_reconnect_hint(
    tmp_path: Path,
) -> None:
    runtime = SimpleNamespace(paths=StatePaths(tmp_path), profile="default")

    exc = AuthenticationRequired(
        "This write requires an API token", next_action="github.configure_token"
    )
    Runtime._enrich_authentication_error(runtime, exc, "github")

    payload = exc.as_dict()
    # The adapter already named the fix; adding a second, different suggestion
    # would give agents conflicting guidance.
    assert "user_action" not in payload
    assert payload["next_action"] == "github.configure_token"
    assert payload["details"]["reconnect_command"][-2:] == ["connect", "github"]


def test_authentication_error_without_stored_session(tmp_path: Path) -> None:
    runtime = SimpleNamespace(paths=StatePaths(tmp_path), profile="default")

    exc = AuthenticationRequired("example requires a signed-in session")
    Runtime._enrich_authentication_error(runtime, exc, "example")

    payload = exc.as_dict()
    assert payload["details"]["reconnect_command"][-2:] == ["connect", "example"]
    assert "session_expired" not in payload["details"]
    assert payload["user_action"] == "Run `agentweb connect example` and sign in"
