from __future__ import annotations

import base64
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from .logs import logger
from .sdk import AgentWebError, HttpSession
from .storage import StatePaths, read_json, write_json

INSPECT_SCRIPT = r"""
(() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const roots = [document];
  for (let index = 0; index < roots.length; index++) {
    for (const element of roots[index].querySelectorAll('*')) {
      if (element.shadowRoot && !roots.includes(element.shadowRoot)) roots.push(element.shadowRoot);
    }
  }
  const selector = 'a[href],button,input,textarea,select,[role="button"],[role="link"],[contenteditable="true"],[tabindex],[ng-click],[data-ng-click],[onclick],summary,form';
  const candidates = roots.flatMap(root => Array.from(root.querySelectorAll(selector)))
    .filter((item, index, all) => all.indexOf(item) === index)
    .filter(visible).slice(0, 300);
  const controls = candidates.map((el, index) => {
    const ref = `__AGENTWEB_REF_PREFIX__${index + 1}`;
    el.setAttribute('data-agentweb-ref', ref);
    const labels = el.labels ? Array.from(el.labels).map(x => x.innerText).join(' ') : '';
    const aria = el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || '';
    const password = (el.type || '').toLowerCase() === 'password';
    const text = (labels || aria || el.innerText || (password ? '' : el.value) || el.placeholder || el.title || '').trim().replace(/\s+/g, ' ').slice(0, 500);
    const value = password ? '[redacted]' : (el.value || null);
    const attributes = Object.fromEntries(Array.from(el.attributes || [])
      .filter(attr => !/^(?:value|src|style)$/i.test(attr.name))
      .filter(attr => !/(?:password|secret|token|cookie|authorization)/i.test(`${attr.name} ${attr.value}`))
      .slice(0, 40)
      .map(attr => [attr.name, String(attr.value).slice(0, 300)]));
    return {
      ref,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role'),
      type: el.getAttribute('type'),
      name: el.getAttribute('name'),
      autocomplete: el.getAttribute('autocomplete'),
      text,
      href: el.href || null,
      value,
      checked: typeof el.checked === 'boolean' ? el.checked : null,
      disabled: !!el.disabled,
      attributes,
      frame_url: location.href,
      options: el.tagName === 'SELECT' ? Array.from(el.options).slice(0, 100).map(o => ({value:o.value, text:o.text, selected:o.selected})) : null
    };
  });
  const pageText = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').trim();
  return {
    url: location.href,
    title: document.title,
    text: pageText.slice(0, 30000),
    text_truncated: pageText.length > 30000,
    controls,
    control_count: controls.length
  };
})()
"""


FIND_REF_SCRIPT = r"""
(() => {
  const target = __AGENTWEB_REF__;
  const roots = [document];
  for (let index = 0; index < roots.length; index++) {
    const direct = roots[index].querySelector(`[data-agentweb-ref="${CSS.escape(target)}"]`);
    if (direct) return direct;
    for (const element of roots[index].querySelectorAll('*')) {
      if (element.shadowRoot && !roots.includes(element.shadowRoot)) roots.push(element.shadowRoot);
    }
  }
  return null;
})()
"""


@dataclass
class WebSession:
    site: str
    profile: str
    port: int
    pid: int
    visible: bool
    profile_dir: str
    started_at: float
    active_target_id: str | None = None


