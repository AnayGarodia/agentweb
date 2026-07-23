from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from typing import Any

from . import __version__
from .connector import connection_handoff
from .runtime import Runtime
from .sdk import AuthenticationRequired, ConfigurationRequired, AgentWebError


TOOLS = [
    {
        "name": "sites_list",
        "description": "List website adapters and their canonical domains currently available through AgentWeb.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "site_describe",
        "description": "Get a compact operation catalog for one site. Then pass operation=<name> only for the complete contract you need; avoid loading every contract.",
        "inputSchema": {
            "type": "object",
            "required": ["site"],
            "properties": {
                "site": {
                    "type": "string",
                    "description": "Adapter name, domain, subdomain, or website URL",
                },
                "operation": {
                    "type": "string",
                    "description": "Return the complete contract for one operation",
                },
                "category": {"type": "string"},
                "query": {
                    "type": "string",
                    "description": "Search operation names and usage descriptions",
                },
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
                "include_parity_details": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include full verification command lists; false keeps agent context compact",
                },
            },
        },
    },
    {
        "name": "site_call",
        "description": "Use any compiled browserless website operation through AgentWeb. Inspect declared_gaps before promising full parity.",
        "inputSchema": {
            "type": "object",
            "required": ["operation", "arguments"],
            "properties": {
                "operation": {
                    "type": "string",
                    "description": "SITE.ACTION or DOMAIN.ACTION",
                },
                "arguments": {"type": "object"},
                "profile": {"type": "string", "default": "default"},
                "fresh": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "site_connect",
        "description": "Prepare a one-time human authorization handoff without opening or controlling a browser from the agent. Already-connected profiles return immediately.",
        "inputSchema": {
            "type": "object",
            "required": ["site"],
            "properties": {
                "site": {
                    "type": "string",
                    "description": "Adapter name, domain, subdomain, or website URL",
                },
                "mode": {
                    "type": "string",
                    "enum": ["login", "signup", "session"],
                    "default": "login",
                    "description": "Use login for an existing account, signup to create one, or session to refresh a public anti-bot session without signing in.",
                },
                "profile": {"type": "string", "default": "default"},
                "timeout_seconds": {"type": "integer", "default": 600},
            },
        },
    },
]


AGENT_INSTRUCTIONS = (
    "Use AgentWeb's compiled direct operations for mapped websites; ordinary replay must stay "
    "browserless. Do not claim an adapter is exhaustive unless parity.browserless_replay is true "
    "and parity.declared_gaps is empty. "
    "Adapter names, domains, subdomains, and full website URLs are accepted. Prefer canonical "
    "domains in new calls. Call sites_list when support is unknown. Call site_describe once for a compact catalog, "
    "then call it with operation=<name> only when you need that operation's full contract. "
    "site_call to execute them. Public operations never need login. If AgentWeb reports a human "
    "handoff for password, CAPTCHA, OTP/passkey, consent, or payment confirmation, ask the user "
    "to complete only the authorization required by the site. If "
    "authentication_required is returned, ask the user whether to log in or sign up, "
    "call site_connect once to obtain the human command, never run that command through an agent "
    "shell, and resume the original operation after the user completes it. Treat cart_scope=account as the "
    "meaning of 'my cart'; use cart_scope=anonymous only when the user explicitly accepts an "
    "isolated cart that will not appear in their normal browser."
)


def tool_surface() -> list[dict[str, Any]]:
    tools = deepcopy(TOOLS)
    try:
        names = sorted(Runtime().registry.installed())
    except Exception:
        names = []
    if names:
        mapped = ", ".join(names)
        tools[0]["description"] += f" Currently installed: {mapped}."
        tools[2]["description"] += f" Currently installed sites: {mapped}."
    return tools


def response(request_id: Any, result: Any = None, error: Any = None) -> dict[str, Any]:
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        value["error"] = error
    else:
        value["result"] = result
    return value


def dispatch(
    message: dict[str, Any], cancel_event: threading.Event | None = None
) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return response(
            request_id,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agentweb", "version": __version__},
                "instructions": AGENT_INSTRUCTIONS,
            },
        )
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "ping":
        return response(request_id, {})
    if method == "tools/list":
        return response(request_id, {"tools": tool_surface()})
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        runtime = Runtime(
            profile=arguments.pop("profile", "default"),
            fresh=bool(arguments.pop("fresh", False)),
            mapping_mode=False,
            cancel_event=cancel_event,
            interface="mcp",
        )
        try:
            if name == "sites_list":
                value = runtime.sites()
            elif name == "site_describe":
                site = arguments.pop("site")
                value = runtime.discover(site, **arguments)
            elif name == "site_call":
                value = runtime.call(
                    arguments["operation"], arguments.get("arguments") or {}
                )
            elif name == "site_connect":
                runtime.analytics.record(
                    "connection_requested",
                    site=runtime.resolve(arguments["site"]).site,
                    success=True,
                    interface="mcp",
                )
                value = connection_handoff(
                    runtime,
                    arguments["site"],
                    mode=arguments.get("mode", "login"),
                    timeout_seconds=int(arguments.get("timeout_seconds", 600)),
                )
            else:
                raise AgentWebError(f"Unknown tool {name!r}")
            return response(
                request_id,
                {"content": [{"type": "text", "text": json.dumps(value)}]},
            )
        except ConfigurationRequired as exc:
            return response(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": "configuration_required",
                                    "message": str(exc),
                                    "next_operation": exc.operation,
                                    "instruction": "Collect the required non-secret configuration from the user, call next_operation through site_call, then resume the original task.",
                                }
                            ),
                        }
                    ],
                    "isError": True,
                },
            )
        except AuthenticationRequired as exc:
            protected_site = str(arguments.get("operation", "")).split(".", 1)[
                0
            ] or arguments.get("site")
            protected_manifest = (
                runtime.describe(protected_site) if protected_site else {}
            )
            allowed_modes = ["login"]
            if protected_manifest.get("signup_url"):
                allowed_modes.append("signup")
            return response(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": "authentication_required",
                                    "message": str(exc),
                                    "next_tool": "site_connect",
                                    "site": protected_site,
                                    "allowed_modes": allowed_modes,
                                    "instruction": "Ask the user whether to sign in or create an account, call site_connect to get the one-time human command, and never run that command through an agent shell. Resume the original task after the user finishes it.",
                                }
                            ),
                        }
                    ],
                    "isError": True,
                },
            )
        except AgentWebError as exc:
            return response(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(exc.as_dict())}],
                    "isError": True,
                },
            )
        except Exception:
            return response(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": "internal_error",
                                    "message": "AgentWeb encountered an unexpected internal error.",
                                    "retryable": False,
                                }
                            ),
                        }
                    ],
                    "isError": True,
                },
            )
    return response(request_id, error={"code": -32601, "message": "Method not found"})


