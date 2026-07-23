from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable
from http.cookiejar import Cookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import websocket

from .auth import (
    ACTIVE_AUTH_STATES,
    AuthAttempt,
    AuthAttemptStore,
    AuthState,
    HumanCheckpoint,
    process_is_alive,
    status_is_connected,
    terminate_attempt,
)
from .logs import logger
from .runtime import Runtime
from .sdk import AgentWebError, ConfigurationRequired
from .storage import atomic_write, read_json, write_json

Progress = Callable[[str], None]
CDP_CONNECTION_ERRORS = (
    ConnectionResetError,
    OSError,
    websocket.WebSocketConnectionClosedException,
    websocket.WebSocketTimeoutException,
)


def stderr_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def connection_verification_fields(
    status: dict[str, Any] | None,
    *,
    verified_connected: bool,
    verification_error: str | None,
    authenticated_cookie_captured: bool,
) -> dict[str, str | None]:
    if verified_connected:
        account_verification = "verified"
        direct_authentication = "verified"
    elif status is not None:
        account_verification = "not_authenticated"
        direct_authentication = "rejected"
    elif verification_error:
        account_verification = "deferred"
        direct_authentication = "not_checked"
    else:
        account_verification = "not_available"
        direct_authentication = "not_checked"
    warning = status.get("warning") if status else None
    if not warning and status is not None and not verified_connected:
        warning = (
            "The browser cookies were saved, but the site's direct account check says "
            "this profile is not signed in. No authenticated operation will be reported as connected."
        )
    elif not warning and verification_error and authenticated_cookie_captured:
        warning = (
            "The website session was captured, but its account-status check was temporarily "
            "unavailable. Resume the original operation; AgentWeb will use the saved session "
            "and report if sign-in is still required."
        )
    return {
        "account_verification": account_verification,
        "direct_authentication": direct_authentication,
        "warning": warning,
    }