class WebRuntime:
    def __init__(
        self,
        paths: StatePaths,
        *,
        site: str,
        profile: str,
        base_url: str,
        cookie_domain: str | None = None,
        allowed_domains: list[str] | None = None,
        root: Path | None = None,
    ) -> None:
        self.paths = paths
        self.site = site
        self.profile = profile
        self.base_url = base_url
        base_host = (urlparse(base_url).hostname or "").lower()
        cookie_host = (cookie_domain or base_host).lstrip(".").lower()
        self.allowed_domains = sorted(
            {value.lstrip(".").lower() for value in (allowed_domains or [])}
            | {base_host, cookie_host}
        )
        self.root = root or (paths.profile_dir(site, profile) / "web-runtime")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.metadata_path = self.root / "session.json"
        self.chrome_profile = self.root / "chrome-profile"

    def _validate_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise AgentWebError("web URL must use http or https")
        host = parsed.hostname.lower()
        if not any(
            host == allowed or host.endswith("." + allowed)
            for allowed in self.allowed_domains
        ):
            raise AgentWebError(
                f"Navigation to {host!r} is outside the {self.site} adapter's allowed domains"
            )
        return url

    def _read_session(self) -> WebSession | None:
        payload = read_json(self.metadata_path, None)
        if not payload:
            return None
        try:
            return WebSession(**payload)
        except TypeError:
            return None

    @staticmethod
    def _port_alive(port: int) -> bool:
        try:
            with urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=0.5
            ) as response:
                return response.status == 200
        except Exception:
            logger.debug("Browser debugger port %s not reachable", port, exc_info=True)
            return False

    def _require_session(self) -> WebSession:
        session = self._read_session()
        if not session or not self._port_alive(session.port):
            raise AgentWebError(
                f"No active {self.site} web session. Call {self.site}.web_start first."
            )
        return session

    def _client(
        self,
        session: WebSession,
        *,
        process: subprocess.Popen[Any] | None = None,
        timeout: float = 5,
    ) -> Any:
        from .connector import CDP, page_target, wait_for_debugger

        targets = wait_for_debugger(
            session.port,
            timeout=timeout,
            process=process,
        )
        target = next(
            (item for item in targets if item.get("id") == session.active_target_id),
            None,
        )
        if target is None:
            target = page_target(targets, self.allowed_domains[0])
            session.active_target_id = target.get("id")
            write_json(self.metadata_path, session.__dict__)
        client = CDP(target["webSocketDebuggerUrl"])
        client.call("Network.enable")
        return client

    @staticmethod
    def _frame_ids(client: Any) -> list[str]:
        tree = client.call("Page.getFrameTree").get("frameTree") or {}
        result: list[str] = []

        def visit(node: dict[str, Any]) -> None:
            frame_id = (node.get("frame") or {}).get("id")
            if frame_id:
                result.append(frame_id)
            for child in node.get("childFrames") or []:
                visit(child)

        visit(tree)
        return result

    @classmethod
    def _frame_contexts(cls, client: Any) -> list[tuple[str, int]]:
        contexts: list[tuple[str, int]] = []
        for frame_id in cls._frame_ids(client):
            try:
                created = client.call(
                    "Page.createIsolatedWorld",
                    {
                        "frameId": frame_id,
                        "worldName": "agentweb",
                        "grantUniveralAccess": False,
                    },
                )
            except AgentWebError:
                continue
            context_id = created.get("executionContextId")
            if context_id:
                contexts.append((frame_id, int(context_id)))
        return contexts

    def _allowed_frame_contexts(self, client: Any) -> list[tuple[str, int]]:
        result: list[tuple[str, int]] = []
        for frame_id, context_id in self._frame_contexts(client):
            try:
                frame_url = str(
                    self._evaluate(client, "location.href", context_id=context_id) or ""
                )
                if frame_url == "about:blank":
                    continue
                self._validate_url(frame_url)
            except AgentWebError:
                continue
            result.append((frame_id, context_id))
        return result

    def _sync_http_cookies_into_browser(self, client: Any) -> int:
        http = HttpSession(self.paths, self.site, self.profile)
        browser_cookies = client.call("Network.getAllCookies").get("cookies", [])
        existing = {
            (
                str(item.get("name") or ""),
                str(item.get("domain") or ""),
                str(item.get("path") or "/"),
            )
            for item in browser_cookies
        }
        cookies = []
        for cookie in http.cookies:
            # CookieJar can retain host-only duplicates without a domain after
            # a browser import. CDP rejects the entire Network.setCookies call
            # when even one entry is malformed, so ignore those unusable
            # duplicates instead of preventing the valid session cookies from
            # reaching the persistent browser.
            if not cookie.name or not cookie.domain:
                continue
            if (cookie.name, cookie.domain, cookie.path or "/") in existing:
                continue
            value: dict[str, Any] = {
                "name": cookie.name,
                "value": cookie.value,
                "path": cookie.path or "/",
                "secure": cookie.secure,
            }
            if cookie.name.startswith("__Host-"):
                # __Host- cookies are expressly host-only. Supplying a Domain
                # attribute makes Chrome reject them.
                value["url"] = f"https://{cookie.domain.lstrip('.')}"
            else:
                value["domain"] = cookie.domain
            if cookie.expires:
                value["expires"] = cookie.expires
            cookies.append(value)
        if cookies:
            client.call("Network.setCookies", {"cookies": cookies})
        return len(cookies)

    def _sync_browser_cookies_into_http(self, client: Any) -> int:
        from http.cookiejar import Cookie

        result = client.call("Network.getAllCookies")
        http = HttpSession(self.paths, self.site, self.profile)
        count = 0
        for item in result.get("cookies", []):
            domain = str(item.get("domain") or "")
            host = domain.lstrip(".").lower()
            if not any(
                host == allowed or host.endswith("." + allowed)
                for allowed in self.allowed_domains
            ):
                continue
            expires = item.get("expires")
            persistent = bool(expires and float(expires) > 0)
            http.cookies.set_cookie(
                Cookie(
                    version=0,
                    name=str(item.get("name", "")),
                    value=str(item.get("value", "")),
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=True,
                    domain_initial_dot=domain.startswith("."),
                    path=str(item.get("path") or "/"),
                    path_specified=True,
                    secure=bool(item.get("secure")),
                    expires=int(float(expires)) if persistent else None,
                    discard=not persistent,
                    comment=None,
                    comment_url=None,
                    rest={
                        "HttpOnly": item.get("httpOnly"),
                        "SameSite": item.get("sameSite"),
                    },
                    rfc2109=False,
                )
            )
            count += 1
        http.save_cookies()
        return count

    def start(self, url: str | None = None, visible: bool = False) -> dict[str, Any]:
        from .connector import available_port, chrome_executable

        existing = self._read_session()
        if existing and self._port_alive(existing.port):
            if visible and not existing.visible:
                self.stop()
            else:
                return {**self.status(), "already_running": True}
        target = self._validate_url(url or self.base_url)
        port = available_port()
        self.chrome_profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        stale_debug_file = self.chrome_profile / "DevToolsActivePort"
        if stale_debug_file.exists():
            try:
                stale_debug_file.unlink()
            except OSError:
                pass
        command = [
            chrome_executable(),
            f"--user-data-dir={self.chrome_profile}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--window-size=1440,1000",
        ]
        if not visible:
            command.append("--headless=new")
        command.append(target)
        launch_log_path = self.root / "browser-launch.log"
        with launch_log_path.open("ab") as launch_log:
            process = subprocess.Popen(
                command,
                stdout=launch_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        session = WebSession(
            site=self.site,
            profile=self.profile,
            port=port,
            pid=process.pid,
            visible=visible,
            profile_dir=str(self.chrome_profile),
            started_at=time.time(),
        )
        write_json(self.metadata_path, session.__dict__)
        client: Any | None = None
        try:
            client = self._client(session, process=process, timeout=20)
            client.call("Page.enable")
            imported = self._sync_http_cookies_into_browser(client)
            client.call("Page.navigate", {"url": target})
            self._wait_ready(client, 15, expected_url=target)
            inspected = self._inspect_when_ready(client)
        except Exception:
            logger.debug("Web runtime startup failed; will retry once", exc_info=True)
            self._terminate(session)
            if not getattr(self, "_startup_retry_active", False):
                self._startup_retry_active = True
                try:
                    return self.start(url=url, visible=visible)
                finally:
                    self._startup_retry_active = False
            raise
        finally:
            if client:
                client.close()
        return {
            "operation": f"{self.site}.web_start",
            "active": True,
            "visible": visible,
            "human_interaction_available": visible,
            "cookies_imported": imported,
            "page": inspected,
            "already_running": False,
        }

    @staticmethod
    def _evaluate(
        client: Any, expression: str, *, context_id: int | None = None
    ) -> Any:
        parameters: dict[str, Any] = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }
        if context_id is not None:
            parameters["contextId"] = context_id
        result = client.call(
            "Runtime.evaluate",
            parameters,
        )
        exception = result.get("exceptionDetails")
        if exception:
            raise AgentWebError(
                "Website action failed: "
                + str(
                    (exception.get("exception") or {}).get("description")
                    or exception.get("text")
                )
            )
        return (result.get("result") or {}).get("value")

    def _evaluate_ref(self, client: Any, ref: str, action: str) -> Any:
        if not ref.startswith(("aw-", "sp-")):
            raise AgentWebError("control ref must come from web_inspect")
        find = FIND_REF_SCRIPT.replace("__AGENTWEB_REF__", json.dumps(ref))
        for _frame_id, context_id in self._allowed_frame_contexts(client):
            found = self._evaluate(client, f"Boolean({find})", context_id=context_id)
            if found:
                expression = (
                    "(() => { const el="
                    + find
                    + "; if(!el) throw new Error('control ref is stale; inspect again'); "
                    + action
                    + " })()"
                )
                return self._evaluate(client, expression, context_id=context_id)
        raise AgentWebError("control ref is stale; inspect again")

    def _node_id_for_ref(self, client: Any, ref: str) -> int:
        client.call("DOM.enable")
        find = FIND_REF_SCRIPT.replace("__AGENTWEB_REF__", json.dumps(ref))
        for _frame_id, context_id in self._allowed_frame_contexts(client):
            evaluated = client.call(
                "Runtime.evaluate",
                {
                    "expression": find,
                    "returnByValue": False,
                    "contextId": context_id,
                },
            )
            remote = evaluated.get("result") or {}
            object_id = remote.get("objectId")
            if remote.get("subtype") == "null" or not object_id:
                continue
            node = client.call("DOM.requestNode", {"objectId": object_id})
            if node.get("nodeId"):
                return int(node["nodeId"])
        raise AgentWebError("control ref is stale; inspect again")

    def _control_center(self, client: Any, ref: str) -> dict[str, float]:
        value = self._evaluate_ref(
            client,
            ref,
            "el.scrollIntoView({block:'center',inline:'center'}); const r=el.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2};",
        )
        if not isinstance(value, dict):
            raise AgentWebError("could not determine control coordinates")
        return {"x": float(value["x"]), "y": float(value["y"])}

    @staticmethod
    def _wait_ready(
        client: Any, timeout_seconds: int, expected_url: str | None = None
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            state = WebRuntime._evaluate(client, "document.readyState")
            current_url = str(WebRuntime._evaluate(client, "location.href") or "")
            parsed_url = urlparse(current_url)
            navigation_started = not expected_url or (
                parsed_url.scheme in {"http", "https"} and bool(parsed_url.hostname)
            )
            if state in {"interactive", "complete"} and navigation_started:
                return
            time.sleep(0.1)
        raise AgentWebError("Website page did not become ready before timeout")

    def _inspect(
        self,
        client: Any,
        *,
        query: str | None = None,
        limit: int = 80,
        max_text_chars: int = 10000,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 300:
            raise AgentWebError("web inspect limit must be between 1 and 300")
        if max_text_chars < 500 or max_text_chars > 30000:
            raise AgentWebError("max_text_chars must be between 500 and 30000")
        frames: list[dict[str, Any]] = []
        controls: list[dict[str, Any]] = []
        for frame_index, (_frame_id, context_id) in enumerate(
            self._allowed_frame_contexts(client), 1
        ):
            script = INSPECT_SCRIPT.replace(
                "__AGENTWEB_REF_PREFIX__", f"aw-{frame_index}-"
            )
            try:
                frame = self._evaluate(client, script, context_id=context_id)
            except AgentWebError:
                continue
            if isinstance(frame, dict):
                frames.append(frame)
                controls.extend(frame.get("controls") or [])
        if not frames:
            raise AgentWebError("Could not inspect the website DOM")
        value = dict(frames[0])
        current_url = value.get("url")
        if current_url:
            self._validate_url(current_url)
        value["frame_count"] = len(frames)
        value["frame_urls"] = [frame.get("url") for frame in frames]
        if len(frames) > 1:
            frame_text = " ".join(
                f"[Embedded frame {frame.get('url')}] {frame.get('text') or ''}"
                for frame in frames[1:]
            )
            value["text"] = f"{value.get('text') or ''} {frame_text}".strip()
        value["control_count_total"] = len(controls)
        all_controls = controls
        if query:
            needle = query.lower()
            controls = [
                item
                for item in controls
                if needle
                in " ".join(
                    str(item.get(key) or "")
                    for key in ("text", "name", "type", "role", "href")
                ).lower()
            ]
        value["controls"] = controls[:limit]
        value["control_count"] = len(value["controls"])
        value["controls_truncated"] = len(controls) > limit
        text = str(value.get("text") or "")
        value["text"] = text[:max_text_chars]
        value["text_truncated"] = (
            bool(value.get("text_truncated")) or len(text) > max_text_chars
        )
        lower_text = text.lower()
        interaction_controls = [
            item
            for item in all_controls
            if item.get("tag") in {"input", "textarea", "select", "button", "form"}
        ]
        control_text = " ".join(
            " ".join(
                str(item.get(key) or "")
                for key in ("text", "name", "type", "autocomplete")
            )
            for item in interaction_controls
        ).lower()
        title_and_url = f"{value.get('title') or ''} {value.get('url') or ''}".lower()
        handoff = None
        if (value.get("title") or "").strip().lower() in {
            "sorry.",
            "access denied",
            "just a moment...",
        } or re.search(
            r"access denied|request blocked|unusual traffic|automated queries",
            f"{title_and_url} {lower_text[:1500]}",
        ):
            handoff = "site challenge or automation block"
        elif re.search(
            r"captcha|verify (?:that )?you are human|robot check|security check|"
            r"type (?:the )?characters (?:you see|shown)|characters in the image",
            f"{title_and_url} {control_text} {lower_text}",
        ):
            handoff = "CAPTCHA or human-verification challenge"
        elif re.search(
            r"one[- ]time (?:password|code)|verification code|two[- ]factor|authenticator|passkey",
            control_text,
        ):
            handoff = "OTP, two-factor authentication, or passkey"
        elif any(
            str(item.get("type") or "").lower() == "password" for item in all_controls
        ):
            handoff = "password entry"
        elif re.search(
            r"card number|payment method|confirm (?:your )?(?:purchase|payment)|place your order",
            f"{title_and_url} {control_text}",
        ):
            handoff = "payment or purchase confirmation"
        value["human_interaction"] = {
            "required": handoff is not None,
            "reason": handoff,
            "instruction": (
                "Call web_start with visible=true and ask the user to complete this step; then continue through AgentWeb."
                if handoff
                else None
            ),
        }
        return value

    def _inspect_when_ready(
        self,
        client: Any,
        *,
        query: str | None = None,
        limit: int = 80,
        max_text_chars: int = 10000,
        timeout: float = 5,
    ) -> dict[str, Any]:
        """Give client-rendered pages a bounded chance to expose meaningful DOM."""
        deadline = time.monotonic() + max(timeout, 0)
        while True:
            page = self._inspect(
                client,
                query=query,
                limit=limit,
                max_text_chars=max_text_chars,
            )
            if page.get("text") or page.get("control_count_total"):
                return page
            if time.monotonic() >= deadline:
                return page
            time.sleep(0.25)

    def inspect(
        self,
        query: str | None = None,
        limit: int = 80,
        max_text_chars: int = 10000,
    ) -> dict[str, Any]:
        session = self._require_session()
        client = self._client(session)
        try:
            return {
                "operation": f"{self.site}.web_inspect",
                "active": True,
                "page": self._inspect_when_ready(
                    client,
                    query=query,
                    limit=limit,
                    max_text_chars=max_text_chars,
                ),
            }
        finally:
            client.close()

    def _confirm_gate(
        self, client: Any, ref: Any, index: int, confirm: bool
    ) -> None:
        risk = self._control_risk(client, str(ref or ""))
        if risk and not confirm:
            raise AgentWebError(
                f"web step {index + 1} may change website state ({risk}); repeat with confirm=true"
            )

    @staticmethod
    def _step_text(step: dict[str, Any], index: int, kind: str) -> str:
        if "text" in step and "value" in step and step["text"] != step["value"]:
            raise AgentWebError(
                f"{kind} action received conflicting text and value fields",
                code="invalid_web_action",
                field=f"steps.{index}.text",
            )
        if "text" not in step and "value" not in step:
            raise AgentWebError(
                f"{kind} action requires a text field",
                code="missing_input",
                field=f"steps.{index}.text",
            )
        return str(step.get("text", step.get("value")) or "")

    def _step_goto(self, client: Any, step: dict[str, Any]) -> Any:
        url = self._validate_url(str(step.get("url") or ""))
        client.call("Page.navigate", {"url": url})
        self._wait_ready(
            client,
            int(step.get("timeout_seconds", 20)),
            expected_url=url,
        )
        return {"url": url}

    def _step_history(self, client: Any, step: dict[str, Any], kind: str) -> Any:
        history = client.call("Page.getNavigationHistory")
        entries = history.get("entries") or []
        current = int(history.get("currentIndex", 0))
        target_index = current - 1 if kind == "back" else current + 1
        if target_index < 0 or target_index >= len(entries):
            raise AgentWebError(
                f"cannot navigate {kind}; no history entry is available"
            )
        target_entry = entries[target_index]
        self._validate_url(str(target_entry.get("url") or ""))
        client.call(
            "Page.navigateToHistoryEntry", {"entryId": target_entry["id"]}
        )
        self._wait_ready(client, int(step.get("timeout_seconds", 20)))
        return {"url": target_entry.get("url")}

    def _step_fill(self, client: Any, step: dict[str, Any], index: int) -> Any:
        value = json.dumps(self._step_text(step, index, "fill"))
        return self._evaluate_ref(
            client,
            str(step.get("ref") or ""),
            f"el.focus(); if(el.isContentEditable){{el.textContent={value};}}else{{const proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype; const setter=Object.getOwnPropertyDescriptor(proto,'value'); if(setter&&setter.set) setter.set.call(el,{value}); else el.value={value};}} el.dispatchEvent(new InputEvent('input',{{bubbles:true,inputType:'insertText',data:{value}}})); el.dispatchEvent(new Event('change',{{bubbles:true}})); el.dispatchEvent(new KeyboardEvent('keyup',{{bubbles:true,key:'Unidentified'}})); return true;",
        )

    def _step_type(self, client: Any, step: dict[str, Any], index: int) -> Any:
        typed = self._step_text(step, index, "type")
        if len(typed) > 5000:
            raise AgentWebError("typed text cannot exceed 5000 characters")
        self._evaluate_ref(
            client,
            str(step.get("ref") or ""),
            "el.focus(); return true;",
        )
        if step.get("clear", True):
            self._evaluate_ref(
                client,
                str(step.get("ref") or ""),
                "if(el.isContentEditable) el.textContent=''; else el.value=''; el.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'deleteContentBackward',data:null})); return true;",
            )
        for character in typed:
            client.call(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyDown",
                    "key": character,
                    "text": character,
                    "unmodifiedText": character,
                },
            )
            client.call(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", "key": character},
            )
        return {"characters": len(typed)}

    _PRESS_NAMED_KEYS = frozenset(
        {
            "Enter",
            "Escape",
            "Tab",
            "ArrowUp",
            "ArrowDown",
            "ArrowLeft",
            "ArrowRight",
            "Backspace",
            "Delete",
            " ",
            "Home",
            "End",
            "PageUp",
            "PageDown",
            "Insert",
        }
    )

    def _step_press(
        self, client: Any, step: dict[str, Any], index: int, confirm: bool
    ) -> Any:
        key = str(step.get("key") or "")
        if key not in self._PRESS_NAMED_KEYS and not re.fullmatch(
            r"F(?:[1-9]|1[0-2])|.", key
        ):
            raise AgentWebError("press key is not allowed")
        ref = step.get("ref")
        if ref:
            if key == "Enter":
                self._confirm_gate(client, ref, index, confirm)
            self._evaluate_ref(client, str(ref), "el.focus(); return true;")
        modifiers = (
            (1 if step.get("alt") else 0)
            | (2 if step.get("ctrl") else 0)
            | (4 if step.get("meta") else 0)
            | (8 if step.get("shift") else 0)
        )
        client.call(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "key": key, "modifiers": modifiers},
        )
        client.call(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": key, "modifiers": modifiers},
        )
        return True

    def _step_upload(
        self, client: Any, step: dict[str, Any], index: int, confirm: bool
    ) -> Any:
        if not confirm:
            raise AgentWebError(
                f"web step {index + 1} can upload data to the website; repeat with confirm=true"
            )
        upload = Path(str(step.get("path") or "")).expanduser().resolve()
        if not upload.is_file():
            raise AgentWebError("upload path must be an existing file")
        node_id = self._node_id_for_ref(client, str(step.get("ref") or ""))
        client.call(
            "DOM.setFileInputFiles",
            {"nodeId": node_id, "files": [str(upload)]},
        )
        return {"filename": upload.name, "bytes": upload.stat().st_size}

    def _step_drag(
        self, client: Any, step: dict[str, Any], index: int, confirm: bool
    ) -> Any:
        if not confirm:
            raise AgentWebError(
                f"web step {index + 1} may reorder or change website state; repeat with confirm=true"
            )
        source = self._control_center(client, str(step.get("ref") or ""))
        if step.get("target_ref"):
            target = self._control_center(
                client, str(step.get("target_ref"))
            )
        else:
            target = {
                "x": float(step.get("x", source["x"])),
                "y": float(step.get("y", source["y"])),
            }
        client.call(
            "Input.dispatchMouseEvent", {"type": "mouseMoved", **source}
        )
        client.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "button": "left",
                "clickCount": 1,
                **source,
            },
        )
        for position in range(1, 11):
            ratio = position / 10
            client.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "button": "left",
                    "buttons": 1,
                    "x": source["x"] + (target["x"] - source["x"]) * ratio,
                    "y": source["y"] + (target["y"] - source["y"]) * ratio,
                },
            )
        client.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "button": "left",
                "clickCount": 1,
                **target,
            },
        )
        return {"from": source, "to": target}

    def _step_scroll(self, client: Any, step: dict[str, Any]) -> Any:
        if step.get("ref"):
            return self._evaluate_ref(
                client,
                str(step.get("ref")),
                "el.scrollIntoView({block:'center',behavior:'instant'}); return true;",
            )
        delta_y = int(step.get("delta_y", 700))
        if abs(delta_y) > 10000:
            raise AgentWebError("scroll delta_y cannot exceed 10000")
        return self._evaluate(
            client, f"window.scrollBy(0,{delta_y}); window.scrollY"
        )

    def _step_screenshot(self, client: Any, step: dict[str, Any]) -> Any:
        requested = step.get("path")
        if requested:
            screenshot_path = Path(str(requested)).expanduser().resolve()
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            screenshot_dir = self.root / "screenshots"
            screenshot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            screenshot_path = (
                screenshot_dir / f"page-{int(time.time() * 1000)}.png"
            )
        captured = client.call(
            "Page.captureScreenshot",
            {
                "format": "png",
                "captureBeyondViewport": bool(step.get("full_page", False)),
            },
        )
        screenshot_path.write_bytes(base64.b64decode(captured["data"]))
        return {
            "path": str(screenshot_path),
            "bytes": screenshot_path.stat().st_size,
        }

    def _step_download(
        self, client: Any, step: dict[str, Any], index: int, confirm: bool
    ) -> Any:
        download_dir = (
            Path(
                str(
                    step.get("output_directory")
                    or (self.root / "downloads")
                )
            )
            .expanduser()
            .resolve()
        )
        download_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        before = {
            item.name for item in download_dir.iterdir() if item.is_file()
        }
        client.call(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": str(download_dir),
                "eventsEnabled": True,
            },
        )
        self._confirm_gate(client, step.get("ref"), index, confirm)
        self._evaluate_ref(
            client, str(step.get("ref") or ""), "el.click(); return true;"
        )
        deadline = time.monotonic() + min(
            max(int(step.get("timeout_seconds", 30)), 1), 120
        )
        completed_files: list[Path] = []
        while time.monotonic() < deadline:
            completed_files = [
                item
                for item in download_dir.iterdir()
                if item.is_file()
                and item.name not in before
                and not item.name.endswith((".crdownload", ".tmp"))
            ]
            partial = any(
                item.name.endswith((".crdownload", ".tmp"))
                for item in download_dir.iterdir()
            )
            if completed_files and not partial:
                break
            time.sleep(0.2)
        if not completed_files:
            raise AgentWebError("download did not complete before timeout")
        return {
            "files": [
                {"path": str(item), "bytes": item.stat().st_size}
                for item in completed_files
            ]
        }

    def _step_dialog(
        self, client: Any, step: dict[str, Any], index: int, confirm: bool
    ) -> Any:
        accept = bool(step.get("accept", True))
        if accept and not confirm:
            raise AgentWebError(
                f"web step {index + 1} accepts a website confirmation dialog; repeat with confirm=true"
            )
        params: dict[str, Any] = {"accept": accept}
        if step.get("prompt_text") is not None:
            params["promptText"] = str(step.get("prompt_text"))
        client.call("Page.handleJavaScriptDialog", params)
        return {"accepted": accept}

    @staticmethod
    def _step_wait(step: dict[str, Any], index: int) -> Any:
        if (
            "ms" in step
            and "milliseconds" in step
            and step["ms"] != step["milliseconds"]
        ):
            raise AgentWebError(
                "wait action received conflicting ms and milliseconds fields",
                code="invalid_web_action",
                field=f"steps.{index}.ms",
            )
        raw_ms = step.get("ms", step.get("milliseconds", 500))
        milliseconds = int(500 if raw_ms is None else raw_ms)
        if milliseconds < 0 or milliseconds > 15000:
            raise AgentWebError(
                "wait milliseconds must be between 0 and 15000"
            )
        time.sleep(milliseconds / 1000)
        return {"waited_ms": milliseconds}

    def _execute_step(
        self, client: Any, step: dict[str, Any], index: int, confirm: bool
    ) -> Any:
        kind = step.get("action")
        ref = step.get("ref")
        if kind == "goto":
            return self._step_goto(client, step)
        if kind in {"back", "forward"}:
            return self._step_history(client, step, kind)
        if kind == "reload":
            client.call(
                "Page.reload",
                {"ignoreCache": bool(step.get("ignore_cache", False))},
            )
            self._wait_ready(client, int(step.get("timeout_seconds", 20)))
            return True
        if kind == "click":
            self._confirm_gate(client, ref, index, confirm)
            return self._evaluate_ref(
                client, str(ref or ""), "el.click(); return true;"
            )
        if kind == "double_click":
            self._confirm_gate(client, ref, index, confirm)
            return self._evaluate_ref(
                client,
                str(ref or ""),
                "el.dispatchEvent(new MouseEvent('dblclick',{bubbles:true,cancelable:true,view:window})); return true;",
            )
        if kind == "fill":
            return self._step_fill(client, step, index)
        if kind == "type":
            return self._step_type(client, step, index)
        if kind == "select":
            self._confirm_gate(client, ref, index, confirm)
            value = json.dumps(str(step.get("value") or ""))
            return self._evaluate_ref(
                client,
                str(ref or ""),
                f"el.value={value}; el.dispatchEvent(new Event('change',{{bubbles:true}})); return el.value;",
            )
        if kind in {"check", "uncheck"}:
            self._confirm_gate(client, ref, index, confirm)
            checked = "true" if kind == "check" else "false"
            return self._evaluate_ref(
                client,
                str(ref or ""),
                f"const desired={checked}; if(el.checked!==desired){{if(!desired&&el.type==='radio') throw new Error('radio buttons cannot be unchecked directly; check another option'); el.click();}} return el.checked;",
            )
        if kind == "submit":
            self._confirm_gate(client, ref, index, confirm)
            return self._evaluate_ref(
                client,
                str(ref or ""),
                "const form=el.tagName==='FORM'?el:el.form; if(!form) throw new Error('control has no form'); form.requestSubmit(); return true;",
            )
        if kind in {"focus", "blur"}:
            return self._evaluate_ref(
                client,
                str(ref or ""),
                f"el.{kind}(); return true;",
            )
        if kind == "press":
            return self._step_press(client, step, index, confirm)
        if kind == "upload":
            return self._step_upload(client, step, index, confirm)
        if kind == "hover":
            return self._evaluate_ref(
                client,
                str(ref or ""),
                "el.scrollIntoView({block:'center'}); el.dispatchEvent(new PointerEvent('pointerover',{bubbles:true})); el.dispatchEvent(new MouseEvent('mouseover',{bubbles:true})); return true;",
            )
        if kind == "drag":
            return self._step_drag(client, step, index, confirm)
        if kind == "scroll":
            return self._step_scroll(client, step)
        if kind == "screenshot":
            return self._step_screenshot(client, step)
        if kind == "download":
            return self._step_download(client, step, index, confirm)
        if kind == "dialog":
            return self._step_dialog(client, step, index, confirm)
        if kind == "wait":
            return self._step_wait(step, index)
        raise AgentWebError(
            "web action must be goto, back, forward, reload, click, double_click, fill, type, select, check, uncheck, submit, focus, blur, press, upload, hover, drag, scroll, screenshot, download, dialog, or wait"
        )

    def _capture_trace(
        self,
        client: Any,
        steps: list[dict[str, Any]],
        capture_name: str,
        page_before: Any,
        page: Any,
    ) -> dict[str, Any]:
        from .capture import (
            capture_response_bodies,
            compile_network_trace,
            write_trace,
        )

        events = client.drain_events(timeout=0.5)
        response_bodies = capture_response_bodies(
            client,
            events,
            allowed_domains=self.allowed_domains,
        )
        page_after = page
        if page_after is None:
            try:
                page_after = self._inspect_when_ready(client)
            except AgentWebError:
                page_after = None
        trace = compile_network_trace(
            events,
            site=self.site,
            profile=self.profile,
            allowed_domains=self.allowed_domains,
            action_steps=steps,
            page_before=page_before,
            page_after=page_after,
            response_bodies=response_bodies,
        )
        # Keep the author's stable operation name inside the portable
        # trace. Batch compilation should not have to reverse-engineer
        # it from a timestamped filename.
        trace["operation"] = capture_name
        trace_path = write_trace(self.root / "captures", capture_name, trace)
        return {
            "path": str(trace_path),
            "request_count": trace["request_count"],
            "redacted": True,
            "response_bodies_captured": len(response_bodies),
            "endpoint_count": trace["compiler"]["endpoint_count"],
            "recipe_draft": trace["compiler"]["recipe_draft"],
            "review_required": trace["compiler"]["review_required"],
        }

    def action(
        self,
        steps: list[dict[str, Any]],
        inspect_after: bool = True,
        inspect_query: str | None = None,
        inspect_limit: int = 80,
        capture_name: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(steps, list) or not steps or len(steps) > 50:
            raise AgentWebError("steps must contain between 1 and 50 web actions")
        session = self._require_session()
        client = self._client(session)
        results = []
        try:
            client.call("Page.enable")
            page_before = self._inspect(client) if capture_name else None
            client.clear_events()
            for index, step in enumerate(steps):
                if not isinstance(step, dict):
                    raise AgentWebError(f"web step {index + 1} must be an object")
                kind = step.get("action")
                if step.get("target"):
                    step["ref"] = self._resolve_target_ref(
                        client,
                        dict(step["target"]),
                        step_number=index + 1,
                    )
                result = self._execute_step(client, step, index, confirm)
                wait_after = int(
                    step.get(
                        "wait_after_ms",
                        500 if kind in {"click", "submit", "press"} else 0,
                    )
                )
                if wait_after:
                    time.sleep(min(max(wait_after, 0), 15000) / 1000)
                results.append({"step": index + 1, "action": kind, "result": result})
            cookies_saved = self._sync_browser_cookies_into_http(client)
            page = None
            inspection_error = None
            if inspect_after:
                try:
                    page = self._inspect_when_ready(
                        client, query=inspect_query, limit=inspect_limit
                    )
                except AgentWebError as exc:
                    # A successful form submission can replace or close the
                    # inspected target before Chrome exposes the next DOM.
                    # Network capture is still valuable and must not be lost.
                    inspection_error = str(exc)
            trace_result = None
            if capture_name:
                trace_result = self._capture_trace(
                    client, steps, capture_name, page_before, page
                )
            return {
                "operation": f"{self.site}.web_action",
                "completed_steps": len(results),
                "results": results,
                "cookies_saved": cookies_saved,
                "page": page,
                "inspection_error": inspection_error,
                "capture": trace_result,
            }
        finally:
            client.close()

    def _resolve_target_ref(
        self,
        client: Any,
        target: dict[str, Any],
        *,
        step_number: int,
    ) -> str:
        controls = self._inspect(client, limit=300, max_text_chars=500).get(
            "controls", []
        )

        def safe_url(value: str) -> str:
            parsed = urlparse(value)
            return parsed._replace(query="", fragment="").geturl()

        def matches(control: dict[str, Any]) -> bool:
            for key in ("tag", "type", "name", "role"):
                if key in target and control.get(key) != target.get(key):
                    return False
            if "href" in target and safe_url(str(control.get("href") or "")) != str(
                target["href"]
            ):
                return False
            if "value" in target and control.get("value") != target.get("value"):
                return False
            if "text" in target and control.get("text") != target.get("text"):
                return False
            return True

        candidates = [control for control in controls if matches(control)]
        if len(candidates) != 1:
            raise AgentWebError(
                f"web step {step_number} control changed or became ambiguous; inspect the page again",
                code="authoring_control_drift",
                retryable=True,
                next_action="web_inspect",
            )
        return str(candidates[0]["ref"])

    def _control_risk(self, client: Any, ref: str) -> str | None:
        details = self._evaluate_ref(
            client,
            ref,
            "const form=el.form || (el.tagName==='FORM'?el:null); return {text:(el.innerText||el.value||el.getAttribute('aria-label')||'').trim(), href:el.href||'', action:form?form.action:'', method:form?form.method:''};",
        )
        if str((details or {}).get("method") or "").lower() not in {"", "get"}:
            return "form submission"
        haystack = " ".join(
            str((details or {}).get(key) or "") for key in ("text", "href", "action")
        ).lower()
        match = re.search(
            r"\b(buy now|place (?:your )?order|confirm purchase|purchase|pay|checkout|delete|remove|cancel|close|reopen|approve|reject|accept|apply|submit|publish|post|send|merge|revert|transfer|refund|return|vote|flag|hide|unhide|favorite|unfavorite|star|unstar|watch|unwatch|follow|unfollow|subscribe|unsubscribe|pin|unpin|lock|unlock|enable|disable|archive|restore|block|report|resolve|dismiss|commit|deploy|run workflow|save changes|create|update)\b",
            haystack,
        )
        return match.group(1) if match else None

    def status(self) -> dict[str, Any]:
        session = self._read_session()
        active = bool(session and self._port_alive(session.port))
        return {
            "operation": f"{self.site}.web_status",
            "site": self.site,
            "profile": self.profile,
            "active": active,
            "visible": session.visible if active and session else None,
            "started_at_unix": session.started_at if active and session else None,
            "allowed_domains": self.allowed_domains,
            "active_target_id": session.active_target_id
            if active and session
            else None,
        }

    def tabs(self) -> dict[str, Any]:
        session = self._require_session()
        with urlopen(
            f"http://127.0.0.1:{session.port}/json/list", timeout=2
        ) as response:
            targets = json.load(response)
        tabs = [
            {
                "target_id": item.get("id"),
                "title": item.get("title"),
                "url": item.get("url"),
                "active": item.get("id") == session.active_target_id,
            }
            for item in targets
            if item.get("type") == "page"
        ]
        if tabs and not any(item["active"] for item in tabs):
            tabs[0]["active"] = True
        return {
            "operation": f"{self.site}.web_tabs",
            "count": len(tabs),
            "tabs": tabs,
        }

    def focus(self, target_id: str) -> dict[str, Any]:
        session = self._require_session()
        with urlopen(
            f"http://127.0.0.1:{session.port}/json/list", timeout=2
        ) as response:
            targets = json.load(response)
        target = next(
            (
                item
                for item in targets
                if item.get("id") == target_id and item.get("type") == "page"
            ),
            None,
        )
        if target is None:
            raise AgentWebError("target_id is not an open website tab")
        self._validate_url(str(target.get("url") or ""))
        session.active_target_id = target_id
        write_json(self.metadata_path, session.__dict__)
        client = self._client(session)
        try:
            client.call("Page.bringToFront")
            page = self._inspect_when_ready(client)
        finally:
            client.close()
        return {
            "operation": f"{self.site}.web_focus",
            "target_id": target_id,
            "page": page,
        }

    def new_tab(self, url: str | None = None) -> dict[str, Any]:
        session = self._require_session()
        target_url = self._validate_url(url or self.base_url)
        client = self._client(session)
        try:
            created = client.call("Target.createTarget", {"url": target_url})
        finally:
            client.close()
        target_id = str(created.get("targetId") or "")
        if not target_id:
            raise AgentWebError("Chrome did not create a new tab")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                return self.focus(target_id)
            except AgentWebError:
                time.sleep(0.1)
        raise AgentWebError("new website tab did not become available")

    def close_tab(
        self, target_id: str | None = None, confirm: bool = False
    ) -> dict[str, Any]:
        session = self._require_session()
        selected = target_id or session.active_target_id
        tabs = self.tabs()["tabs"]
        if not selected and tabs:
            selected = tabs[0]["target_id"]
        if not selected or not any(item["target_id"] == selected for item in tabs):
            raise AgentWebError("target_id is not an open website tab")
        if len(tabs) == 1 and not confirm:
            raise AgentWebError(
                "closing the last website tab ends the active page; repeat with confirm=true"
            )
        client = self._client(session)
        try:
            closed = client.call("Target.closeTarget", {"targetId": selected})
        finally:
            client.close()
        remaining = [item for item in tabs if item["target_id"] != selected]
        session.active_target_id = remaining[0]["target_id"] if remaining else None
        write_json(self.metadata_path, session.__dict__)
        return {
            "operation": f"{self.site}.web_close_tab",
            "closed": bool(closed.get("success")),
            "target_id": selected,
            "remaining_tabs": len(remaining),
        }

    def _terminate(self, session: WebSession) -> None:
        try:
            os.kill(session.pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            process_alive = True
            try:
                os.kill(session.pid, 0)
            except OSError:
                process_alive = False
            if not process_alive and not self._port_alive(session.port):
                break
            time.sleep(0.1)
        if self._port_alive(session.port):
            try:
                os.kill(session.pid, signal.SIGKILL)
            except OSError:
                pass
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and self._port_alive(session.port):
                time.sleep(0.1)
        try:
            self.metadata_path.unlink(missing_ok=True)
        except OSError:
            pass

    def stop(self) -> dict[str, Any]:
        session = self._read_session()
        if not session or not self._port_alive(session.port):
            if session:
                self._terminate(session)
            return {
                "operation": f"{self.site}.web_stop",
                "stopped": False,
                "already_stopped": True,
            }
        client = self._client(session)
        cookies_saved = 0
        try:
            cookies_saved = self._sync_browser_cookies_into_http(client)
            try:
                client.call("Browser.close")
            except Exception:
                logger.debug("Browser.close failed during web stop", exc_info=True)
        finally:
            try:
                client.close()
            except Exception:
                logger.debug("Failed to close CDP client during web stop", exc_info=True)
            self._terminate(session)
        return {
            "operation": f"{self.site}.web_stop",
            "stopped": True,
            "already_stopped": False,
            "cookies_saved": cookies_saved,
        }