def serve() -> int:
    output_lock = threading.Lock()
    active_lock = threading.Lock()
    active: dict[Any, threading.Event] = {}

    def emit_result(result: dict[str, Any] | None) -> None:
        if result is None:
            return
        with output_lock:
            print(json.dumps(result, separators=(",", ":")), flush=True)

    def completed(request_id: Any, future: Future[dict[str, Any] | None]) -> None:
        with active_lock:
            active.pop(request_id, None)
        try:
            emit_result(future.result())
        except Exception as exc:
            emit_result(
                response(request_id, error={"code": -32603, "message": str(exc)})
            )

    with ThreadPoolExecutor(
        max_workers=8, thread_name_prefix="agentweb-mcp"
    ) as executor:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                if message.get("method") == "notifications/cancelled":
                    params = message.get("params") or {}
                    cancelled_id = params.get("requestId", params.get("request_id"))
                    with active_lock:
                        event = active.get(cancelled_id)
                    if event is not None:
                        event.set()
                    continue
                request_id = message.get("id")
                cancel_event = threading.Event()
                if request_id is not None:
                    with active_lock:
                        active[request_id] = cancel_event
                future = executor.submit(dispatch, message, cancel_event)
                future.add_done_callback(
                    lambda item, rid=request_id: completed(rid, item)
                )
            except Exception as exc:
                emit_result(response(None, error={"code": -32603, "message": str(exc)}))
    return 0
