from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .analytics import Analytics
from .capture import verify_flow_capsule
from .registry import Registry, validate_manifest
from .sdk import (
    AdapterContext,
    AgentWebError,
    AuthenticationRequired,
    CancellationToken,
    enforce_data_budget,
    map_operation_inputs,
    match_operation_response_error,
    normalize_operation_output,
    paginate_operation_output,
)
from .storage import (
    StatePaths,
    exclusive_path_lock,
    read_json,
    safe_component,
    write_json,
)
from .targets import (
    ResolvedTarget,
    canonical_domain,
    extract_resource,
    host_matches,
    normalized_host,
    target_url,
)
from .web_runtime import WebRuntime

WEB_COMMANDS: dict[str, dict[str, Any]] = {
    "web_start": {
        "description": "Start or resume AgentWeb's persistent internal browser for website-only actions. The agent stays in AgentWeb; visible=true allows human CAPTCHA, OTP, consent, or payment confirmation.",
        "cli": {"positionals": []},
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Optional allowed URL; defaults to the site home page",
                },
                "visible": {
                    "type": "boolean",
                    "default": False,
                    "description": "Show the browser only when human interaction may be required",
                },
            },
        },
    },
    "web_inspect": {
        "description": "Inspect the active website page as compact text and referenced controls without screenshots.",
        "cli": {"positionals": []},
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional text, name, type, role, or URL filter for controls",
                },
                "limit": {"type": "integer", "default": 80},
                "max_text_chars": {"type": "integer", "default": 10000},
            },
        },
    },
    "web_action": {
        "description": "Execute referenced DOM actions inside AgentWeb's persistent website session. Supports navigation, clicks, forms, keyboard input, uploads, downloads, hover menus, scrolling, screenshots, and waits.",
        "cli": {"positionals": []},
        "input_schema": {
            "type": "object",
            "required": ["steps"],
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Ordered web action objects using refs returned by web_inspect; repeat --steps with one JSON object per action in the direct CLI",
                },
                "inspect_after": {"type": "boolean", "default": True},
                "inspect_query": {
                    "type": "string",
                    "description": "Optional control filter for the resulting page",
                },
                "inspect_limit": {"type": "integer", "default": 80},
                "capture_name": {
                    "type": "string",
                    "description": "Mapping mode only: save a redacted causal network trace for these actions",
                },
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required when a referenced control appears to create, publish, purchase, delete, vote, send, or otherwise change state",
                },
            },
        },
    },
    "web_status": {
        "description": "Report whether the persistent internal website session is active.",
        "cli": {"positionals": []},
        "input_schema": {"type": "object", "properties": {}},
    },
    "web_tabs": {
        "description": "List open tabs in the active internal website session.",
        "cli": {"positionals": []},
        "input_schema": {"type": "object", "properties": {}},
    },
    "web_focus": {
        "description": "Focus an allowed website tab returned by web_tabs.",
        "cli": {"positionals": ["target_id"]},
        "input_schema": {
            "type": "object",
            "required": ["target_id"],
            "properties": {"target_id": {"type": "string"}},
        },
    },
    "web_new_tab": {
        "description": "Open and focus a new allowed website tab inside AgentWeb.",
        "cli": {"positionals": []},
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}},
    },
    "web_close_tab": {
        "description": "Close an internal website tab. Closing the last tab requires confirm=true.",
        "cli": {"positionals": []},
        "input_schema": {
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
        },
    },
    "web_stop": {
        "description": "Save the site's cookies and stop its internal browser session.",
        "cli": {"positionals": []},
        "input_schema": {"type": "object", "properties": {}},
    },
}

DIRECT_COMMANDS: dict[str, dict[str, Any]] = {
    "inspect_page": {
        "description": "Inspect a same-site page as compact text plus structured links and forms, without a browser or screenshots.",
        "cli": {"positionals": []},
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "/"},
                "query": {"type": "string", "description": "Optional control filter"},
                "limit": {"type": "integer", "default": 80},
                "max_text_chars": {"type": "integer", "default": 10000},
            },
        },
    },
    "submit_form": {
        "description": "Fetch a current same-site HTML form, preserve its hidden anti-CSRF fields, override named fields, and submit it directly. Requires confirm.",
        "cli": {"positionals": ["page_path", "selector"]},
        "input_schema": {
            "type": "object",
            "required": ["page_path", "selector", "fields"],
            "properties": {
                "page_path": {"type": "string"},
                "selector": {"type": "string"},
                "fields": {"type": "object"},
                "confirm": {"type": "boolean", "default": False},
            },
        },
    },
    "direct_request": {
        "description": "Call any HTTPS endpoint on this adapter's fixed host allowlist using the retained AgentWeb session. Supports JSON, forms, raw bodies, multipart uploads, and guarded mutations.",
        "cli": {"positionals": ["path"]},
        "input_schema": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path or allowlisted absolute HTTPS URL",
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
                    "default": "GET",
                },
                "query": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repeatable key=value query parameters",
                },
                "form": {
                    "type": "object",
                    "description": "URL-encoded or multipart form fields",
                },
                "json_body": {"type": "object", "description": "JSON request body"},
                "body": {"type": "string", "description": "Raw UTF-8 or base64 body"},
                "body_encoding": {
                    "type": "string",
                    "enum": ["utf8", "base64"],
                    "default": "utf8",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Multipart file objects with path, field, optional filename and content_type",
                },
                "headers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repeatable name=value headers; cookies and host are managed by AgentWeb",
                },
                "confirm": {"type": "boolean", "default": False},
                "max_items": {"type": "integer", "default": 100},
                "max_string": {"type": "integer", "default": 10000},
                "max_total_chars": {"type": "integer", "default": 50000},
            },
        },
    },
    "direct_workflow": {
        "description": "Replay an ordered same-site HTTP workflow. Later steps can use values extracted from earlier JSON, headers, regex matches, or HTML selectors; secret-like values are never returned.",
        "cli": {"positionals": []},
        "input_schema": {
            "type": "object",
            "required": ["steps"],
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Ordered direct request and extraction step objects",
                },
                "variables": {
                    "type": "object",
                    "description": "Non-secret template variables",
                },
                "confirm": {"type": "boolean", "default": False},
                "max_items": {"type": "integer", "default": 100},
                "max_string": {"type": "integer", "default": 10000},
                "max_total_chars": {"type": "integer", "default": 50000},
            },
        },
    },
}

