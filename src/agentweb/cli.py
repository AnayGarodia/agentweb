from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .analytics import Analytics
from .capture import (
    analyze_network_trace,
    build_flow_capsule,
    verify_flow_capsule,
)
from .connector import (
    authentication_status,
    cancel_authentication,
    connect_site,
    disconnect_site,
    install_agent,
    setup_detected_agents,
)
from .dashboard import serve_dashboard
from .mcp import serve
from .registry import (
    audit_registry,
    build_index,
    bundled_registry,
    generate_registry_keypair,
)
from .runtime import Runtime
from .scaffold import create_adapter
from .sdk import AgentWebError
from .storage import Cache, StatePaths


def emit(value: Any, pretty: bool = True) -> None:
    print(json.dumps(value, indent=2 if pretty else None, sort_keys=pretty))


def parse_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AgentWebError(f"Invalid JSON input: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AgentWebError("Input JSON must be an object")
    return parsed


def parse_json_or_file(value: str, *, expected: type, label: str) -> Any:
    """Accept inline JSON for agent callers while retaining file-based CLI input."""
    raw = value
    if not value.lstrip().startswith(("{", "[")):
        path = Path(value.removeprefix("@"))
        try:
            raw = path.read_text()
        except OSError as exc:
            raise AgentWebError(
                f"Could not read {label} file {path}: {exc.strerror or exc}",
                code="authoring_input_unreadable",
                field=label,
            ) from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentWebError(
            f"Invalid {label} JSON: {exc}",
            code="invalid_authoring_input",
            field=label,
        ) from exc
    if not isinstance(parsed, expected):
        expected_name = "array" if expected is list else "object"
        raise AgentWebError(f"{label} must be a JSON {expected_name}", field=label)
    return parsed


def dynamic_site_call(
    argv: list[str], global_args: argparse.Namespace
) -> dict[str, Any]:
    if len(argv) < 2:
        raise AgentWebError(
            "Use `agentweb DOMAIN ACTION --help` to select an operation"
        )
    site, raw_action, *remaining = argv
    action = raw_action.replace("-", "_")
    runtime = Runtime(
        profile=global_args.profile,
        fresh=global_args.fresh,
        mapping_mode=bool(getattr(global_args, "mapping_mode", False)),
    )
    domain_first = "." in site or "://" in site
    resolved = runtime.resolve(site) if domain_first else None
    if resolved:
        site = resolved.site
        action = runtime.resolve_action(site, action)
    manifest = runtime.describe(site)
    command = (manifest.get("commands") or {}).get(action)
    if not command:
        raise AgentWebError(f"Unknown operation {site}.{action}")
    schema = command.get("input_schema") or {"type": "object", "properties": {}}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    positionals = (command.get("cli") or {}).get("positionals") or []
    parser = argparse.ArgumentParser(
        prog=f"agentweb {resolved.domain if resolved else site} {raw_action}"
    )
    for name in positionals:
        prop = properties[name]
        options: dict[str, Any] = {"help": prop.get("description")}
        if prop.get("type") == "integer":
            options["type"] = int
        elif prop.get("type") == "number":
            options["type"] = float
        if prop.get("enum"):
            options["choices"] = prop["enum"]
        parser.add_argument(name, **options)
    for name, prop in properties.items():
        if name in positionals:
            continue
        flag = "--" + name.replace("_", "-")
        prop_type = prop.get("type", "string")
        options: dict[str, Any] = {"help": prop.get("description")}
        if prop_type == "boolean":
            options["action"] = argparse.BooleanOptionalAction
            options["default"] = prop.get("default") if "default" in prop else None
        elif prop_type == "integer":
            options["type"] = int
            if "default" in prop:
                options["default"] = prop["default"]
        elif prop_type == "number":
            options["type"] = float
            if "default" in prop:
                options["default"] = prop["default"]
        elif prop_type == "array":
            options["action"] = "append"
            options["help"] = (prop.get("description") or "") + " (repeatable)"
            item_type = (prop.get("items") or {}).get("type")
            if item_type == "object":
                options["type"] = parse_json
            elif item_type == "integer":
                options["type"] = int
            elif item_type == "number":
                options["type"] = float
        elif prop_type == "object":
            options["type"] = parse_json
            if "default" in prop:
                options["default"] = prop["default"]
        elif "default" in prop:
            options["default"] = prop["default"]
        if prop.get("enum"):
            options["choices"] = prop["enum"]
        if name in required:
            options["required"] = True
        parser.add_argument(flag, dest=name, **options)
    parsed = vars(parser.parse_args(remaining))
    for name, prop in properties.items():
        if prop.get("type") != "array" or not parsed.get(name):
            continue
        item_type = (prop.get("items") or {}).get("type")
        if item_type == "object" or name == "steps":
            converted = []
            for item in parsed[name]:
                if isinstance(item, dict):
                    converted.append(item)
                    continue
                try:
                    value = json.loads(item)
                except json.JSONDecodeError as exc:
                    raise AgentWebError(
                        f"--{name.replace('_', '-')} values must be JSON objects: {exc}"
                    ) from exc
                if not isinstance(value, dict):
                    raise AgentWebError(
                        f"--{name.replace('_', '-')} values must be JSON objects"
                    )
                converted.append(value)
            parsed[name] = converted
    if resolved:
        return runtime.execute(site, action, parsed)
    return runtime.call(f"{site}.{action}", parsed)


def parse_global_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Parse only the leading global options so site operation fields cannot collide."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--mapping-mode", action="store_true")
    prefix: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"--fresh", "--compact", "--mapping-mode"}:
            prefix.append(token)
            index += 1
            continue
        if token == "--profile":
            if index + 1 >= len(argv):
                parser.error("argument --profile: expected one argument")
            prefix.extend(argv[index : index + 2])
            index += 2
            continue
        if token.startswith("--profile="):
            prefix.append(token)
            index += 1
            continue
        break
    return parser.parse_args(prefix), argv[index:]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentweb",
        description="Use websites through fast, structured commands for coding agents.",
    )
    parser.add_argument("--profile", default="default", help="Local account profile")
    parser.add_argument("--fresh", action="store_true", help="Bypass read cache")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    parser.add_argument(
        "--mapping-mode",
        action="store_true",
        help="Expose internal browser capture/debug commands to adapter authors",
    )
    subparsers = parser.add_subparsers(dest="command")

    sync = subparsers.add_parser("sync", help="Install or update website adapters")
    sync.add_argument("--registry", help="Registry directory or HTTPS URL")
    sync.add_argument(
        "--public-key",
        help="Trusted Ed25519 public key path or base64 value required for remote registries",
    )
    sync.add_argument(
        "--prune",
        action="store_true",
        help="Remove installed adapters absent from this registry (off by default)",
    )
    subparsers.add_parser("sites", help="List websites available to AgentWeb")
    subparsers.add_parser("profiles", help="List local named account profiles")
    capabilities = subparsers.add_parser(
        "capabilities",
        help="List the actions available for a website",
    )
    capabilities.add_argument("target")
    capabilities.add_argument("--query")
    capabilities.add_argument("--limit", type=int, default=50)
    capabilities.add_argument(
        "--live", action="store_true", help="Also verify current account/session state"
    )
    get = subparsers.add_parser(
        "get",
        help="Resolve a website URL and fetch it through the best typed operation",
    )
    get.add_argument("url")
    run = subparsers.add_parser(
        "run", help="Run one operation through the stable domain-first agent interface"
    )
    run.add_argument("target")
    run.add_argument("action")
    run.add_argument("--input", default="{}")
    run.add_argument("--idempotency-key")
    task = subparsers.add_parser(
        "task", help="Run a task-shaped website operation with retry protection"
    )
    task.add_argument("target")
    task.add_argument("action")
    task.add_argument("--input", default="{}")
    task.add_argument("--idempotency-key")
    workflow = subparsers.add_parser(
        "workflow", help="Run a JSON sequence of same-site operations"
    )
    workflow.add_argument("target")
    workflow.add_argument(
        "--steps",
        required=True,
        help="JSON array or @path; use $STEP.data.FIELD references",
    )
    describe = subparsers.add_parser("describe", help="Describe mapped operations")
    describe.add_argument("site")
    describe.add_argument("--operation")
    describe.add_argument("--category")
    describe.add_argument("--query")
    describe.add_argument("--cursor")
    describe.add_argument("--limit", type=int, default=50)
    describe.add_argument(
        "--parity-details",
        action="store_true",
        help="Include full verified and unverified operation lists",
    )
    call = subparsers.add_parser("call", help="Call SITE.ACTION with JSON")
    call.add_argument("operation")
    call.add_argument("--input", default="{}")

    connect = subparsers.add_parser("connect", help="Sign in to a website once")
    connect.add_argument("site")
    connect.add_argument(
        "--mode", choices=["login", "signup", "session"], default="login"
    )
    connect.add_argument("--timeout", type=int, default=600)
    connect.add_argument("--capture-now", action="store_true", help=argparse.SUPPRESS)

    agent = subparsers.add_parser("install-agent", help="Connect AgentWeb to an agent")
    agent.add_argument("agent", choices=["claude", "codex"])
    agent.add_argument("--scope", choices=["local", "user", "project"], default="user")
    agent.add_argument("--dry-run", action="store_true")

    subparsers.add_parser("setup", help="Sync all sites and verify the AgentWeb CLI")

    onboard = subparsers.add_parser(
        "onboard", help="Install the agent tool and connect one website in one flow"
    )
    onboard.add_argument("site")
    onboard.add_argument("--agent", choices=["claude", "codex"], required=True)
    onboard.add_argument(
        "--scope", choices=["local", "user", "project"], default="user"
    )
    onboard.add_argument("--timeout", type=int, default=600)
    onboard.add_argument(
        "--connect",
        action="store_true",
        help="Optionally connect an account now; public operations never require this",
    )

    auth = subparsers.add_parser("auth", help="Manage website authorization")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    status = auth_sub.add_parser("status")
    status.add_argument("site")
    cookie_file = auth_sub.add_parser("import-cookies")
    cookie_file.add_argument("site")
    cookie_file.add_argument("path", type=Path)
    cookie_header = auth_sub.add_parser("import-header")
    cookie_header.add_argument("site")
    cookie_header.add_argument(
        "--header",
        help="Cookie header. Omit to read it from stdin so it does not enter shell history.",
    )
    auth_resume = auth_sub.add_parser(
        "resume", help="Resume an open authorization window"
    )
    auth_resume.add_argument("site")
    auth_resume.add_argument("--timeout", type=int, default=600)
    auth_cancel = auth_sub.add_parser(
        "cancel", help="Cancel an open authorization attempt"
    )
    auth_cancel.add_argument("site")
    auth_disconnect = auth_sub.add_parser(
        "disconnect", help="Remove the website session for this profile"
    )
    auth_disconnect.add_argument("site")
    auth_disconnect.add_argument("--confirm", action="store_true")
    auth_switch = auth_sub.add_parser(
        "switch-account", help="Disconnect and open a fresh account login"
    )
    auth_switch.add_argument("site")
    auth_switch.add_argument("--timeout", type=int, default=600)
    auth_switch.add_argument("--confirm", action="store_true")

    cache = subparsers.add_parser("cache", help="Manage local read cache")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    clear = cache_sub.add_parser("clear")
    clear.add_argument("--site")

    telemetry = subparsers.add_parser(
        "telemetry", help="Inspect or control privacy-safe anonymous analytics"
    )
    telemetry_sub = telemetry.add_subparsers(
        dest="telemetry_command", required=True
    )
    telemetry_sub.add_parser("status", help="Show telemetry state and privacy rules")
    telemetry_sub.add_parser("enable", help="Enable local and configured remote analytics")
    telemetry_sub.add_parser("disable", help="Disable all analytics recording")
    telemetry_sub.add_parser("reset-id", help="Replace the anonymous installation ID")
    telemetry_sub.add_parser("inspect", help="Show exactly what one event contains")
    configure_posthog = telemetry_sub.add_parser(
        "configure-posthog", help="Configure optional aggregate analytics delivery"
    )
    configure_posthog.add_argument("--project-key", required=True)
    configure_posthog.add_argument(
        "--host", default="https://us.i.posthog.com"
    )

    dashboard = subparsers.add_parser(
        "dashboard", help="Open the private AgentWeb usage dashboard on localhost"
    )
    dashboard.add_argument("--port", type=int, default=0)
    dashboard.add_argument("--no-open", action="store_true")

    subparsers.add_parser(
        "mcp", help="Run the scalable four-tool MCP server over stdio"
    )
    mcp_config = subparsers.add_parser(
        "mcp-config", help="Print agent MCP configuration"
    )
    mcp_config.add_argument("--executable", default="agentweb")
    registry_build = subparsers.add_parser(
        "registry-build", help="Hash registry bundles"
    )
    registry_build.add_argument("root", type=Path)
    registry_build.add_argument(
        "--signing-key", type=Path, help="Ed25519 private key used to sign the index"
    )
    registry_keygen = subparsers.add_parser(
        "registry-keygen", help="Create an Ed25519 registry signing keypair"
    )
    registry_keygen.add_argument("--private-key", type=Path, required=True)
    registry_keygen.add_argument("--public-key", type=Path, required=True)
    audit = subparsers.add_parser(
        "audit", help="Verify adapter contracts and declared full-site coverage"
    )
    audit.add_argument("site", nargs="?")
    audit.add_argument("--root", type=Path, default=bundled_registry())
    adapter_new = subparsers.add_parser(
        "adapter-new", help="Scaffold a safe full-site adapter"
    )
    adapter_new.add_argument("site")
    adapter_new.add_argument("--base-url", required=True)
    adapter_new.add_argument("--version", default="0.1.0")
    adapter_new.add_argument("--root", type=Path, default=bundled_registry())
    capture_compile = subparsers.add_parser(
        "capture-compile",
        help="Compile a redacted browser trace into endpoint and recipe drafts",
    )
    capture_compile.add_argument("trace", type=Path)
    capture_compile.add_argument("--operation")
    capture_compile.add_argument("--capsule-out", type=Path)
    verify = subparsers.add_parser(
        "verify", help="Validate and replay a compiled flow capsule"
    )
    verify.add_argument("capsule", type=Path)
    verify.add_argument("--input", default="{}")
    verify.add_argument("--confirm", action="store_true")
    verify.add_argument(
        "--offline",
        action="store_true",
        help="Validate structure without making requests",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    known = {
        "sync",
        "sites",
        "profiles",
        "capabilities",
        "get",
        "run",
        "task",
        "workflow",
        "describe",
        "call",
        "connect",
        "install-agent",
        "setup",
        "onboard",
        "auth",
        "cache",
        "telemetry",
        "dashboard",
        "mcp",
        "mcp-config",
        "registry-build",
        "registry-keygen",
        "audit",
        "adapter-new",
        "capture-compile",
        "verify",
    }
    parser = build_parser()
    try:
        # Treat --version as a global command only when it is the whole command.
        # Dynamic site operations are allowed to expose a field named "version"
        # (for example, `npmjs.com get_version --version latest`).
        if argv == ["--version"]:
            print(__version__)
            return 0
        global_args, remaining = parse_global_args(argv)
        first_command = remaining[0] if remaining else None
        if first_command and first_command not in known:
            result = dynamic_site_call(remaining, global_args)
            emit(result, not global_args.compact)
            return 0
        args = parser.parse_args(remaining)
        args.profile = global_args.profile
        args.fresh = global_args.fresh
        args.compact = global_args.compact
        if not args.command:
            parser.print_help()
            return 0
        if args.command == "mcp":
            return serve()
        paths = StatePaths.discover()
        if args.command == "dashboard":
            return serve_dashboard(
                paths, port=args.port, open_browser=not args.no_open
            )
        analytics = Analytics(paths)
        runtime = Runtime(
            paths,
            profile=args.profile,
            fresh=args.fresh,
            mapping_mode=global_args.mapping_mode,
        )
        if args.command == "sync":
            result = runtime.registry.sync(
                args.registry,
                trusted_public_key=args.public_key,
                prune=args.prune,
            )
        elif args.command == "sites":
            result = runtime.sites()
        elif args.command == "profiles":
            result = runtime.profiles()
        elif args.command == "capabilities":
            result = runtime.capabilities(
                args.target, query=args.query, limit=args.limit, live=args.live
            )
        elif args.command == "get":
            result = runtime.get(args.url)
        elif args.command in {"run", "task"}:
            result = runtime.execute(
                args.target,
                args.action,
                parse_json(args.input),
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "workflow":
            result = runtime.workflow(
                args.target,
                parse_json_or_file(args.steps, expected=list, label="steps"),
            )
        elif args.command == "describe":
            result = runtime.discover(
                args.site,
                operation=args.operation,
                category=args.category,
                query=args.query,
                cursor=args.cursor,
                limit=args.limit,
                include_parity_details=args.parity_details,
            )
        elif args.command == "call":
            result = runtime.call(args.operation, parse_json(args.input))
        elif args.command == "connect":
            connected_at = time.monotonic()
            try:
                result = connect_site(
                    runtime,
                    args.site,
                    mode=args.mode,
                    timeout_seconds=args.timeout,
                    capture_now=args.capture_now,
                )
            except AgentWebError as exc:
                try:
                    connection_site = runtime.resolve(args.site).site
                except AgentWebError:
                    connection_site = None
                analytics.record(
                    "connection_completed",
                    site=connection_site,
                    success=False,
                    duration_ms=(time.monotonic() - connected_at) * 1000,
                    interface="cli",
                    error_code=exc.code,
                )
                raise
            analytics.record(
                "connection_completed",
                site=runtime.resolve(args.site).site,
                success=bool(result.get("connected")),
                duration_ms=(time.monotonic() - connected_at) * 1000,
                interface="cli",
                error_code=None if result.get("connected") else "connection_failed",
            )
        elif args.command == "install-agent":
            result = install_agent(args.agent, scope=args.scope, dry_run=args.dry_run)
            if not args.dry_run:
                analytics.record(
                    "agent_connected",
                    operation=args.agent,
                    success=bool(result.get("installed", True)),
                    interface="cli",
                )
        elif args.command == "setup":
            result = {
                **setup_detected_agents(runtime),
                "interface": "cli",
                "command": "agentweb DOMAIN ACTION [arguments]",
                "sites": sorted(item["name"] for item in runtime.sites()),
                "next": "Restart any detected coding agent, then ask it to use AgentWeb in normal language.",
            }
            analytics.record(
                "setup_completed",
                success=bool(result.get("ready")),
                interface="cli",
            )
            for connection in result.get("agent_connections") or []:
                analytics.record(
                    "agent_connected",
                    operation=connection.get("agent"),
                    success=bool(connection.get("installed")),
                    interface="cli",
                )
        elif args.command == "onboard":
            agent_result = install_agent(args.agent, scope=args.scope)
            sync_result = runtime.registry.sync()
            connection_result = None
            if args.connect:
                connection_result = connect_site(
                    runtime, args.site, timeout_seconds=args.timeout
                )
            result = {
                "ready": True,
                "public_operations_ready": True,
                "agent": agent_result,
                "registry": sync_result,
                "connection": connection_result,
                "authentication": (
                    "connected"
                    if connection_result and connection_result.get("connected")
                    else "lazy; only requested by protected operations"
                ),
            }
        elif args.command == "auth":
            if args.auth_command == "status":
                result = authentication_status(runtime, args.site)
            elif args.auth_command == "resume":
                attempt = authentication_status(runtime, args.site).get("attempt")
                if not attempt or attempt.get("state") not in {
                    "authorizing",
                    "human_required",
                    "verifying",
                }:
                    raise AgentWebError(
                        f"No resumable authorization attempt exists for {args.site}"
                    )
                result = connect_site(
                    runtime,
                    args.site,
                    mode=str(attempt.get("mode") or "login"),
                    timeout_seconds=args.timeout,
                )
            elif args.auth_command == "cancel":
                result = cancel_authentication(runtime, args.site)
            elif args.auth_command in {"disconnect", "switch-account"}:
                if not args.confirm:
                    raise AgentWebError(
                        f"{args.auth_command} removes the saved website session; retry with --confirm"
                    )
                result = disconnect_site(runtime, args.site)
                if args.auth_command == "switch-account":
                    result = connect_site(
                        runtime,
                        args.site,
                        timeout_seconds=args.timeout,
                    )
            else:
                manifest = runtime.describe(args.site)
                adapter = runtime.adapter(args.site)
                session = adapter.session()
            if args.auth_command == "import-cookies":
                result = {
                    "imported": session.import_netscape_cookies(args.path),
                    **session.cookie_summary(),
                }
            elif args.auth_command == "import-header":
                header = (
                    args.header if args.header is not None else sys.stdin.read().strip()
                )
                if not header:
                    raise AgentWebError("Cookie header was empty")
                result = {
                    "imported": session.import_cookie_header(
                        header, manifest.get("cookie_domain", f".{args.site}.com")
                    ),
                    **session.cookie_summary(),
                }
        elif args.command == "cache":
            result = {"deleted": Cache(paths.cache_db).clear(args.site)}
        elif args.command == "telemetry":
            if args.telemetry_command == "status":
                result = analytics.status()
            elif args.telemetry_command == "enable":
                result = analytics.set_enabled(True)
            elif args.telemetry_command == "disable":
                result = analytics.set_enabled(False)
            elif args.telemetry_command == "reset-id":
                result = analytics.reset_installation_id()
            elif args.telemetry_command == "inspect":
                result = analytics.inspect_event()
            else:
                result = analytics.configure_posthog(args.project_key, args.host)
        elif args.command == "capture-compile":
            try:
                trace = json.loads(args.trace.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise AgentWebError(f"Could not read capture trace: {exc}") from exc
            if not isinstance(trace, dict) or trace.get("kind") not in {
                "agentweb_redacted_network_trace",
                "sitepack_redacted_network_trace",
            }:
                raise AgentWebError(
                    "capture-compile requires an AgentWeb redacted network trace"
                )
            if args.capsule_out:
                result = build_flow_capsule(trace, operation=args.operation)
                args.capsule_out.parent.mkdir(parents=True, exist_ok=True)
                args.capsule_out.write_text(
                    json.dumps(result, indent=2, sort_keys=True) + "\n"
                )
                result["written_to"] = str(args.capsule_out)
            else:
                result = analyze_network_trace(trace)
        elif args.command == "verify":
            try:
                capsule = json.loads(args.capsule.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise AgentWebError(f"Could not read flow capsule: {exc}") from exc
            variables = parse_json(args.input)
            structural = verify_flow_capsule(capsule)
            if not structural["passed"] or args.offline:
                result = structural
            else:
                required = set(
                    (capsule.get("recipe") or {}).get("required_inputs") or []
                )
                missing = sorted(required - set(variables))
                if missing:
                    raise AgentWebError(
                        "Missing flow capsule inputs: " + ", ".join(missing),
                        code="missing_input",
                    )
                replay = runtime.call(
                    f"{capsule['site']}.direct_workflow",
                    {
                        "steps": capsule["recipe"]["steps"],
                        "variables": variables,
                        "confirm": args.confirm,
                    },
                )
                result = verify_flow_capsule(capsule, replay)
        elif args.command == "mcp-config":
            result = {
                "mcpServers": {
                    "agentweb": {
                        "command": args.executable,
                        "args": ["mcp"],
                    }
                }
            }
        elif args.command == "registry-build":
            result = build_index(
                args.root.resolve(),
                signing_key=args.signing_key.resolve() if args.signing_key else None,
            )
        elif args.command == "registry-keygen":
            result = generate_registry_keypair(
                args.private_key.resolve(), args.public_key.resolve()
            )
        elif args.command == "audit":
            result = audit_registry(args.root.resolve(), args.site)
        elif args.command == "adapter-new":
            result = create_adapter(
                args.root.resolve(),
                args.site,
                args.base_url,
                version=args.version,
            )
        else:
            raise AgentWebError(f"Unknown command {args.command}")
        emit(result, not args.compact)
        return 0
    except (AgentWebError, FileNotFoundError, KeyError, ValueError) as exc:
        payload = (
            exc.as_dict()
            if isinstance(exc, AgentWebError)
            else {
                "error": "invalid_request",
                "message": str(exc),
                "retryable": False,
            }
        )
        print(json.dumps(payload), file=sys.stderr)
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "error": "internal_error",
                    "message": "AgentWeb encountered an unexpected internal error.",
                    "retryable": False,
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
