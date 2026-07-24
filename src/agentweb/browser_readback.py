"""Browser-assisted read-back for capture oracles.

Some sites (LinkedIn's Voyager API behind PerimeterX, for example) actively
refuse keyless browserless replay: a saved session authenticates once during
``agentweb connect`` but a plain ``curl_cffi``/``urllib`` request seconds later
is bounced into a redirect/challenge loop. Their reads can still be verified,
just not from outside a real browser.

This module runs a *single typed read operation* inside the already-authenticated
Chrome profile via CDP: the adapter builds and parses the request exactly as
usual, but the actual HTTP is issued with ``fetch(..., {credentials:"include"})``
from a page on the site's own origin, so first-party cookies and anti-bot
context apply. The parsed response flows back through the normal AgentWeb
envelope, so a response oracle captured this way is identical in shape to one
captured browserlessly.

Invariant: this path is *only* reached through the explicit ``--via-browser``
flag on ``capture-oracle``/``verify-capture``. Ordinary typed operations never
launch a browser.
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .connector import (
    CDP,
    CDP_CONNECTION_ERRORS,
    available_port,
    chrome_executable,
    open_debugger_client,
    seed_profile_from_default_browser,
    use_default_browser_enabled,
)
from .runtime import Runtime
from .sdk import AgentWebError, HttpSession, Response

# Request headers a browser manages itself; setting them via fetch() is either
# forbidden by the Fetch spec or would fight the browser's first-party context.
_BROWSER_MANAGED_HEADERS = frozenset(
    {
        "host",
        "cookie",
        "content-length",
        "connection",
        "origin",
        "referer",
        "user-agent",
        "accept-encoding",
        "accept-language",
        "content-encoding",
        "transfer-encoding",
        "cache-control",
    }
)

_FETCH_SCRIPT = r"""
(async () => {
  const spec = JSON.parse(__AGENTWEB_FETCH_SPEC__);
  try {
    const init = {
      method: spec.method,
      headers: spec.headers,
      credentials: 'include',
      redirect: 'follow',
    };
    if (spec.body_b64 !== null) {
      const bin = atob(spec.body_b64);
      const arr = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      init.body = arr;
    }
    const resp = await fetch(spec.url, init);
    const buf = await resp.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let binary = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    const headers = {};
    resp.headers.forEach((value, key) => { headers[key] = value; });
    return {status: resp.status, url: resp.url, headers: headers, body_b64: btoa(binary)};
  } catch (error) {
    return {error: String(error && error.message || error)};
  }
})()
"""


class BrowserSession(HttpSession):
    """An :class:`HttpSession` whose transport is an in-page ``fetch``.

    Reuses all of :class:`HttpSession`'s request building, cookie jar (for the
    CSRF token adapters derive from ``JSESSIONID`` and friends) and response
    parsing; only :meth:`_perform_request` is swapped so the request rides the
    authenticated browser instead of ``curl_cffi``/``urllib``.
    """

    def __init__(
        self,
        paths: Any,
        site: str,
        profile: str,
        *,
        client: CDP,
        allowed_domains: tuple[str, ...],
        fresh: bool = False,
        cancellation: Any | None = None,
    ) -> None:
        super().__init__(paths, site, profile, fresh=fresh, cancellation=cancellation)
        self._client = client
        self._allowed_domains = tuple(
            domain.lstrip(".").lower() for domain in allowed_domains if domain
        )

    def sync_from_browser(self) -> int:
        """Refresh the cookie jar from the live browser session.

        Adapters derive request state (e.g. LinkedIn's ``Csrf-Token`` from the
        current ``JSESSIONID``) from this jar. The browser rotates those cookies,
        so a stale on-disk jar would make the adapter send a token that no longer
        matches the cookie the browser attaches, and the site would reject the
        request. Pull the browser's current cookies (in memory only) so the two
        stay consistent.
        """
        from http.cookiejar import Cookie

        cookies = self._client.call("Network.getAllCookies").get("cookies", [])
        count = 0
        for item in cookies:
            domain = str(item.get("domain") or "")
            host = domain.lstrip(".").lower()
            if self._allowed_domains and not any(
                host == domain_name or host.endswith("." + domain_name)
                for domain_name in self._allowed_domains
            ):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            expires = item.get("expires")
            expires_value = (
                int(float(expires)) if expires and float(expires) > 0 else None
            )
            cookie = Cookie(
                version=0,
                name=name,
                value=str(item.get("value", "")),
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=bool(domain),
                domain_initial_dot=domain.startswith("."),
                path=str(item.get("path") or "/"),
                path_specified=True,
                secure=bool(item.get("secure", True)),
                expires=expires_value,
                discard=expires_value is None,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": item.get("httpOnly")},
            )
            self.cookies.set_cookie(cookie)
            count += 1
        return count

    def _assert_allowed(self, url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https":
            raise AgentWebError(
                f"Browser-assisted request refused non-HTTPS URL for {self.site}"
            )
        if self._allowed_domains and not any(
            host == domain or host.endswith("." + domain)
            for domain in self._allowed_domains
        ):
            raise AgentWebError(
                f"Browser-assisted request for {self.site} escaped its host allowlist"
            )

    def _perform_request(
        self,
        *,
        method: str,
        url: str,
        data: bytes | None,
        content_type: str | None,
        headers: dict[str, str] | None,
        referer: str | None,
        impersonate: str | None,
        allowed_redirect_domains: tuple[str, ...] | None,
        timeout_seconds: float,
    ) -> Response:
        self.cancellation.check()
        self._assert_allowed(url)
        fetch_headers: dict[str, str] = {}
        for name, value in (headers or {}).items():
            if name.lower() in _BROWSER_MANAGED_HEADERS:
                continue
            fetch_headers[name] = value
        if data is not None and content_type:
            fetch_headers.setdefault("Content-Type", content_type)
        spec = json.dumps(
            {
                "url": url,
                "method": method.upper(),
                "headers": fetch_headers,
                "body_b64": (
                    base64.b64encode(data).decode("ascii") if data is not None else None
                ),
            }
        )
        expression = _FETCH_SCRIPT.replace("__AGENTWEB_FETCH_SPEC__", json.dumps(spec))
        started = time.perf_counter()
        previous_timeout = self._client.socket.gettimeout()
        try:
            self._client.socket.settimeout(min(max(timeout_seconds, 1.0), 60.0) + 5.0)
            result = self._client.call(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
        finally:
            try:
                self._client.socket.settimeout(previous_timeout)
            except Exception:
                pass
        exception = result.get("exceptionDetails")
        if exception:
            raise AgentWebError(
                "Browser-assisted request failed: "
                + str(
                    (exception.get("exception") or {}).get("description")
                    or exception.get("text")
                    or "unknown error"
                )
            )
        payload = (result.get("result") or {}).get("value") or {}
        if payload.get("error"):
            raise AgentWebError(
                f"Browser-assisted request to {self.site} failed: {payload['error']}"
            )
        body = base64.b64decode(payload.get("body_b64") or "")
        return Response(
            status=int(payload.get("status") or 0),
            url=str(payload.get("url") or url),
            headers=dict(payload.get("headers") or {}),
            body=body,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            transport="cdp_browser",
        )


def _wait_for_origin(client: CDP, host: str, timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    expression = (
        "({ready: document.readyState, host: location.hostname, "
        "href: location.href})"
    )
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            result = client.call(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            )
        except (AgentWebError, *CDP_CONNECTION_ERRORS):
            time.sleep(0.5)
            continue
        last = (result.get("result") or {}).get("value") or {}
        current = str(last.get("host") or "").lower()
        if last.get("ready") == "complete" and (
            current == host or current.endswith("." + host)
        ):
            return
        time.sleep(0.5)
    raise AgentWebError(
        f"Browser did not reach {host} for the read-back "
        f"(last page: {last.get('href') or 'unknown'})"
    )


def browser_execute(
    runtime: Runtime,
    site: str,
    operation: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Run one typed read operation inside the authenticated browser via CDP.

    Returns the normal AgentWeb response envelope. Raises if no saved browser
    session exists for the site (the caller should run ``agentweb connect`` first)
    or if Chrome cannot be started.
    """
    resolved = runtime.resolve(site).site
    manifest = runtime.describe(resolved)
    base_url = str(manifest.get("base_url") or "")
    if not base_url:
        raise AgentWebError(f"{resolved} does not declare a base_url")
    cookie_domain = str(manifest.get("cookie_domain") or resolved)
    allowed_domains = tuple(str(d) for d in (manifest.get("allowed_domains") or []))

    from .web_runtime import WebRuntime

    # `agentweb connect` keys the persistent browser profile off the argument the
    # user typed (e.g. "linkedin.com"), while the adapter/cookie jar use the
    # resolved short name ("linkedin"). Probe both so a session captured either
    # way is found, without creating empty directories.
    profile_key: str | None = None
    for candidate in (site, resolved, cookie_domain.lstrip(".")):
        if not candidate:
            continue
        candidate_dir = (
            runtime.paths.profile_dir(candidate, runtime.profile)
            / "web-runtime"
            / "chrome-profile"
        )
        if candidate_dir.exists() and any(candidate_dir.iterdir()):
            profile_key = candidate
            break

    # No saved session yet. Rather than forcing an explicit `agentweb connect`,
    # seed a fresh per-site profile from the user's everyday Chrome (the same
    # default-browser reuse `connect` performs) so a member who is already
    # signed in on this machine just gets a browser tab that opens
    # authenticated. Only the target site's cookies are ever kept.
    seeded = False
    if profile_key is None:
        profile_key = resolved
        web = WebRuntime(
            runtime.paths,
            site=profile_key,
            profile=runtime.profile,
            base_url=base_url,
            cookie_domain=manifest.get("cookie_domain"),
            allowed_domains=manifest.get("allowed_domains"),
        )
        profile_dir: Path = web.chrome_profile
        web.stop()
        if use_default_browser_enabled():
            result = seed_profile_from_default_browser(profile_dir)
            seeded = bool(result.get("seeded"))
        if not seeded:
            raise AgentWebError(
                f"No saved browser session for {resolved} and no default Chrome "
                f"profile to reuse. Run `agentweb connect {site}` to sign in once.",
                code="browser_session_missing",
            )
    else:
        web = WebRuntime(
            runtime.paths,
            site=profile_key,
            profile=runtime.profile,
            base_url=base_url,
            cookie_domain=manifest.get("cookie_domain"),
            allowed_domains=manifest.get("allowed_domains"),
        )
        profile_dir = web.chrome_profile
        # A browser must not hold the profile while we attach to it.
        web.stop()
    stale = profile_dir / "DevToolsActivePort"
    if stale.exists():
        try:
            stale.unlink()
        except OSError:
            pass

    parsed_base = urlparse(base_url)
    origin = f"{parsed_base.scheme}://{parsed_base.netloc}" if base_url else base_url
    process: subprocess.Popen[Any] | None = None
    client: CDP | None = None
    try:
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
            origin or base_url,
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        client = open_debugger_client(port, cookie_domain, timeout=25, process=process)
        bound_client = client
        _wait_for_origin(bound_client, cookie_domain.lstrip("."))

        def session_factory(bound_site: str) -> BrowserSession:
            session = BrowserSession(
                runtime.paths,
                bound_site,
                runtime.profile,
                client=bound_client,
                allowed_domains=allowed_domains,
                fresh=runtime.fresh,
                cancellation=runtime.cancellation,
            )
            session.sync_from_browser()
            return session

        previous_override = runtime._session_override
        runtime._session_override = session_factory
        try:
            # Route through the normal execute() path so the browser response
            # flows through the same envelope, contract checks and data budget.
            return runtime.execute(resolved, operation, arguments)
        finally:
            runtime._session_override = previous_override
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