COMMON_ACTIONS: dict[str, dict[str, str]] = {
    "search": {
        "arxiv": "search_papers",
        "github": "search_repositories",
        "npm": "search_packages",
    },
    "status": {
        "amazon": "account_status",
        "github": "auth_status",
        "hn": "account_status",
        "spotify": "setup_status",
        "stackoverflow": "account_status",
        "wikipedia": "account_status",
    },
}

RUNTIME_OPERATION_ERRORS = [
    {
        "code": "adapter_load_failed",
        "meaning": "The installed adapter could not load under the running AgentWeb core.",
    },
    {
        "code": "flow_drift",
        "meaning": "A current website response no longer matches the reviewed captured shape.",
    },
    {
        "code": "operation_output_mapping_failed",
        "meaning": "The current response could not be normalized into the operation contract.",
    },
    {
        "code": "internal_error",
        "meaning": "An unanticipated implementation failure occurred; local paths and secrets are suppressed.",
    },
]


class Runtime:
    def __init__(
        self,
        paths: StatePaths | None = None,
        *,
        profile: str = "default",
        fresh: bool = False,
        mapping_mode: bool | None = None,
        cancel_event: threading.Event | None = None,
        max_output_chars: int = 100_000,
        interface: str = "cli",
    ) -> None:
        self.paths = paths or StatePaths.discover()
        self.paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.profile = safe_component(profile, label="profile")
        except ValueError as exc:
            raise AgentWebError(str(exc)) from exc
        self.fresh = fresh
        self.cancellation = CancellationToken(cancel_event)
        self.max_output_chars = max_output_chars
        self.interface = interface
        self.mapping_mode = (
            (
                os.environ.get("AGENTWEB_MAPPING_MODE")
                or os.environ.get("SITEPACK_MAPPING_MODE")
            )
            == "1"
            if mapping_mode is None
            else mapping_mode
        )
        self.registry = Registry(self.paths)
        self.analytics = Analytics(self.paths)
        # Optional per-call hook to bind adapters to an alternate transport
        # (e.g. a CDP-backed browser session for read-back verification). Only
        # set by explicit browser-assisted paths; ordinary operations leave it
        # ``None`` and never launch a browser.
        self._session_override: Callable[[str], Any] | None = None

    def ensure_registry(self) -> None:
        installed = self.registry.installed()
        config = read_json(self.paths.registry_config, {}) or {}
        source = self.registry.configured_source()
        remote = source.startswith(("https://", "http://"))
        last_attempt = max(
            float(config.get("last_sync_at", 0)),
            float(config.get("last_sync_attempt_at", 0)),
        )
        stale = time.time() - last_attempt > 300
        if not installed or (remote and stale):
            try:
                self.registry.sync()
            except AgentWebError as exc:
                if not installed:
                    raise
                config["last_sync_attempt_at"] = time.time()
                config["last_sync_error"] = str(exc)
                write_json(self.paths.registry_config, config)

    def sites(self, full: bool = False) -> list[dict[str, Any]]:
        """List installed sites; compact rows by default, full manifests with full=True."""
        self.ensure_registry()
        result = []
        for name, entry in sorted(self.registry.installed().items()):
            manifest = self.describe(name)
            if full:
                result.append(
                    {
                        "name": name,
                        "domain": canonical_domain(manifest),
                        "domains": manifest.get("allowed_domains") or [],
                        "aliases": sorted(set(manifest.get("aliases") or []) | {name}),
                        "version": entry["version"],
                        "description": manifest.get("description"),
                        "commands": sorted(manifest.get("commands", {})),
                    }
                )
            else:
                result.append(
                    {
                        "name": name,
                        "domain": canonical_domain(manifest),
                        "version": entry["version"],
                        "operations": len(manifest.get("commands", {})),
                        "description": manifest.get("description"),
                    }
                )
        return result

    def resolve(self, target: str) -> ResolvedTarget:
        """Resolve an adapter name, alias, domain, subdomain, or URL."""
        self.ensure_registry()
        raw_url = target_url(target)
        host = normalized_host(raw_url or target)
        candidates: list[tuple[int, str, str]] = []
        installed = self.registry.installed()
        for name in installed:
            manifest = self.registry.manifest(name)
            domain = canonical_domain(manifest)
            aliases = {name, domain, *(manifest.get("aliases") or [])}
            if target.strip().lower() in {str(alias).lower() for alias in aliases}:
                candidates.append((10_000, name, domain))
                continue
            for allowed in manifest.get("allowed_domains") or []:
                if host and host_matches(host, str(allowed)):
                    candidates.append((len(str(allowed)), name, domain))
        if not candidates:
            known = sorted(
                canonical_domain(self.registry.manifest(name)) for name in installed
            )
            raise AgentWebError(
                f"No AgentWeb adapter matches {target!r}",
                code="site_not_found",
                field="target",
                next_action="agentweb sites",
                details={"available_domains": known},
            )
        _, site, domain = max(candidates)
        return ResolvedTarget(site=site, domain=domain, url=raw_url)

    def capabilities(
        self,
        target: str,
        *,
        query: str | None = None,
        limit: int = 50,
        live: bool = False,
    ) -> dict[str, Any]:
        resolved = self.resolve(target)
        catalog = self.discover(resolved.site, query=query, limit=limit)
        manifest = self.describe(resolved.site)
        session = self.adapter(resolved.site).session().cookie_summary()
        operations = catalog.get("operations") or []
        authentication: dict[str, Any] = {
            "strategy": (manifest.get("auth") or {}).get("strategy", "none"),
            "saved_cookie_count": session.get("cookie_count", 0),
            "profile": self.profile,
            "live_verification": "not_requested",
        }
        if live:
            status_action = (COMMON_ACTIONS.get("status") or {}).get(resolved.site)
            if status_action and status_action in (manifest.get("commands") or {}):
                try:
                    authentication["live"] = self.call(
                        f"{resolved.site}.{status_action}", {}
                    )
                    authentication["live_verification"] = "completed"
                except AgentWebError as exc:
                    authentication["live"] = exc.as_dict()
                    authentication["live_verification"] = "failed"
            else:
                authentication["live_verification"] = "unsupported"
        return {
            "ok": True,
            "target": resolved.as_dict(),
            "description": catalog.get("description"),
            "version": catalog.get("version"),
            "authentication": authentication,
            "operations": operations,
            "page": catalog.get("page"),
            "parity": catalog.get("parity"),
            "examples": [
                f"agentweb {resolved.domain} ACTION --help",
                f"agentweb describe {resolved.domain} --operation ACTION",
                f"agentweb run {resolved.domain} ACTION --input '{{}}'",
            ],
        }

    def profiles(self) -> dict[str, Any]:
        root = self.paths.root / "profiles"
        profiles: dict[str, set[str]] = {}
        if root.is_dir():
            for site_dir in root.iterdir():
                if not site_dir.is_dir():
                    continue
                for profile_dir in site_dir.iterdir():
                    if profile_dir.is_dir():
                        profiles.setdefault(profile_dir.name, set()).add(site_dir.name)
        profiles.setdefault(self.profile, set())
        return {
            "active": self.profile,
            "profiles": [
                {"name": name, "sites_with_local_state": sorted(sites)}
                for name, sites in sorted(profiles.items())
            ],
            "usage": "agentweb --profile NAME DOMAIN ACTION [arguments]",
        }

    def execute(
        self,
        target: str,
        action: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Execute through the stable, domain-first agent response envelope."""
        started = time.monotonic()
        resolved = self.resolve(target)
        action = self.resolve_action(resolved.site, action)
        request_hash = hashlib.sha256(
            json.dumps(
                {"site": resolved.site, "action": action, "arguments": arguments},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if not idempotency_key:
            return self._execute_response(resolved, action, arguments, started)
        safe_component(idempotency_key, label="idempotency key")
        receipt_dir = self.paths.root / "idempotency" / self.profile
        receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        receipt_path = receipt_dir / f"{idempotency_key}.json"
        lock_path = receipt_dir / f".{idempotency_key}.lock"
        with exclusive_path_lock(lock_path, timeout=600, stale_after=900):
            receipt = read_json(receipt_path)
            if receipt:
                if receipt.get("request_hash") != request_hash:
                    raise AgentWebError(
                        "This idempotency key was already used for a different request",
                        code="idempotency_conflict",
                        field="idempotency_key",
                    )
                replay = deepcopy(receipt["response"])
                replay.setdefault("meta", {})["idempotency_replayed"] = True
                return replay
            response = self._execute_response(resolved, action, arguments, started)
            write_json(
                receipt_path,
                {
                    "request_hash": request_hash,
                    "created_at": time.time(),
                    "response": response,
                },
            )
            return response

    @staticmethod
    def _normalize_result_envelope(
        result: dict[str, Any],
    ) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        """Flatten the internal SITE.ACTION envelope into (data, verification, extra).

        Contract and flow-capsule operations return a nested envelope
        ({"operation", "data", "verification", "state_change", ...}); returning it
        verbatim double-nests the payload as ``data.data`` and leaves the public
        ``verification`` field empty. Flat legacy adapter results are passed through
        unchanged with a best-effort verification scan so both tiers look identical.
        """
        standard_envelope = "operation" in result and "data" in result
        if not standard_envelope:
            scanned: dict[str, Any] = {
                key: result[key]
                for key in ("accepted", "verified", "changed", "state_changed")
                if key in result
            }
            return result, scanned, {}
        data = result["data"]
        verification: dict[str, Any] = {}
        inner = result.get("verification")
        if isinstance(inner, dict):
            verification.update(inner)
        state_change = result.get("state_change")
        if isinstance(state_change, dict):
            verification["state_change"] = state_change
        extra: dict[str, Any] = {
            key: result[key]
            for key in ("pagination", "warnings", "truncated", "truncation")
            if key in result
        }
        return data, verification, extra

    def _execute_response(
        self,
        resolved: ResolvedTarget,
        action: str,
        arguments: dict[str, Any],
        started: float,
    ) -> dict[str, Any]:
        result = self.call(f"{resolved.site}.{action}", arguments)
        data, verification, extra = self._normalize_result_envelope(result)

        next_hints = data if isinstance(data, dict) else {}
        next_actions = [
            value
            for key, value in next_hints.items()
            if key in {"next", "next_action", "next_operation", "next_tool"}
            and isinstance(value, str)
        ]
        for source in (result, next_hints):
            declared = source.get("next_actions")
            if isinstance(declared, list):
                next_actions.extend(
                    item for item in declared if isinstance(item, str)
                )
        response = {
            "ok": True,
            "site": resolved.site,
            "domain": resolved.domain,
            "operation": action,
            "profile": self.profile,
            "data": data,
            "verification": verification,
            "meta": {
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "interface": "agentweb_cli_v1",
            },
            "next_actions": next_actions,
        }
        if extra:
            response["result_meta"] = extra
        return response

    def resolve_action(self, target: str, action: str) -> str:
        resolved = self.resolve(target)
        normalized = action.replace("-", "_")
        commands = self.describe(resolved.site).get("commands") or {}
        if normalized in commands:
            return normalized
        mapped = (COMMON_ACTIONS.get(normalized) or {}).get(resolved.site)
        if mapped and mapped in commands:
            return mapped
        return normalized

    @staticmethod
    def _workflow_value(value: Any, outputs: dict[str, Any]) -> Any:
        if isinstance(value, list):
            return [Runtime._workflow_value(item, outputs) for item in value]
        if isinstance(value, dict):
            return {
                key: Runtime._workflow_value(item, outputs)
                for key, item in value.items()
            }
        if not isinstance(value, str) or not value.startswith("$"):
            return value
        path = value[1:].split(".")
        current: Any = outputs
        for part in path:
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif (
                isinstance(current, list)
                and part.isdigit()
                and int(part) < len(current)
            ):
                current = current[int(part)]
            else:
                raise AgentWebError(
                    f"Workflow reference {value!r} did not match an earlier step",
                    code="workflow_reference_missing",
                    field="steps",
                )
        return current

    def workflow(self, target: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        if not steps or len(steps) > 50:
            raise AgentWebError(
                "A workflow must contain between 1 and 50 steps",
                code="invalid_workflow",
                field="steps",
            )
        resolved = self.resolve(target)
        outputs: dict[str, Any] = {}
        started = time.monotonic()
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise AgentWebError(
                    "Every workflow step must be an object", field="steps"
                )
            action = str(step.get("action") or "")
            name = str(step.get("name") or f"step_{index + 1}")
            safe_component(name, label="workflow step name")
            if not action or name in outputs:
                raise AgentWebError(
                    "Workflow steps need a unique name and an action",
                    code="invalid_workflow",
                    field="steps",
                )
            arguments = self._workflow_value(step.get("arguments") or {}, outputs)
            if not isinstance(arguments, dict):
                raise AgentWebError(
                    "Workflow step arguments must be an object", field="steps"
                )
            outputs[name] = self.execute(
                resolved.site,
                action,
                arguments,
                idempotency_key=step.get("idempotency_key"),
            )
        return {
            "ok": True,
            "site": resolved.site,
            "domain": resolved.domain,
            "profile": self.profile,
            "operation": "workflow",
            "data": {"steps": outputs, "count": len(outputs)},
            "verification": {},
            "meta": {
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "interface": "agentweb_cli_v1",
            },
            "next_actions": [],
        }

    def get(self, url: str) -> dict[str, Any]:
        resolved = self.resolve(url)
        if not resolved.url:
            raise AgentWebError(
                "agentweb get requires a complete HTTP(S) URL",
                code="invalid_target",
                field="url",
            )
        manifest = self.describe(resolved.site)
        typed = extract_resource(manifest, resolved.url)
        if typed:
            action, arguments = typed
            if action in (manifest.get("commands") or {}):
                return self.execute(resolved.site, action, arguments)
        parsed = urlparse(resolved.url)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        result = self.execute(resolved.site, "inspect_page", {"path": path})
        result["resolution"] = "generic_same_site_inspection"
        return result

    def _flow_capsules(
        self, site: str, manifest: dict[str, Any], installed: dict[str, Any]
    ) -> dict[str, tuple[dict[str, Any], Path]]:
        manifest_path = Path(str(installed.get("manifest") or ""))
        root = manifest_path.parent.resolve()
        result: dict[str, tuple[dict[str, Any], Path]] = {}
        for action, declaration in (manifest.get("flow_capsules") or {}).items():
            try:
                safe_component(str(action), label="flow capsule command")
            except ValueError as exc:
                raise AgentWebError(str(exc)) from exc
            if isinstance(declaration, str):
                relative = declaration
            elif isinstance(declaration, dict):
                relative = str(declaration.get("path") or "")
            else:
                raise AgentWebError(
                    f"{site}.{action} has an invalid flow capsule declaration"
                )
            path = (root / relative).resolve()
            if not relative or (path != root and root not in path.parents):
                raise AgentWebError(
                    f"{site}.{action} flow capsule escapes its adapter directory"
                )
            capsule = read_json(path)
            structural = verify_flow_capsule(capsule)
            if not structural["passed"]:
                raise AgentWebError(
                    f"{site}.{action} flow capsule is invalid: "
                    + "; ".join(structural["errors"])
                )
            if ((capsule.get("verification") or {}).get("status")) != "passed":
                raise AgentWebError(
                    f"{site}.{action} flow capsule has not passed a live replay verification"
                )
            if capsule.get("site") != site:
                raise AgentWebError(
                    f"{site}.{action} flow capsule targets another site"
                )
            result[str(action)] = (capsule, path)
        return result

    @staticmethod
    def _flow_command(
        action: str,
        capsule: dict[str, Any],
        declaration: Any,
        contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        contract = contract or {}
        recipe = capsule.get("recipe") or {}
        required = list(recipe.get("required_inputs") or [])
        human = set(recipe.get("human_inputs") or [])
        captured_properties = {
            name: {
                "type": "string",
                "description": (
                    "Human-provided verification value required by the website"
                    if name in human
                    else "Captured workflow input"
                ),
            }
            for name in required
        }
        input_schema = deepcopy(contract.get("input_schema") or {})
        if not input_schema:
            input_schema = {
                "type": "object",
                "required": required,
                "properties": captured_properties,
            }
        properties = input_schema.setdefault("properties", {})
        risk = contract.get("risk") or {}
        if (capsule.get("risk") or {}).get("mutating") or risk.get("level") != "read":
            properties["confirm"] = {
                "type": "boolean",
                "default": False,
                "description": "Required before a captured workflow with mutations is replayed",
            }
        description = (
            (
                contract.get("summary")
                or (
                    declaration.get("description")
                    if isinstance(declaration, dict)
                    else None
                )
            )
            or f"Replay the verified captured {action.replace('_', ' ')} workflow without a browser."
        )
        return {
            "description": description,
            "category": contract.get("category", "other"),
            "when_to_use": contract.get("when_to_use"),
            "when_not_to_use": contract.get("when_not_to_use"),
            "cli": {
                "positionals": list(
                    (contract.get("cli") or {}).get("positionals")
                    or input_schema.get("required")
                    or required
                )
            },
            "input_schema": input_schema,
            "output_schema": contract.get("output_schema"),
            "examples": contract.get("examples") or [],
            "errors": contract.get("errors") or [],
            "authentication": contract.get("authentication") or {},
            "risk": risk,
            "pagination": contract.get("pagination") or {"supported": False},
            "verification": contract.get("verification") or {},
            "flow_capsule": True,
            "mutating": bool((capsule.get("risk") or {}).get("mutating")),
        }

    def describe(self, site: str, *, include_cli: bool = True) -> dict[str, Any]:
        self.ensure_registry()
        site = self.resolve(site).site
        manifest = deepcopy(self.registry.manifest(site))
        installed = self.registry.installed().get(site) or {}
        capsules = self._flow_capsules(site, manifest, installed)
        for action, (capsule, _path) in capsules.items():
            declaration = (manifest.get("flow_capsules") or {}).get(action)
            contract_name = (
                declaration.get("contract") if isinstance(declaration, dict) else action
            ) or action
            contract = (manifest.get("operation_contracts") or {}).get(contract_name)
            manifest.setdefault("commands", {})[action] = self._flow_command(
                action, capsule, declaration, contract
            )
        audit = validate_manifest(
            manifest,
            path=Path(installed["manifest"]) if installed.get("manifest") else None,
        )
        if manifest.get("base_url"):
            for name, command in deepcopy(DIRECT_COMMANDS).items():
                manifest.setdefault("commands", {}).setdefault(name, command)
        if manifest.get("base_url") and self.mapping_mode:
            manifest.setdefault("commands", {}).update(deepcopy(WEB_COMMANDS))
        runtime_contract = manifest.get("runtime") or {}
        manifest["parity"] = {
            "target": "browserless_full_website_action_parity",
            "typed_fast_paths": sorted(
                name for name in manifest["commands"] if name not in WEB_COMMANDS
            ),
            "browserless_replay": bool(runtime_contract.get("browserless_replay")),
            "runtime_browser_commands_exposed": bool(self.mapping_mode),
            "mapping_only_browser_commands": sorted(WEB_COMMANDS),
            "declared_gaps": (manifest.get("coverage") or {}).get("not_mapped", []),
            "manifest_complete": audit["manifest_complete"],
            "parity_verified": audit["parity_verified"],
            "verified_commands": audit["verified_commands"],
            "shape_verified_commands": audit["shape_verified_commands"],
            "unverified_commands": audit["unverified_commands"],
            "agent_leaves_agentweb": False,
            "human_handoffs": [
                "CAPTCHA",
                "OTP or passkey",
                "consent",
                "irreversible action confirmation",
                "payment confirmation",
            ],
        }
        if not include_cli:
            for command in (manifest.get("commands") or {}).values():
                if isinstance(command, dict):
                    command.pop("cli", None)
        return manifest

    def discover(
        self,
        site: str,
        *,
        operation: str | None = None,
        category: str | None = None,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        include_parity_details: bool = False,
    ) -> dict[str, Any]:
        """Return compact, bounded operation discovery for agents."""
        if limit < 1 or limit > 100:
            raise AgentWebError(
                "site_describe limit must be between 1 and 100", field="limit"
            )
        try:
            resolved = self.resolve(site)
        except AgentWebError:
            # Preserve discovery for injected/development manifests that have not
            # been packaged into a registry yet. Normal CLI calls still fail when
            # describe() cannot load the requested adapter.
            resolved = ResolvedTarget(site=site, domain=site)
        site = resolved.site
        manifest = deepcopy(self.describe(site))
        for command in (manifest.get("commands") or {}).values():
            if isinstance(command, dict):
                command.pop("cli", None)
        commands = manifest.get("commands") or {}
        parity = manifest.get("parity") or {}
        parity_view = (
            parity
            if include_parity_details
            else {
                "target": parity.get("target"),
                "browserless_replay": parity.get("browserless_replay"),
                "manifest_complete": parity.get("manifest_complete"),
                "parity_verified": parity.get("parity_verified"),
                "declared_gaps": parity.get("declared_gaps") or [],
                "verified_command_count": len(parity.get("verified_commands") or []),
                "shape_verified_command_count": len(
                    parity.get("shape_verified_commands") or []
                ),
                "unverified_command_count": len(
                    parity.get("unverified_commands") or []
                ),
                "details_omitted": True,
            }
        )
        if operation:
            command = commands.get(operation)
            if not command:
                raise AgentWebError(
                    f"Unknown operation {site}.{operation}",
                    code="operation_not_found",
                    field="operation",
                    next_action=f"site_describe(site={site})",
                )
            command["errors_exhaustive"] = False
            command["error_contract_scope"] = (
                "The errors array documents anticipated site-specific failures. "
                "runtime_errors documents shared failure classes; unexpected upstream "
                "or implementation failures can still occur."
            )
            command["runtime_errors"] = deepcopy(RUNTIME_OPERATION_ERRORS)
            return {
                "site": site,
                "domain": resolved.domain,
                "operation": operation,
                "contract": command,
                "parity": parity_view,
            }
        needle = (query or "").strip().lower()
        matches: list[tuple[str, dict[str, Any]]] = []
        for name, command in sorted(commands.items()):
            command_category = str(command.get("category") or "other")
            if category and command_category != category:
                continue
            searchable = " ".join(
                str(command.get(field) or "")
                for field in ("description", "when_to_use", "when_not_to_use")
            )
            if needle and needle not in f"{name} {searchable}".lower():
                continue
            matches.append((name, command))
        try:
            offset = int(cursor or 0)
        except ValueError as exc:
            raise AgentWebError(
                "site_describe cursor is invalid", field="cursor"
            ) from exc
        if offset < 0 or offset > len(matches):
            raise AgentWebError("site_describe cursor is out of range", field="cursor")
        page = matches[offset : offset + limit]
        next_cursor = (
            str(offset + len(page)) if offset + len(page) < len(matches) else None
        )
        categories: dict[str, int] = {}
        for _name, command in commands.items():
            key = str(command.get("category") or "other")
            categories[key] = categories.get(key, 0) + 1
        return {
            "site": site,
            "domain": resolved.domain,
            "description": manifest.get("description"),
            "version": manifest.get("version"),
            "authentication": manifest.get("auth") or {},
            "categories": categories,
            "operations": [
                {
                    "name": name,
                    "summary": command.get("description"),
                    "category": command.get("category", "other"),
                    "risk": command.get("risk"),
                    "authentication": command.get("authentication"),
                    "inputs": [
                        {
                            key: value
                            for key, value in {
                                "name": input_name,
                                "type": input_contract.get("type", "string"),
                                "required": input_name
                                in set(
                                    (command.get("input_schema") or {}).get("required")
                                    or []
                                ),
                                "default": input_contract.get("default"),
                                "enum": input_contract.get("enum"),
                                "minimum": input_contract.get("minimum"),
                                "maximum": input_contract.get("maximum"),
                                "description": input_contract.get("description"),
                            }.items()
                            if value is not None
                        }
                        for input_name, input_contract in (
                            (command.get("input_schema") or {}).get("properties") or {}
                        ).items()
                    ],
                    "example": (
                        (command.get("examples") or [{}])[0].get("input")
                        if command.get("examples")
                        else None
                    ),
                }
                for name, command in page
            ],
            "page": {
                "cursor": cursor,
                "next_cursor": next_cursor,
                "limit": limit,
                "total": len(matches),
            },
            "parity": parity_view,
            "next": (
                "Call site_describe with operation=<name> for that operation's complete "
                "input contract. Use query/category to narrow this catalog."
            ),
        }

    def adapter(self, site: str):
        self.ensure_registry()
        site = self.resolve(site).site
        installed = self.registry.installed().get(site)
        if not installed:
            raise AgentWebError(f"Unknown site {site!r}")
        manifest = self.registry.manifest(site)
        compatibility = manifest.get("compatibility") or {}
        minimum_runtime = str(compatibility.get("minimum_runtime") or "")
        if minimum_runtime:
            def version_tuple(value: str) -> tuple[int, ...]:
                return tuple(int(item) for item in value.split(".") if item.isdigit())

            if version_tuple(__version__) < version_tuple(minimum_runtime):
                raise AgentWebError(
                    f"The installed {site} adapter requires AgentWeb {minimum_runtime} or newer",
                    code="adapter_runtime_incompatible",
                    retryable=False,
                    next_action="agentweb setup",
                    user_action="Update AgentWeb, restart any long-running MCP process, and retry the operation.",
                    details={
                        "site": site,
                        "adapter_version": installed["version"],
                        "minimum_runtime": minimum_runtime,
                        "running_runtime": __version__,
                    },
                )
        module_path = Path(installed["manifest"]).parent / manifest.get(
            "entrypoint", "adapter.py"
        )
        module_name = (
            f"agentweb_adapter_{site}_{installed['version'].replace('.', '_')}"
        )
        specification = importlib.util.spec_from_file_location(module_name, module_path)
        if not specification or not specification.loader:
            raise AgentWebError(f"Could not load adapter for {site}")
        module = importlib.util.module_from_spec(specification)
        try:
            specification.loader.exec_module(module)
        except Exception as exc:
            raise AgentWebError(
                f"The installed {site} adapter could not be loaded",
                code="adapter_load_failed",
                retryable=False,
                next_action="agentweb sync",
                user_action="Update AgentWeb and retry the operation.",
                details={"site": site, "adapter_version": installed["version"]},
            ) from exc
        context = AdapterContext(
            self.paths,
            profile=self.profile,
            fresh=self.fresh,
            cancellation=self.cancellation,
        )
        adapter = module.Adapter(context)
        if self._session_override is not None:
            # Bind to the adapter's own site_name so the session's cookie jar
            # matches the one the adapter would otherwise build for itself.
            adapter._session = self._session_override(adapter.site_name)
        return adapter

    def call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        site: str | None = None
        action: str | None = None
        adapter_version: str | None = None
        recorded_operation: str | None = None
        try:
            if "." in operation:
                target, action = operation.rsplit(".", 1)
                site = self.resolve(target).site
                manifest = self.describe(site)
                command = (manifest.get("commands") or {}).get(action) or {}
                contract = (manifest.get("operation_contracts") or {}).get(action) or {}
                risk = command.get("risk") or contract.get("risk") or {}
                sensitive = bool(command.get("mutating")) or risk.get("level") not in {
                    None,
                    "read",
                    "read_only",
                }
                recorded_operation = "account_write" if sensitive else action
                adapter_version = str(
                    (self.registry.installed().get(site) or {}).get("version") or ""
                ) or None
        except Exception:
            # Invalid targets and manifests must never leak their raw value into analytics.
            site = action = recorded_operation = adapter_version = None
        try:
            result = self._call_uninstrumented(operation, arguments)
        except AgentWebError as exc:
            if isinstance(exc, AuthenticationRequired) and site:
                self._enrich_authentication_error(exc, site)
            self.analytics.record(
                "operation_completed",
                site=site,
                operation=recorded_operation,
                success=False,
                duration_ms=(time.monotonic() - started) * 1000,
                interface=self.interface,
                adapter_version=adapter_version,
                error_code=exc.code,
            )
            raise
        except Exception:
            self.analytics.record(
                "operation_completed",
                site=site,
                operation=recorded_operation,
                success=False,
                duration_ms=(time.monotonic() - started) * 1000,
                interface=self.interface,
                adapter_version=adapter_version,
                error_code="internal_error",
            )
            raise
        meta_value = result.get("meta")
        meta = meta_value if isinstance(meta_value, dict) else {}
        self.analytics.record(
            "operation_completed",
            site=site,
            operation=recorded_operation,
            success=True,
            duration_ms=(time.monotonic() - started) * 1000,
            interface=self.interface,
            adapter_version=adapter_version,
            from_cache=meta.get("from_cache"),
        )
        return result

    def _enrich_authentication_error(
        self, exc: AuthenticationRequired, site: str
    ) -> None:
        """Attach the reconnect command and, when the stored session has
        provably expired, a `session_expired` marker so callers can tell an
        expired session apart from a site or adapter failure."""
        exc.details.setdefault(
            "reconnect_command",
            ["agentweb", "--profile", self.profile, "connect", site],
        )
        meta = read_json(
            self.paths.profile_dir(site, self.profile) / "session-meta.json", None
        )
        expires_at = (meta or {}).get("session_expires_at_unix")
        if isinstance(expires_at, (int, float)) and 0 < expires_at <= time.time():
            exc.details.setdefault("session_expired", True)
            exc.details.setdefault("session_expired_at_unix", int(expires_at))
        if not exc.user_action and not exc.next_action:
            exc.user_action = (
                f"Run `agentweb connect {site}` and sign in to refresh the session"
                if exc.details.get("session_expired")
                else f"Run `agentweb connect {site}` and sign in"
            )

    def _call_uninstrumented(
        self, operation: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.cancellation.check()
        if "." not in operation:
            raise AgentWebError(
                "Operation must be SITE.ACTION, for example amazon.search"
            )
        target, action = operation.rsplit(".", 1)
        site = self.resolve(target).site
        manifest = self.describe(site)
        command = (manifest.get("commands") or {}).get(action)
        if not command:
            raise AgentWebError(f"Unknown operation {operation!r}")
        required = command.get("input_schema", {}).get("required", [])
        missing = [
            name
            for name in required
            if name not in arguments or arguments[name] is None
        ]
        if missing:
            raise AgentWebError(
                f"Missing required input: {', '.join(missing)}",
                code="missing_input",
                field=missing[0] if len(missing) == 1 else None,
            )
        self._validate_arguments(operation, arguments, command.get("input_schema", {}))
        if action in WEB_COMMANDS:
            web = WebRuntime(
                self.paths,
                site=site,
                profile=self.profile,
                base_url=manifest["base_url"],
                cookie_domain=manifest.get("cookie_domain"),
                allowed_domains=manifest.get("allowed_domains"),
            )
            if action == "web_start":
                result = web.start(**arguments)
            elif action == "web_inspect":
                result = web.inspect(**arguments)
            elif action == "web_action":
                result = web.action(**arguments)
            elif action == "web_status":
                result = web.status()
            elif action == "web_tabs":
                result = web.tabs()
            elif action == "web_focus":
                result = web.focus(**arguments)
            elif action == "web_new_tab":
                result = web.new_tab(**arguments)
            elif action == "web_close_tab":
                result = web.close_tab(**arguments)
            else:
                result = web.stop()
            return self._bounded_result(operation, result)
        installed = self.registry.installed().get(site) or {}
        capsules = self._flow_capsules(site, self.registry.manifest(site), installed)
        if action in capsules:
            capsule, _path = capsules[action]
            arguments_without_confirmation = {
                name: value for name, value in arguments.items() if name != "confirm"
            }
            contract = (manifest.get("operation_contracts") or {}).get(action) or {}
            binding = contract.get("binding") or {}
            variables = map_operation_inputs(
                arguments_without_confirmation, binding.get("variables")
            )
            replay = self.adapter(site).call(
                "direct_workflow",
                {
                    "steps": (capsule.get("recipe") or {}).get("steps") or [],
                    "variables": variables,
                    "confirm": bool(arguments.get("confirm")),
                    "max_items": 1_000,
                    "max_total_chars": 5_000_000,
                },
            )
            final_status = (
                (replay.get("steps") or [{}])[-1].get("status")
                if replay.get("steps")
                else None
            )
            if isinstance(final_status, int) and final_status >= 400:
                error_code = str(
                    (binding.get("http_errors") or {}).get(str(final_status))
                    or "website_http_error"
                )
                documented: dict[str, Any] = next(
                    (
                        item
                        for item in contract.get("errors") or []
                        if item.get("code") == error_code
                    ),
                    {},
                )
                raise AgentWebError(
                    str(
                        documented.get("message")
                        or f"The website returned HTTP {final_status}."
                    ),
                    code=error_code,
                    retryable=bool(documented.get("retryable")),
                    next_action=documented.get("next_operation"),
                    user_action=documented.get("user_action"),
                    field=documented.get("field"),
                    retry_after_seconds=documented.get("retry_after_seconds"),
                    details={"http_status": final_status},
                )
            response_error = match_operation_response_error(
                replay.get("data", replay), binding.get("response_errors")
            )
            if response_error:
                error_code = response_error["code"]
                documented = next(
                    (
                        item
                        for item in contract.get("errors") or []
                        if item.get("code") == error_code
                    ),
                    {},
                )
                raise AgentWebError(
                    str(
                        documented.get("message")
                        or "The website reported that it could not complete the request."
                    ),
                    code=error_code,
                    retryable=bool(documented.get("retryable")),
                    next_action=documented.get("next_operation"),
                    user_action=documented.get("user_action"),
                    field=documented.get("field"),
                    retry_after_seconds=documented.get("retry_after_seconds"),
                    details=response_error.get("details") or {},
                )
            verification = verify_flow_capsule(capsule, replay)
            if not verification["passed"]:
                diagnostics = (verification.get("errors") or []) + (
                    verification.get("mismatches") or []
                )
                raise AgentWebError(
                    f"{operation} is temporarily degraded because the website response "
                    "no longer matches its verified shape"
                    + (f": {'; '.join(diagnostics)}" if diagnostics else "."),
                    code="flow_drift",
                    retryable=True,
                    next_action=f"site_describe(site={site}, operation=direct_request)",
                    user_action=(
                        "Retry once with fresh=true. If it still fails, use the site's "
                        "read-only direct_request fallback or report the degraded operation."
                    ),
                )
            if not contract:
                return self._bounded_result(
                    operation,
                    {
                        "operation": operation,
                        "verified": True,
                        "transport": "installed_flow_capsule",
                        "result": replay,
                    },
                )
            data = normalize_operation_output(
                replay.get("data", replay), binding.get("output")
            )
            data, local_pagination, local_warnings = paginate_operation_output(
                data, binding.get("output"), arguments_without_confirmation
            )
            output_schema = contract.get("output_schema")
            if output_schema:
                output_problems = self._schema_problems(
                    data, output_schema, path="data"
                )
                if output_problems:
                    raise AgentWebError(
                        f"{operation} returned data that violated its reviewed contract: "
                        + "; ".join(output_problems),
                        code="adapter_output_invalid",
                        retryable=False,
                        next_action=f"site_describe(site={site}, operation={action})",
                    )
            risk = contract.get("risk") or {}
            return self._bounded_result(
                operation,
                {
                    "operation": operation,
                    "data": data,
                    "state_change": {
                        "changed": bool(replay.get("state_changed")),
                        "reversible": risk.get("level") == "reversible_write",
                        "idempotent": risk.get("idempotent"),
                    },
                    "pagination": local_pagination
                    if local_pagination.get("supported")
                    else replay.get("pagination")
                    or contract.get("pagination")
                    or {"supported": False},
                    "warnings": (replay.get("warnings") or []) + local_warnings,
                    "verification": {
                        "verified": True,
                        "transport": "installed_flow_capsule",
                        "response_checked_at_unix": int(time.time()),
                        "build_verified_at_unix": (
                            capsule.get("verification") or {}
                        ).get("verified_at_unix"),
                        "meaning": (
                            "The live response matched a flow reviewed at build_verified_at_unix; "
                            "response_checked_at_unix is this call's check time."
                        ),
                        "reviewed": bool(
                            (capsule.get("verification") or {}).get("reviewed")
                        ),
                    },
                },
            )
        result = self.adapter(site).call(action, arguments)
        if not isinstance(result, dict):
            raise AgentWebError(f"Adapter {operation} returned a non-object result")
        return self._bounded_result(operation, result)

    def _bounded_result(self, operation: str, result: dict[str, Any]) -> dict[str, Any]:
        bounded, truncated, original_chars = enforce_data_budget(
            result, max_total_chars=self.max_output_chars
        )
        if not truncated:
            return result
        return {
            "operation": operation,
            "data": bounded,
            "truncated": True,
            "truncation": {
                "reason": "maximum_output_characters",
                "maximum_characters": self.max_output_chars,
                "original_characters": original_chars,
            },
            "original_chars": original_chars,
        }

    @staticmethod
    def _schema_problems(value: Any, schema: dict[str, Any], *, path: str) -> list[str]:
        problems: list[str] = []
        expected = schema.get("type")
        valid = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }
        if isinstance(expected, list):
            if not any(valid.get(option, True) for option in expected):
                return [f"{path} must be one of {', '.join(map(str, expected))}"]
            expected = next(
                (option for option in expected if valid.get(option)), None
            )
        elif expected in valid and not valid[expected]:
            return [f"{path} must be {expected}"]
        if "enum" in schema and value not in schema["enum"]:
            problems.append(f"{path} is not one of the declared values")
        if expected == "object" and isinstance(value, dict):
            for field in schema.get("required") or []:
                if field not in value:
                    problems.append(f"{path}.{field} is required")
            for field, child in (schema.get("properties") or {}).items():
                if field in value and isinstance(child, dict):
                    problems.extend(
                        Runtime._schema_problems(
                            value[field], child, path=f"{path}.{field}"
                        )
                    )
        if (
            expected == "array"
            and isinstance(value, list)
            and isinstance(schema.get("items"), dict)
        ):
            for index, item in enumerate(value):
                problems.extend(
                    Runtime._schema_problems(
                        item, schema["items"], path=f"{path}[{index}]"
                    )
                )
        return problems

    @staticmethod
    def _validate_arguments(
        operation: str, arguments: dict[str, Any], schema: dict[str, Any]
    ) -> None:
        type_names = {
            "string": "a string",
            "integer": "an integer",
            "number": "a number",
            "boolean": "a boolean",
            "array": "an array",
            "object": "an object",
        }
        properties = schema.get("properties") or {}
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            allowed = ", ".join(sorted(properties)) or "no arguments"
            raise AgentWebError(
                f"{operation} received unknown input field"
                f"{'s' if len(unknown) != 1 else ''}: {', '.join(unknown)}. "
                f"Allowed fields: {allowed}",
                code="invalid_input",
                field=unknown[0] if len(unknown) == 1 else None,
            )
        for name, value in arguments.items():
            if value is None:
                continue
            specification = properties[name]
            expected = specification.get("type")
            validators = {
                "string": lambda item: isinstance(item, str),
                "integer": lambda item: (
                    isinstance(item, int) and not isinstance(item, bool)
                ),
                "number": lambda item: (
                    isinstance(item, (int, float)) and not isinstance(item, bool)
                ),
                "boolean": lambda item: isinstance(item, bool),
                "array": lambda item: isinstance(item, list),
                "object": lambda item: isinstance(item, dict),
            }
            expected_types = expected if isinstance(expected, list) else [expected]
            valid = [validators[item] for item in expected_types if item in validators]
            if valid and not any(check(value) for check in valid):
                expected_label = " or ".join(
                    type_names[item] for item in expected_types if item in type_names
                )
                raise AgentWebError(
                    f"{operation} input {name!r} must be {expected_label}",
                    code="invalid_input",
                    field=name,
                )
            choices = specification.get("enum")
            if choices is not None and value not in choices:
                raise AgentWebError(
                    f"{operation} input {name!r} must be one of: "
                    + ", ".join(map(str, choices)),
                    code="invalid_input",
                    field=name,
                )
            if isinstance(value, str):
                if name in (schema.get("required") or []) and not value.strip():
                    raise AgentWebError(
                        f"{operation} input {name!r} cannot be empty",
                        code="invalid_input",
                        field=name,
                    )
                minimum_length = specification.get("minLength")
                maximum_length = specification.get("maxLength")
                if minimum_length is not None and len(value) < minimum_length:
                    raise AgentWebError(
                        f"{operation} input {name!r} must contain at least {minimum_length} characters",
                        code="invalid_input",
                        field=name,
                    )
                if maximum_length is not None and len(value) > maximum_length:
                    raise AgentWebError(
                        f"{operation} input {name!r} must contain at most {maximum_length} characters",
                        code="invalid_input",
                        field=name,
                    )
                pattern = specification.get("pattern")
                if isinstance(pattern, str) and re.search(pattern, value) is None:
                    raise AgentWebError(
                        f"{operation} input {name!r} has an invalid format",
                        code="invalid_input",
                        field=name,
                    )
            if isinstance(value, list):
                minimum_items = specification.get("minItems")
                maximum_items = specification.get("maxItems")
                if minimum_items is not None and len(value) < minimum_items:
                    raise AgentWebError(
                        f"{operation} input {name!r} must contain at least {minimum_items} items",
                        code="invalid_input",
                        field=name,
                    )
                if maximum_items is not None and len(value) > maximum_items:
                    raise AgentWebError(
                        f"{operation} input {name!r} must contain at most {maximum_items} items",
                        code="invalid_input",
                        field=name,
                    )
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                minimum = specification.get("minimum")
                maximum = specification.get("maximum")
                if isinstance(minimum, (int, float)) and value < minimum:
                    raise AgentWebError(
                        f"{operation} input {name!r} must be at least {minimum}",
                        code="invalid_input",
                        field=name,
                    )
                if isinstance(maximum, (int, float)) and value > maximum:
                    raise AgentWebError(
                        f"{operation} input {name!r} must be at most {maximum}",
                        code="invalid_input",
                        field=name,
                    )