def chrome_executable() -> str:
    candidates = [
        os.environ.get("AGENTWEB_CHROME") or os.environ.get("SITEPACK_CHROME"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("agentweb-chrome") or shutil.which("sitepack-chrome"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise AgentWebError(
        "Chrome or Chromium was not found. Set AGENTWEB_CHROME to its executable path."
    )


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_debugger(
    port: int,
    timeout: float = 15,
    process: subprocess.Popen[Any] | None = None,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise AgentWebError(
                f"Chrome exited before its debugging connection started (exit {process.returncode})",
                code="browser_launch_failed",
                retryable=True,
            )
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                targets = json.load(response)
            if targets:
                return targets
        except (URLError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.2)
    raise AgentWebError(
        f"Chrome debugging connection did not start: {last_error}",
        code="browser_debugger_unavailable",
        retryable=True,
        next_action="retry agentweb connect after closing stale AgentWeb Chrome windows",
        details={"port": port, "timeout_seconds": timeout},
    )


class CDP:
    def __init__(self, websocket_url: str) -> None:
        self.socket = websocket.create_connection(
            websocket_url,
            timeout=5,
            origin="http://127.0.0.1",
            suppress_origin=True,
        )
        self.counter = 0
        self.events: list[dict[str, Any]] = []

    def close(self) -> None:
        self.socket.close()

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.counter += 1
        request_id = self.counter
        self.socket.send(
            json.dumps(
                {"id": request_id, "method": method, "params": params or {}},
                separators=(",", ":"),
            )
        )
        while True:
            message = json.loads(self.socket.recv())
            if message.get("id") != request_id:
                if message.get("method"):
                    self.events.append(message)
                continue
            if "error" in message:
                raise AgentWebError(
                    f"Chrome rejected {method}: {message['error'].get('message', message['error'])}"
                )
            return message.get("result")

    def clear_events(self) -> None:
        self.events.clear()

    def drain_events(self, timeout: float = 0.25) -> list[dict[str, Any]]:
        previous_timeout = self.socket.gettimeout()
        deadline = time.monotonic() + max(timeout, 0)
        try:
            while time.monotonic() < deadline:
                self.socket.settimeout(max(deadline - time.monotonic(), 0.001))
                try:
                    message = json.loads(self.socket.recv())
                except websocket.WebSocketTimeoutException:
                    break
                if message.get("method"):
                    self.events.append(message)
        finally:
            self.socket.settimeout(previous_timeout)
        return list(self.events)


def page_target(targets: list[dict[str, Any]], domain: str) -> dict[str, Any]:
    for target in targets:
        if target.get("type") == "page" and domain.lstrip(".") in target.get("url", ""):
            return target
    for target in targets:
        if target.get("type") == "page":
            return target
    raise AgentWebError("Chrome did not expose a page target")


def open_debugger_client(
    port: int,
    domain: str,
    timeout: float = 15,
    process: subprocess.Popen[Any] | None = None,
) -> CDP:
    targets = wait_for_debugger(port, timeout=timeout, process=process)
    target = page_target(targets, domain)
    client = CDP(target["webSocketDebuggerUrl"])
    try:
        client.call("Network.enable")
    except Exception:
        try:
            client.close()
        except Exception:
            logger.debug("Failed to close CDP client during cleanup", exc_info=True)
        raise
    return client


def browser_auth_snapshot(
    client: CDP, check: dict[str, Any] | None = None
) -> dict[str, Any]:
    check = check or {}
    selector = json.dumps(str(check.get("account_selector") or ""))
    entry_selector = json.dumps(str(check.get("entry_selector") or ""))
    signed_out = json.dumps(
        [str(value).strip().lower() for value in check.get("signed_out_labels") or []]
    )
    success_selector = json.dumps(str(check.get("success_selector") or ""))
    success_url_contains = json.dumps(
        [str(value) for value in check.get("success_url_contains") or []]
    )
    expression = f"""
    (() => {{
      const selector = {selector};
      const entrySelector = {entry_selector};
      const node = selector ? document.querySelector(selector) : null;
      const label = node ? (node.innerText || node.textContent || '').trim().replace(/\\s+/g, ' ') : null;
      const signedOutLabels = {signed_out};
      const successSelector = {success_selector};
      const successUrlContains = {success_url_contains};
      const url = location.href;
      const lower = `${{document.title}} ${{url}} ${{(document.body && document.body.innerText || '').slice(0, 2500)}}`.toLowerCase();
      const hasCaptchaWidget = Boolean(document.querySelector('iframe[src*="recaptcha" i],iframe[src*="hcaptcha" i],iframe[src*="challenges.cloudflare.com" i],[class*="captcha" i],[id*="captcha" i]'));
      const hasOtpInput = Boolean(document.querySelector('input[autocomplete="one-time-code"],input[name*="otp" i],input[name*="verification-code" i]'));
      const signals = [
        {{kind: 'captcha', matched: hasCaptchaWidget || /\\b(?:captcha|robot check|verify (?:that )?you are human)\\b/.test(lower), confidence: hasCaptchaWidget ? 'high' : 'medium'}},
        {{kind: 'passkey', matched: /\\b(?:passkey|security key)\\b/.test(lower), confidence: 'medium'}},
        {{kind: 'otp', matched: hasOtpInput || /\\b(?:one[- ]time (?:password|code)|verification code|enter (?:the )?(?:code|otp))\\b/.test(lower), confidence: hasOtpInput ? 'high' : 'medium'}},
        {{kind: 'email_verification', matched: /\\bverify (?:your )?email\\b/.test(lower), confidence: 'medium'}},
        {{kind: 'phone_verification', matched: /\\bverify (?:your )?(?:phone|mobile)\\b/.test(lower), confidence: 'medium'}},
        {{kind: 'consent', matched: /\\b(?:approve|allow) (?:agentweb|access|permissions?)\\b/.test(lower), confidence: 'medium'}},
        {{kind: 'antibot_interstitial', matched: /\\b(?:checking your browser|performing security verification)\\b/.test(lower), confidence: 'medium'}},
        {{kind: 'security_check', matched: /\\b(?:security check|two[- ]step verification)\\b/.test(lower), confidence: 'medium'}},
      ];
      const checkpoint = signals.find(signal => signal.matched) || null;
      const signInForm = Boolean(document.querySelector('form[name="signIn"], form[action*="signin" i], input[type="password"]'));
      const signedIn = Boolean(selector && label && !signedOutLabels.includes(label.toLowerCase()) && !signInForm && !/\\/ap\\/(?:signin|register)/.test(url));
      const sessionReady = Boolean(
        !signInForm && !checkpoint &&
        (!successSelector || document.querySelector(successSelector)) &&
        (!successUrlContains.length || successUrlContains.some(value => url.includes(value)))
      );
      const deadEntry = /looking for something|not a functioning page/.test(lower);
      const entryReady = entrySelector ? Boolean(document.querySelector(entrySelector)) : null;
      return {{url, title: document.title, account_label: label, signed_in: signedIn, session_ready: sessionReady, sign_in_form: signInForm, challenge: Boolean(checkpoint), human_checkpoint: checkpoint ? {{kind: checkpoint.kind, confidence: checkpoint.confidence, source: 'browser_signals'}} : null, dead_entry: deadEntry, entry_ready: entryReady}};
    }})()
    """
    try:
        result = client.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return dict((result.get("result") or {}).get("value") or {})
    except AgentWebError:
        return {}


def best_browser_auth_snapshot(
    port: int,
    domain: str,
    check: dict[str, Any] | None = None,
    *,
    process: subprocess.Popen[Any] | None = None,
) -> dict[str, Any]:
    """Inspect every page so redirects and newly opened tabs cannot strand auth."""
    try:
        targets = wait_for_debugger(port, timeout=2, process=process)
    except AgentWebError:
        return {}
    pages = [target for target in targets if target.get("type") == "page"]
    pages.sort(
        key=lambda target: domain.lstrip(".") not in str(target.get("url") or "")
    )
    snapshots: list[dict[str, Any]] = []
    for target in pages:
        websocket_url = target.get("webSocketDebuggerUrl")
        if not websocket_url:
            continue
        candidate: CDP | None = None
        try:
            candidate = CDP(str(websocket_url))
            snapshot = browser_auth_snapshot(candidate, check)
            if snapshot:
                snapshot["target_id"] = target.get("id")
                snapshots.append(snapshot)
        except (AgentWebError, *CDP_CONNECTION_ERRORS):
            continue
        finally:
            if candidate:
                try:
                    candidate.close()
                except Exception:
                    logger.debug("Failed to close candidate CDP client", exc_info=True)
    if not snapshots:
        return {}
    selected = max(
        snapshots,
        key=lambda value: (
            bool(value.get("signed_in")),
            domain.lstrip(".") in str(value.get("url") or ""),
            bool(value.get("entry_ready")),
            bool(value.get("human_checkpoint")),
        ),
    )
    selected["page_target_count"] = len(snapshots)
    return selected


def save_browser_identity(runtime: Runtime, site: str, client: CDP) -> None:
    try:
        result = client.call(
            "Runtime.evaluate",
            {
                "expression": "({user_agent:navigator.userAgent,language:navigator.language,platform:navigator.platform})",
                "returnByValue": True,
            },
        )
        value = (result.get("result") or {}).get("value") or {}
        if value.get("user_agent"):
            write_json(
                runtime.paths.profile_dir(site, runtime.profile)
                / "browser_identity.json",
                value,
            )
    except AgentWebError:
        return


def cookie_inventory(
    cookies: list[dict[str, Any]], allowed_domain: str
) -> list[dict[str, Any]]:
    result = []
    for item in cookies:
        domain = str(item.get("domain") or "")
        host = domain.lstrip(".").lower()
        if host != allowed_domain and not host.endswith("." + allowed_domain):
            continue
        result.append(
            {
                "name": str(item.get("name") or ""),
                "domain": domain,
                "path": str(item.get("path") or "/"),
                "secure": bool(item.get("secure")),
                "http_only": bool(item.get("httpOnly")),
                "same_site": item.get("sameSite"),
                "session": bool(item.get("session")),
            }
        )
    return sorted(result, key=lambda item: (item["domain"], item["path"], item["name"]))


def import_cdp_cookies(runtime: Runtime, site: str, cookies: list[dict[str, Any]]) -> int:
    manifest = runtime.describe(site)
    adapter = runtime.adapter(site)
    session = adapter.session()
    allowed_domain = manifest.get("cookie_domain", f".{site}.com").lstrip(".")
    count = 0
    for item in cookies:
        domain = str(item.get("domain") or "")
        if domain.lstrip(".") != allowed_domain and not domain.lstrip(".").endswith(
            "." + allowed_domain
        ):
            continue
        expires = item.get("expires")
        if not expires or float(expires) <= 0:
            expires_value = None
            discard = True
        else:
            expires_value = int(float(expires))
            discard = False
        cookie = Cookie(
            version=0,
            name=str(item["name"]),
            value=str(item.get("value", "")),
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path=str(item.get("path") or "/"),
            path_specified=True,
            secure=bool(item.get("secure", True)),
            expires=expires_value,
            discard=discard,
            comment=None,
            comment_url=None,
            rest={
                key: str(value)
                for key, value in (
                    ("HttpOnly", item.get("httpOnly")),
                    ("SameSite", item.get("sameSite")),
                )
                if value is not None
            },
            rfc2109=False,
        )
        session.cookies.set_cookie(cookie)
        count += 1
    session.save_cookies()
    return count


def _auth_status_operation(manifest: dict[str, Any]) -> str | None:
    commands = manifest.get("commands") or {}
    if "account_status" in commands:
        return "account_status"
    if "auth_status" in commands:
        return "auth_status"
    return None


def _connection_verification(
    manifest: dict[str, Any], mode: str
) -> dict[str, Any]:
    """Return the direct probe that proves a captured browser session is usable."""
    if mode == "session":
        protected = manifest.get("session_verification")
        if isinstance(protected, dict) and protected.get("operation"):
            return {
                "operation": str(protected["operation"]),
                "arguments": dict(protected.get("arguments") or {}),
                "success": str(protected.get("success") or "connected_status"),
            }
    return {
        "operation": _auth_status_operation(manifest),
        "arguments": {},
        "success": "connected_status",
    }


def _connection_verified(
    result: dict[str, Any] | None, verification: dict[str, Any]
) -> bool:
    if result is None:
        return False
    if verification.get("success") == "response":
        return True
    return status_is_connected(result)


def _safe_browser_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {
        key: snapshot.get(key)
        for key in (
            "title",
            "account_label",
            "signed_in",
            "session_ready",
            "sign_in_form",
            "human_checkpoint",
            "dead_entry",
            "entry_ready",
            "page_target_count",
        )
        if key in snapshot
    }
    raw_url = str(snapshot.get("url") or "")
    if raw_url:
        parsed = urlparse(raw_url)
        safe["url"] = parsed._replace(query="", fragment="").geturl()
    return safe


def _browser_is_alive(
    attempt: AuthAttempt, process: subprocess.Popen[Any] | None
) -> bool:
    return process.poll() is None if process is not None else process_is_alive(attempt.pid)


def _stop_auth_browser(
    attempt: AuthAttempt, process: subprocess.Popen[Any] | None
) -> None:
    if process is None:
        terminate_attempt(attempt)
        return
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _monitor_cookie_auth_attempt(
    runtime: Runtime,
    site: str,
    manifest: dict[str, Any],
    attempt: AuthAttempt,
    *,
    timeout_seconds: int,
    capture_now: bool,
    progress: Progress,
    process: subprocess.Popen[Any] | None = None,
    client: CDP | None = None,
) -> dict[str, Any]:
    store = AuthAttemptStore(runtime.paths, site, runtime.profile)
    verification = _connection_verification(manifest, attempt.mode)
    status_operation = verification.get("operation")
    auth_cookie_names = set(manifest.get("auth_cookie_names") or [])
    session_cookie_names = set(manifest.get("session_cookie_names") or [])
    allowed_domain = str(
        manifest.get("cookie_domain")
        or urlparse(str(manifest.get("base_url") or "")).hostname
        or site
    ).lstrip(".")
    if not attempt.debugger_port:
        raise AgentWebError("The saved authorization attempt has no debugger port")
    if client is None:
        client = open_debugger_client(
            attempt.debugger_port,
            allowed_domain,
            timeout=10,
            process=process,
        )
    store.update(
        attempt,
        state=AuthState.AUTHORIZING.value,
        expires_at=max(attempt.expires_at, time.time() + 3600),
        error=None,
    )
    deadline = time.monotonic() + max(timeout_seconds, 1)
    reconnects = 0
    direct_rejections = 0
    last_imported = 0
    last_verification_error: dict[str, Any] | None = None
    last_snapshot: dict[str, Any] = {}
    last_inventory: list[dict[str, Any]] = []
    reported_checkpoint: str | None = None
    try:
        while time.monotonic() < deadline:
            if not _browser_is_alive(attempt, process):
                store.update(
                    attempt,
                    state=AuthState.CANCELLED.value,
                    pid=None,
                    debugger_port=None,
                    error="The authorization window was closed before sign-in completed.",
                )
                raise AgentWebError(
                    "The login window closed before the session was connected",
                    code="authorization_cancelled",
                    retryable=True,
                )
            try:
                observed = client.call("Network.getAllCookies").get("cookies", [])
            except CDP_CONNECTION_ERRORS:
                reconnects += 1
                if reconnects > 5:
                    store.update(
                        attempt,
                        state=AuthState.FAILED.value,
                        error="The login page repeatedly reset its debugging connection.",
                    )
                    _stop_auth_browser(attempt, process)
                    raise AgentWebError(
                        "The login page repeatedly reset its connection",
                        code="browser_debugger_unavailable",
                        retryable=True,
                    )
                try:
                    client.close()
                except Exception:
                    logger.debug(
                        "Failed to close CDP client before reconnect", exc_info=True
                    )
                client = open_debugger_client(
                    attempt.debugger_port,
                    allowed_domain,
                    timeout=10,
                    process=process,
                )
                continue

            names = {cookie.get("name") for cookie in observed}
            last_inventory = cookie_inventory(observed, allowed_domain)
            browser_check = (
                manifest.get("session_browser_check")
                if attempt.mode == "session"
                else manifest.get("auth_browser_check")
            )
            last_snapshot = browser_auth_snapshot(client, browser_check)
            cross_target = best_browser_auth_snapshot(
                attempt.debugger_port,
                allowed_domain,
                browser_check,
                process=process,
            )
            if cross_target and (
                cross_target.get("signed_in")
                or cross_target.get("human_checkpoint")
                or not last_snapshot
            ):
                last_snapshot = cross_target
            if last_snapshot.get("dead_entry"):
                store.update(
                    attempt,
                    state=AuthState.FAILED.value,
                    browser=_safe_browser_snapshot(last_snapshot),
                    error="The adapter authentication entry URL was rejected.",
                )
                _stop_auth_browser(attempt, process)
                raise AgentWebError(
                    f"{site} changed or rejected its authentication entry URL",
                    code="auth_entry_invalid",
                    retryable=True,
                    next_action=f"update the {site} adapter authentication entry URL before retrying",
                    details={"browser": _safe_browser_snapshot(last_snapshot)},
                )

            checkpoint = HumanCheckpoint.from_snapshot(last_snapshot)
            if checkpoint:
                checkpoint_value: dict[str, Any] | None = {
                    "kind": checkpoint.kind,
                    "instruction": checkpoint.instruction,
                    "confidence": checkpoint.confidence,
                    "source": checkpoint.source,
                }
                store.update(
                    attempt,
                    state=AuthState.HUMAN_REQUIRED.value,
                    checkpoint=checkpoint_value,
                    browser=_safe_browser_snapshot(last_snapshot),
                )
                if checkpoint.kind != reported_checkpoint:
                    progress(
                        f"{checkpoint.instruction} AgentWeb will keep this window open and resume automatically."
                    )
                    reported_checkpoint = checkpoint.kind

            site_cookies = [
                cookie
                for cookie in observed
                if str(cookie.get("domain") or "").lstrip(".") == allowed_domain
                or str(cookie.get("domain") or "").lstrip(".").endswith(
                    "." + allowed_domain
                )
            ]
            ready = (
                bool(last_snapshot.get("session_ready"))
                if attempt.mode == "session" and manifest.get("session_browser_check")
                else bool(names.intersection(session_cookie_names))
                if capture_now and session_cookie_names
                else bool(site_cookies)
                if capture_now
                else (
                    bool(last_snapshot.get("signed_in"))
                    or not auth_cookie_names
                    or bool(names.intersection(auth_cookie_names))
                )
            )
            if ready:
                store.update(attempt, state=AuthState.VERIFYING.value)
                imported = import_cdp_cookies(runtime, site, observed)
                last_imported = imported
                status = None
                verification_error: dict[str, Any] | None = None
                if status_operation:
                    for verification_attempt in range(2):
                        try:
                            status = runtime.adapter(site).call(
                                str(status_operation),
                                dict(verification.get("arguments") or {}),
                            )
                            verification_error = None
                            break
                        except AgentWebError as exc:
                            verification_error = exc.as_dict()
                            if verification_attempt == 0:
                                time.sleep(0.5)
                else:
                    verification_error = {
                        "error": "verification_unavailable",
                        "message": (
                            f"{site} does not declare a direct authentication "
                            "verification operation"
                        ),
                        "retryable": False,
                    }
                last_verification_error = verification_error
                verified_connected = _connection_verified(status, verification)
                if verified_connected:
                    authenticated_cookie_captured = bool(
                        names.intersection(auth_cookie_names)
                    )
                    verification_fields = connection_verification_fields(
                        status,
                        verified_connected=verified_connected,
                        verification_error=(
                            str(verification_error.get("message"))
                            if verification_error
                            else None
                        ),
                        authenticated_cookie_captured=authenticated_cookie_captured,
                    )
                    _stop_auth_browser(attempt, process)
                    store.update(
                        attempt,
                        state=AuthState.CONNECTED.value,
                        pid=None,
                        debugger_port=None,
                        checkpoint=None,
                        browser=_safe_browser_snapshot(last_snapshot),
                    )
                    return {
                        "site": site,
                        "profile": runtime.profile,
                        "mode": attempt.mode,
                        "state": AuthState.CONNECTED.value,
                        "attempt_id": attempt.attempt_id,
                        "connected": True,
                        "cookies_imported": imported,
                        "account": status,
                        "account_verification": verification_fields[
                            "account_verification"
                        ],
                        "warning": verification_fields["warning"],
                        "verification_error": verification_error,
                        "verification_operation": status_operation,
                        "verification_scope": (
                            "protected_session"
                            if attempt.mode == "session"
                            else "account"
                        ),
                        "browser_profile": "site_scoped_persistent",
                        "browser_profile_persisted": True,
                        "session_only": attempt.mode == "session",
                        "browser_opened": True,
                        "browser_retained": False,
                        "browser_authentication": (
                            "verified"
                            if last_snapshot.get("signed_in")
                            else "cookie_candidate"
                        ),
                        "direct_authentication": verification_fields[
                            "direct_authentication"
                        ],
                    }
                direct_rejections += 1
                if direct_rejections >= 2:
                    store.update(
                        attempt,
                        state=AuthState.CAPTURED_UNVERIFIED.value,
                        browser=_safe_browser_snapshot(last_snapshot),
                        error="The imported browser session failed direct verification.",
                    )
                    return {
                        "site": site,
                        "profile": runtime.profile,
                        "mode": attempt.mode,
                        "state": AuthState.CAPTURED_UNVERIFIED.value,
                        "attempt_id": attempt.attempt_id,
                        "connected": False,
                        "cookies_imported": imported,
                        "cookies_captured": imported > 0,
                        "human_action_required": True,
                        "instruction": (
                            f"{site} is open and its cookies were captured, but "
                            "AgentWeb could not verify that direct account requests "
                            "work. Complete any visible website checkpoint, then "
                            "resume verification."
                        ),
                        "verification_operation": status_operation,
                        "verification_error": verification_error,
                        "browser_opened": True,
                        "browser_retained": True,
                        "resume_command": [
                            "agentweb",
                            "--profile",
                            runtime.profile,
                            "auth",
                            "resume",
                            site,
                        ],
                        "cookie_inventory": last_inventory,
                    }
            time.sleep(1)

        checkpoint = HumanCheckpoint.from_snapshot(last_snapshot)
        checkpoint_value = (
            {
                "kind": checkpoint.kind,
                "instruction": checkpoint.instruction,
                "confidence": checkpoint.confidence,
                "source": checkpoint.source,
            }
            if checkpoint
            else None
        )
        state = (
            AuthState.HUMAN_REQUIRED.value
            if checkpoint
            else AuthState.CAPTURED_UNVERIFIED.value
            if last_imported
            else AuthState.AUTHORIZING.value
        )
        store.update(
            attempt,
            state=state,
            checkpoint=checkpoint_value,
            browser=_safe_browser_snapshot(last_snapshot),
        )
        return {
            "site": site,
            "profile": runtime.profile,
            "mode": attempt.mode,
            "state": state,
            "attempt_id": attempt.attempt_id,
            "connected": False,
            "cookies_captured": last_imported > 0,
            "cookies_imported": last_imported,
            "human_action_required": True,
            "checkpoint": checkpoint_value,
            "instruction": (
                checkpoint.instruction
                if checkpoint
                else (
                    f"{site} cookies were captured, but direct verification has "
                    "not succeeded. Complete any visible website checkpoint, then "
                    "resume verification."
                )
                if last_imported
                else f"Finish signing in to {site} in the open window."
            ),
            "verification_operation": status_operation,
            "verification_error": last_verification_error,
            "browser_opened": True,
            "browser_retained": True,
            "expires_at": attempt.expires_at,
            "resume_command": [
                "agentweb",
                "--profile",
                runtime.profile,
                "auth",
                "resume",
                site,
            ],
            "cookie_inventory": last_inventory,
        }
    finally:
        try:
            client.close()
        except Exception:
            logger.debug("Failed to close CDP client after connect flow", exc_info=True)


def connect_with_oauth2_pkce(
    runtime: Runtime,
    site: str,
    *,
    mode: str,
    timeout_seconds: int,
    progress: Progress,
) -> dict[str, Any]:
    adapter = runtime.adapter(site)
    status = adapter.call("auth_status", {})
    if status.get("authenticated"):
        return {
            "site": site,
            "profile": runtime.profile,
            "mode": mode,
            "connected": True,
            "account": status,
            "account_verification": "verified",
            "credential_provider": "oauth2_pkce",
            "client_secret_stored": False,
            "browser_opened": False,
            "already_connected": True,
        }

    callback: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            values = parse_qs(parsed.query)
            for key in ("code", "state", "error"):
                if values.get(key):
                    callback[key] = values[key][0]
            successful = bool(callback.get("code")) and not callback.get("error")
            body = (
                f"<h1>{site.title()} authorization received</h1><p>Return to your agent while AgentWeb verifies the account.</p>"
                if successful
                else f"<h1>{site.title()} authorization was not completed</h1><p>Return to AgentWeb for details.</p>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
    server.timeout = 1
    redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
    oauth = adapter._oauth_begin(redirect_uri)
    progress(
        f"Opening {site}'s OAuth PKCE authorization flow in your system browser. "
        + (
            "Create an account if needed, then approve AgentWeb's requested permissions."
            if mode == "signup"
            else "Sign in if needed, then approve AgentWeb's requested permissions."
        )
    )
    if not webbrowser.open(str(oauth["authorization_url"]), new=1, autoraise=True):
        raise AgentWebError(
            f"Could not open the system browser for {site} authorization",
            code="browser_launch_failed",
            retryable=True,
            details={"authorization_url": oauth["authorization_url"]},
        )
    try:
        deadline = time.monotonic() + max(timeout_seconds, 1)
        while time.monotonic() < deadline and not callback:
            server.handle_request()
        if not callback:
            raise AgentWebError(
                f"Timed out after {timeout_seconds} seconds waiting for {site} authorization"
            )
        if callback.get("state") != oauth["state"]:
            raise AgentWebError(f"{site} authorization state did not match; no token was saved")
        if callback.get("error"):
            raise AgentWebError(
                f"{site} authorization was denied: {callback['error']}"
            )
        code = callback.get("code")
        if not code:
            raise AgentWebError(f"{site} authorization did not return a code")
        adapter._oauth_exchange(
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=oauth["code_verifier"],
        )
        status = adapter.call("auth_status", {})
        if not status.get("authenticated"):
            raise AgentWebError(
                f"{site} authorization completed, but the account could not be verified"
            )
        return {
            "site": site,
            "profile": runtime.profile,
            "mode": mode,
            "connected": True,
            "account": status,
            "account_verification": "verified",
            "credential_provider": "oauth2_pkce",
            "client_secret_stored": False,
            "browser_opened": True,
            "browser_kind": "system",
            "already_connected": False,
            "granted_scopes": sorted(
                str((read_json(adapter.tokens_path, {}) or {}).get("scope") or "").split()
            ),
            "website_cookies_captured": 0,
        }
    finally:
        server.server_close()


def connect_site(
    runtime: Runtime,
    site: str,
    *,
    mode: str = "login",
    timeout_seconds: int = 600,
    capture_now: bool = False,
    progress: Progress = stderr_progress,
) -> dict[str, Any]:
    manifest = runtime.describe(site)
    if mode not in {"login", "signup", "session"}:
        raise AgentWebError("connection mode must be login, signup, or session")
    if mode == "session":
        capture_now = True
    auth = manifest.get("auth") or {}
    strategy = auth.get("strategy") or "none"
    if strategy == "personal_access_token":
        adapter = runtime.adapter(site)
        status = adapter.call("auth_status", {})
        if status.get("authenticated"):
            return {
                "site": site,
                "profile": runtime.profile,
                "mode": mode,
                "connected": True,
                "account": status,
                "account_verification": "verified",
                "credential_provider": "personal_access_token",
                "browser_opened": False,
                "already_connected": True,
                "authorization_scope": manifest.get("auth") or {},
            }
        raise ConfigurationRequired(
            f"{site} needs a one-time access token before account operations can run",
            operation=f"{site}.{auth.get('setup_operation') or 'configure_token'}",
        )
    if strategy == "oauth2_pkce":
        return connect_with_oauth2_pkce(
            runtime,
            site,
            mode=mode,
            timeout_seconds=timeout_seconds,
            progress=progress,
        )
    commands = manifest.get("commands") or {}
    status_operation = _auth_status_operation(manifest)

    # session mode is an explicit refresh request. Do not short-circuit merely
    # because a weaker existing session can still load an account page.
    if strategy == "cookie_session" and status_operation and mode != "session":
        try:
            existing_status = runtime.adapter(site).call(status_operation, {})
        except AgentWebError:
            existing_status = None
        if status_is_connected(existing_status):
            existing_attempt_store = AuthAttemptStore(
                runtime.paths, site, runtime.profile
            )
            existing_attempt = existing_attempt_store.load()
            if existing_attempt and existing_attempt.state in ACTIVE_AUTH_STATES:
                terminate_attempt(existing_attempt)
                existing_attempt_store.update(
                    existing_attempt,
                    state=AuthState.CONNECTED.value,
                    pid=None,
                    debugger_port=None,
                    checkpoint=None,
                    error=None,
                )
            return {
                "site": site,
                "profile": runtime.profile,
                "mode": mode,
                "connected": True,
                "account": existing_status,
                "account_verification": "verified",
                "credential_provider": "cookie_session",
                "browser_opened": False,
                "already_connected": True,
                "authorization_scope": manifest.get("auth") or {},
            }
    auth_supported = bool(
        manifest.get("login_url")
        or manifest.get("signup_url")
        or manifest.get("auth_cookie_names")
        or strategy != "none"
        or "account_status" in commands
    )
    if not auth_supported:
        raise AgentWebError(
            f"The {site} adapter does not implement authentication. "
            "site_connect cannot provide private or account operations for this adapter; "
            "its public operations work without connecting."
        )
    target_url = (
        manifest.get(f"{mode}_url")
        if mode != "session"
        else manifest.get("session_url") or manifest.get("base_url")
    )
    if not target_url:
        raise AgentWebError(f"{site} does not declare a {mode} URL")
    from .web_runtime import WebRuntime

    web = WebRuntime(
        runtime.paths,
        site=site,
        profile=runtime.profile,
        base_url=manifest["base_url"],
        cookie_domain=manifest.get("cookie_domain"),
        allowed_domains=manifest.get("allowed_domains"),
    )
    attempt_store = AuthAttemptStore(runtime.paths, site, runtime.profile)
    active_attempt = attempt_store.load()
    if active_attempt and active_attempt.state == AuthState.EXPIRED.value:
        terminate_attempt(active_attempt)
        attempt_store.update(
            active_attempt,
            pid=None,
            debugger_port=None,
            error="The authorization attempt expired before it was completed.",
        )
    if active_attempt and active_attempt.state in ACTIVE_AUTH_STATES:
        if process_is_alive(active_attempt.pid) and active_attempt.debugger_port:
            progress(f"Resuming the existing {site} authorization window.")
            return _monitor_cookie_auth_attempt(
                runtime,
                site,
                manifest,
                active_attempt,
                timeout_seconds=timeout_seconds,
                capture_now=active_attempt.mode == "session" or capture_now,
                progress=progress,
            )
        attempt_store.update(
            active_attempt,
            state=AuthState.FAILED.value,
            pid=None,
            debugger_port=None,
            error="The saved authorization browser is no longer running.",
        )
    web.stop()
    profile_dir = web.chrome_profile
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    browser_impersonation = manifest.get("browser_impersonation") or {}
    progress(
        f"Opening {site} "
        + (
            "account creation. Complete the normal website flow in the new window."
            if mode == "signup"
            else "sign-in. Complete the normal website flow in the new window."
            if mode == "login"
            else "at the protected page. Complete any website-requested security step; AgentWeb will capture the refreshed session and close the window automatically."
        )
    )
    process: subprocess.Popen[Any] | None = None
    client: CDP | None = None
    retain_browser = False
    try:
        launch_error: AgentWebError | None = None
        port = 0
        for launch_attempt in range(2):
            stale_debug_file = profile_dir / "DevToolsActivePort"
            if stale_debug_file.exists():
                try:
                    stale_debug_file.unlink()
                except OSError:
                    pass
            port = available_port()
            command = [
                chrome_executable(),
                f"--user-data-dir={profile_dir}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=http://127.0.0.1",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
            ]
            if browser_impersonation.get("user_agent"):
                command.append(
                    f"--user-agent={browser_impersonation['user_agent']}"
                )
            command.append(target_url)
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                client = open_debugger_client(
                    port,
                    manifest.get("cookie_domain", site),
                    timeout=20,
                    process=process,
                )
                launch_error = None
                break
            except (AgentWebError, *CDP_CONNECTION_ERRORS) as exc:
                launch_error = (
                    exc
                    if isinstance(exc, AgentWebError)
                    else AgentWebError(str(exc), code="browser_debugger_unavailable", retryable=True)
                )
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if launch_attempt == 0:
                    progress("Chrome did not expose its debugger; retrying once with a fresh port.")
                    time.sleep(0.5)
        if client is None or process is None:
            raise launch_error or AgentWebError(
                "Could not start the managed login browser",
                code="browser_launch_failed",
                retryable=True,
            )
        save_browser_identity(runtime, site, client)
        attempt = AuthAttempt.create(
            site=site,
            profile=runtime.profile,
            mode=mode,
            strategy=strategy,
            timeout_seconds=timeout_seconds,
            pid=process.pid,
            debugger_port=port,
        )
        attempt_store.save(attempt)
        result = _monitor_cookie_auth_attempt(
            runtime,
            site,
            manifest,
            attempt,
            timeout_seconds=timeout_seconds,
            capture_now=capture_now,
            progress=progress,
            process=process,
            client=client,
        )
        client = None  # The monitor owns and closes the debugger client.
        retain_browser = bool(result.get("browser_retained"))
        return result
    finally:
        if client:
            try:
                client.close()
            except Exception:
                logger.debug("Failed to close CDP client during teardown", exc_info=True)
        if process is not None and process.poll() is None and not retain_browser:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def connection_handoff(
    runtime: Runtime,
    site: str,
    *,
    mode: str = "login",
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Describe one-time authorization without opening a browser for an agent."""
    manifest = runtime.describe(site)
    if mode not in {"login", "signup", "session"}:
        raise AgentWebError("connection mode must be login, signup, or session")
    if mode == "signup" and not manifest.get("signup_url"):
        raise AgentWebError(f"{site} does not declare an account creation URL")
    status_operation = _auth_status_operation(manifest)
    if status_operation and mode != "session":
        try:
            status = runtime.adapter(site).call(status_operation, {})
        except AgentWebError:
            status = None
        if status_is_connected(status):
            return {
                "site": site,
                "profile": runtime.profile,
                "mode": mode,
                "connected": True,
                "already_connected": True,
                "account": status,
                "agent_browser_opened": False,
                "authorization_scope": manifest.get("auth") or {},
            }
    attempt = AuthAttemptStore(runtime.paths, site, runtime.profile).load()
    if (
        attempt
        and attempt.state in ACTIVE_AUTH_STATES
        and process_is_alive(attempt.pid)
    ):
        resume_command = ["agentweb"]
        if runtime.profile != "default":
            resume_command.extend(["--profile", runtime.profile])
        resume_command.extend(["auth", "resume", site])
        return {
            **attempt.public(),
            "connected": False,
            "human_action_required": True,
            "agent_browser_opened": False,
            "command": resume_command,
            "instruction": (
                (attempt.checkpoint or {}).get("instruction")
                or f"Finish signing in to {site} in the open authorization window."
            ),
            "resume_operation_automatically": False,
            "resume_policy": "retry safe reads after connection; reconfirm writes",
        }
    command = ["agentweb"]
    if runtime.profile != "default":
        command.extend(["--profile", runtime.profile])
    command.extend([
        "connect",
        site,
        "--mode",
        mode,
        "--timeout",
        str(max(1, timeout_seconds)),
    ])
    return {
        "site": site,
        "profile": runtime.profile,
        "mode": mode,
        "connected": False,
        "human_action_required": True,
        "agent_browser_opened": False,
        "authorization_scope": manifest.get("auth") or {},
        "authorization_url": (
            manifest.get(f"{mode}_url")
            if mode != "session"
            else manifest.get("session_url") or manifest.get("base_url")
        ),
        "command": command,
        "instruction": (
            "Ask the user to run this one command in their own terminal and complete "
            "the website's security checks. Do not run it through an agent shell. "
            "After it finishes, retry the original direct operation."
        ),
        "resume_operation_automatically": False,
        "resume_policy": "retry safe reads after connection; reconfirm writes",
    }


def authentication_status(runtime: Runtime, site: str) -> dict[str, Any]:
    manifest = runtime.describe(site)
    account = None
    status_operation = _auth_status_operation(manifest)
    if status_operation:
        try:
            account = runtime.adapter(site).call(status_operation, {})
        except AgentWebError:
            account = None
    attempt = AuthAttemptStore(runtime.paths, site, runtime.profile).load()
    return {
        "site": site,
        "profile": runtime.profile,
        "state": (
            AuthState.CONNECTED.value
            if status_is_connected(account)
            else attempt.state if attempt else AuthState.DISCONNECTED.value
        ),
        "connected": status_is_connected(account),
        "account": account,
        "attempt": attempt.public() if attempt else None,
    }


def cancel_authentication(runtime: Runtime, site: str) -> dict[str, Any]:
    store = AuthAttemptStore(runtime.paths, site, runtime.profile)
    attempt = store.load()
    if not attempt or attempt.state not in ACTIVE_AUTH_STATES:
        return {
            "site": site,
            "profile": runtime.profile,
            "cancelled": False,
            "already_stopped": True,
        }
    terminate_attempt(attempt)
    store.update(
        attempt,
        state=AuthState.CANCELLED.value,
        pid=None,
        debugger_port=None,
        error=None,
    )
    return {
        "site": site,
        "profile": runtime.profile,
        "attempt_id": attempt.attempt_id,
        "state": attempt.state,
        "cancelled": True,
    }


def disconnect_site(runtime: Runtime, site: str) -> dict[str, Any]:
    manifest = runtime.describe(site)
    cancel_authentication(runtime, site)
    credential_cleanup = None
    if "disconnect" in (manifest.get("commands") or {}):
        credential_cleanup = runtime.adapter(site).call("disconnect", {"confirm": True})
    from .web_runtime import WebRuntime

    web = WebRuntime(
        runtime.paths,
        site=site,
        profile=runtime.profile,
        base_url=manifest["base_url"],
        cookie_domain=manifest.get("cookie_domain"),
        allowed_domains=manifest.get("allowed_domains"),
    )
    web.stop()
    cookie_path = runtime.paths.cookie_file(site, runtime.profile)
    cookie_removed = cookie_path.exists()
    cookie_path.unlink(missing_ok=True)
    browser_profile_removed = web.chrome_profile.exists()
    if browser_profile_removed:
        shutil.rmtree(web.chrome_profile)
    return {
        "site": site,
        "profile": runtime.profile,
        "state": AuthState.DISCONNECTED.value,
        "connected": False,
        "cookies_removed": cookie_removed,
        "browser_profile_removed": browser_profile_removed,
        "credential_cleanup": credential_cleanup,
    }


def install_agent(agent: str, *, scope: str = "user", dry_run: bool = False) -> dict[str, Any]:
    executable = shutil.which("agentweb")
    if not executable:
        raise AgentWebError("The agentweb executable is not on PATH")
    if agent == "claude":
        binary = shutil.which("claude")
        if not binary:
            raise AgentWebError("Claude Code was not found on PATH")
        remove = [binary, "mcp", "remove", "--scope", scope, "agentweb"]
        legacy_remove = [binary, "mcp", "remove", "--scope", scope, "sitepack"]
        add = [binary, "mcp", "add", "--scope", scope, "agentweb", "--", executable, "mcp"]
    elif agent == "codex":
        binary = shutil.which("codex")
        if not binary:
            raise AgentWebError("Codex was not found on PATH")
        remove = [binary, "mcp", "remove", "agentweb"]
        legacy_remove = [binary, "mcp", "remove", "sitepack"]
        add = [binary, "mcp", "add", "agentweb", "--", executable, "mcp"]
    else:
        raise AgentWebError("agent must be claude or codex")
    if dry_run:
        return {
            "agent": agent,
            "remove": remove,
            "legacy_remove": legacy_remove,
            "add": add,
            "installed": False,
        }
    subprocess.run(legacy_remove, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(remove, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    completed = subprocess.run(add, capture_output=True, text=True)
    if completed.returncode != 0:
        raise AgentWebError(completed.stderr.strip() or completed.stdout.strip())
    return {
        "agent": agent,
        "scope": scope if agent == "claude" else "global",
        "installed": True,
        "command": [executable, "mcp"],
    }


def install_agent_skills(
    executable: str | None = None,
    *,
    home: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Teach shell-capable agents to discover the CLI without requiring MCP."""
    discovered = executable or shutil.which("agentweb")
    if not discovered:
        raise AgentWebError("The agentweb executable is not on PATH")
    resolved = str(Path(discovered).expanduser().resolve())
    user_home = (home or Path.home()).expanduser()
    destinations = {
        "claude": user_home / ".claude" / "skills" / "agentweb" / "SKILL.md",
        "codex": user_home / ".codex" / "skills" / "agentweb" / "SKILL.md",
    }
    content = f"""---
name: agentweb
description: Use mapped websites through the AgentWeb CLI instead of browser clicking. Use for Amazon, arXiv, GitHub, GST, Hacker News, Hugging Face, LinkedIn, npm, Spotify, Stack Overflow, Wikipedia, and any site shown by AgentWeb.
---

# AgentWeb

Use `{resolved}` through the shell for website tasks. AgentWeb returns bounded structured JSON and should be preferred over browser use when a mapped operation exists.

Start discovery with `{resolved} sites`. For one website, use `{resolved} capabilities DOMAIN` or `{resolved} DOMAIN ACTION --help`. Execute operations with `{resolved} DOMAIN ACTION [arguments]` or `{resolved} run DOMAIN ACTION --input '{{...}}'`.

Public reads need no setup. If an account operation reports authentication required, run `{resolved} connect DOMAIN` and let the user complete only the website's requested login or security checkpoint. Respect confirmation requirements for consequential writes.
"""
    if not dry_run:
        for destination in destinations.values():
            atomic_write(destination, content.encode("utf-8"), mode=0o600)
    return {
        "installed": not dry_run,
        "executable": resolved,
        "skills": {agent: str(path) for agent, path in destinations.items()},
        "interface": "cli",
        "mcp_installed": False,
    }


def setup_detected_agents(
    runtime: Runtime, *, scope: str = "user", dry_run: bool = False
) -> dict[str, Any]:
    sync_result = runtime.registry.sync()
    detected = [agent for agent in ("claude", "codex") if shutil.which(agent)]
    installed = []
    errors = []
    for agent in detected:
        try:
            installed.append(install_agent(agent, scope=scope, dry_run=dry_run))
        except AgentWebError as exc:
            errors.append({"agent": agent, "error": str(exc)})
    return {
        "ready": not errors,
        "agentweb": "installed",
        "registry": sync_result,
        "detected_agents": detected,
        "agent_connections": installed,
        "errors": errors,
        "public_operations_ready": True,
        "authentication": "lazy; only requested by protected operations",
        "restart_required": detected,
        "manual_mcp_config": (
            None
            if detected
            else {"mcpServers": {"agentweb": {"command": "agentweb", "args": ["mcp"]}}}
        ),
    }
