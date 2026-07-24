from __future__ import annotations

import base64
import hashlib
import importlib.resources
import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .sdk import USER_AGENT, AgentWebError
from .storage import StatePaths, contained_path, read_json, safe_component, write_json

# The hosted registry lets every install pick up newly published adapters
# without upgrading the tool. Its index is signed with the pinned key below;
# the host cannot substitute adapters without the private key.
DEFAULT_REMOTE_REGISTRY = (
    "https://raw.githubusercontent.com/AnayGarodia/agentweb/registry/index.json"
)
DEFAULT_REMOTE_PUBLIC_KEY = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAplqSSVJLk4X3IedJ0Et3H1gaHxYBYWQv7mWvmqnS6u0=\n"
    "-----END PUBLIC KEY-----\n"
)

MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_BUNDLE_FILE_BYTES = 25 * 1024 * 1024
HASH = re.compile(r"[0-9a-f]{64}")
SYNC_LOCK = threading.Lock()


def _canonical_index(index: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in index.items() if key != "signature"}
    return json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _load_public_key(value: str) -> Ed25519PublicKey:
    candidate = Path(value).expanduser()
    try:
        is_file = "\n" not in value and len(value) < 4096 and candidate.is_file()
    except OSError:
        is_file = False
    raw = candidate.read_bytes() if is_file else value.encode("utf-8")
    try:
        if b"BEGIN PUBLIC KEY" in raw:
            key = serialization.load_pem_public_key(raw)
        else:
            key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(raw, validate=True)
            )
    except (ValueError, TypeError) as exc:
        raise AgentWebError(
            "Registry public key is not valid Ed25519 PEM or base64"
        ) from exc
    if not isinstance(key, Ed25519PublicKey):
        raise AgentWebError("Registry public key must be Ed25519")
    return key


def _public_key_id(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def verify_signed_index(index: dict[str, Any], trusted_public_key: str) -> str:
    signature = index.get("signature")
    if not isinstance(signature, dict) or signature.get("algorithm") != "ed25519":
        raise AgentWebError("Remote registry index is not signed with Ed25519")
    key = _load_public_key(trusted_public_key)
    key_id = _public_key_id(key)
    if signature.get("key_id") != key_id:
        raise AgentWebError(
            "Remote registry signature key does not match the trusted key"
        )
    try:
        encoded = base64.b64decode(str(signature.get("value") or ""), validate=True)
        key.verify(encoded, _canonical_index(index))
    except (ValueError, InvalidSignature) as exc:
        raise AgentWebError(
            "Remote registry index signature verification failed"
        ) from exc
    return key_id


def _safe_relative(value: str, *, label: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise AgentWebError(f"Unsafe {label}: {value}")
    return path


def _read_limited(response: Any, limit: int, *, label: str) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            declared_size = int(declared)
        except (TypeError, ValueError) as exc:
            raise AgentWebError(f"{label} returned an invalid Content-Length") from exc
        if declared_size < 0:
            raise AgentWebError(f"{label} returned an invalid Content-Length")
        if declared_size > limit:
            raise AgentWebError(f"{label} exceeds AgentWeb's {limit}-byte limit")
    data = response.read(limit + 1)
    if len(data) > limit:
        raise AgentWebError(f"{label} exceeds AgentWeb's {limit}-byte limit")
    return data


def _split_reference(value: str) -> tuple[str, str | None]:
    path, separator, fragment = value.partition("#")
    _safe_relative(path, label="evidence reference")
    return path, fragment if separator else None


def _bundle_reference(bundle: Path, value: str) -> tuple[Path, str | None]:
    relative, fragment = _split_reference(value)
    target = (bundle / relative).resolve()
    resolved_bundle = bundle.resolve()
    if resolved_bundle not in target.parents:
        raise AgentWebError(f"Evidence reference escaped its bundle: {value}")
    return target, fragment


def _fixture_reference(bundle: Path, value: str) -> tuple[Path | None, str | None]:
    relative, fragment = _split_reference(value)
    for parent in (bundle, *bundle.parents):
        candidate = (parent / relative).resolve()
        if candidate.is_file():
            return candidate, fragment
    return None, fragment


MISSING_FRAGMENT = object()


def _json_fragment(path: Path, fragment: str | None) -> Any:
    try:
        value: Any = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return MISSING_FRAGMENT
    if not fragment:
        return value
    for part in fragment.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return MISSING_FRAGMENT
    return value


def _json_has_fragment(path: Path, fragment: str | None) -> bool:
    return _json_fragment(path, fragment) is not MISSING_FRAGMENT


def _source_has_fragment(path: Path, fragment: str | None) -> bool:
    if not fragment:
        return True
    try:
        source = path.read_text()
    except OSError:
        return False
    symbol = fragment.rsplit(".", 1)[-1]
    return bool(
        re.search(rf"\b(?:async\s+)?def\s+{re.escape(symbol)}\s*\(", source)
        or re.search(rf"[\"']{re.escape(symbol)}[\"']\s*:", source)
    )


def _validate_evidence_record(
    record: dict[str, Any], *, bundle: Path, commands: set[str]
) -> str | None:
    required = (
        "command",
        "browser_capture",
        "direct_recipe",
        "fixture",
        "last_verified",
    )
    if not all(
        isinstance(record.get(field), str) and record.get(field) for field in required
    ):
        return "record is missing required non-empty fields"
    command = str(record["command"])
    if command not in commands:
        return f"evidence names undeclared command {command!r}"
    try:
        verified_on = date.fromisoformat(str(record["last_verified"]))
    except ValueError:
        return f"{command}: last_verified is not an ISO date"
    age = (date.today() - verified_on).days
    if age < -1:
        return f"{command}: evidence date is in the future"
    if age > 180:
        return f"{command}: evidence is stale ({age} days old)"
    try:
        browser_path, browser_fragment = _bundle_reference(
            bundle, str(record["browser_capture"])
        )
        recipe_path, recipe_fragment = _bundle_reference(
            bundle, str(record["direct_recipe"])
        )
        fixture_path, fixture_fragment = _fixture_reference(
            bundle, str(record["fixture"])
        )
    except AgentWebError as exc:
        return f"{command}: {exc}"
    if not browser_path.is_file() or not _json_has_fragment(
        browser_path, browser_fragment
    ):
        return f"{command}: browser capture or fragment does not exist"
    if not recipe_path.is_file() or not _source_has_fragment(
        recipe_path, recipe_fragment
    ):
        return f"{command}: direct recipe or symbol does not exist"
    if fixture_path == recipe_path:
        return (
            f"{command}: regression fixture must be independent from the direct recipe"
        )
    fixture_exists = False
    if fixture_path is not None:
        fixture_exists = (
            _json_has_fragment(fixture_path, fixture_fragment)
            if fixture_path.suffix.lower() == ".json"
            else _source_has_fragment(fixture_path, fixture_fragment)
        )
    if not fixture_exists:
        return f"{command}: regression fixture or symbol does not exist"
    if record.get("verification_level") == "semantic" and (
        not fixture_path or fixture_path.suffix.lower() != ".json"
    ):
        return f"{command}: semantic verification requires a portable JSON receipt"
    if fixture_path and fixture_path.suffix.lower() == ".json":
        fixture_value = _json_fragment(fixture_path, fixture_fragment)
        if (
            not isinstance(fixture_value, dict)
            or fixture_value.get("passed") is not True
        ):
            return f"{command}: JSON conformance fixture must explicitly contain passed=true"
        semantic = record.get("verification_level") == "semantic"
        if semantic:
            if fixture_value.get("schema_version") != 2:
                return f"{command}: semantic receipt must use schema_version 2"
            if fixture_value.get("command") != command:
                return f"{command}: semantic receipt command does not match"
            if (
                fixture_value.get("adapter_sha256")
                != hashlib.sha256(recipe_path.read_bytes()).hexdigest()
            ):
                return f"{command}: semantic receipt adapter hash does not match"
            if (
                fixture_value.get("browser_capture_sha256")
                != hashlib.sha256(browser_path.read_bytes()).hexdigest()
            ):
                return (
                    f"{command}: semantic receipt browser-capture hash does not match"
                )
            assertions = fixture_value.get("assertions")
            if (
                not isinstance(assertions, list)
                or not assertions
                or not all(
                    isinstance(assertion, dict)
                    and isinstance(assertion.get("name"), str)
                    and bool(assertion.get("name"))
                    and assertion.get("passed") is True
                    for assertion in assertions
                )
            ):
                return f"{command}: semantic receipt needs named passing assertions"
            runner = fixture_value.get("runner")
            if not (
                isinstance(runner, dict)
                and isinstance(runner.get("name"), str)
                and runner.get("name")
                and isinstance(runner.get("version"), str)
                and runner.get("version")
                and runner.get("exit_code") == 0
            ):
                return f"{command}: semantic receipt needs runner provenance and exit_code=0"
            try:
                datetime.fromisoformat(str(fixture_value.get("verified_at")))
            except ValueError:
                return f"{command}: semantic receipt verified_at is not ISO-8601"
    return None


def _validate_authoring_evidence(
    manifest: dict[str, Any], path: Path | None, commands: set[str]
) -> tuple[dict[str, bool], set[str], list[str]]:
    gates = {
        "catalog_complete": False,
        "matrix_verified": False,
        "browserless_parity": False,
        "tool_quality_verified": False,
    }
    declaration = manifest.get("authoring")
    if declaration is None:
        return gates, set(), []
    errors: list[str] = []
    if not isinstance(declaration, dict):
        return gates, set(), ["authoring must be an object"]
    report_path = path.parent / "authoring-report.json" if path else None
    if not report_path or not report_path.is_file():
        return gates, set(), ["authoring-report.json is missing"]
    try:
        report = json.loads(report_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return gates, set(), [f"could not read authoring-report.json: {exc}"]
    for gate in gates:
        gates[gate] = declaration.get(gate) is True and report.get(gate) is True
        if not gates[gate]:
            errors.append(f"authoring gate {gate} has not passed")
    capabilities = report.get("capabilities") or {}
    terminal = {"verified", "human_only", "blocked", "not_applicable"}
    if not isinstance(capabilities, dict) or any(
        not isinstance(item, dict) or item.get("state") not in terminal
        for item in capabilities.values()
    ):
        gates["matrix_verified"] = False
        errors.append("authoring report contains non-terminal capabilities")
    elif any(
        item.get("in_matrix")
        and item.get("state") == "human_only"
        and not item.get("boundary_evidence")
        for item in capabilities.values()
    ):
        gates["browserless_parity"] = False
        errors.append("human-only capabilities need observed site-boundary evidence")
    contracts = manifest.get("operation_contracts") or {}
    report_operations = report.get("operations") or {}
    runtime_capsules = manifest.get("flow_capsules") or {}
    evidence_capsules = manifest.get("evidence_capsules") or {}
    capsules = {**evidence_capsules, **runtime_capsules}
    if not isinstance(contracts, dict) or set(contracts) != commands:
        gates["tool_quality_verified"] = False
        errors.append(
            "every published command must have exactly one reviewed operation contract"
        )
    if not isinstance(report_operations, dict) or set(report_operations) != commands:
        gates["browserless_parity"] = False
        errors.append("authoring report operations do not match published commands")
    verified: set[str] = set()
    for name in sorted(commands):
        contract = contracts.get(name) or {}
        required_text = ("summary", "when_to_use", "when_not_to_use")
        risk = contract.get("risk") or {}
        authentication = contract.get("authentication") or {}
        contract_errors = contract.get("errors") or []
        examples = contract.get("examples") or []
        verification_plan = contract.get("verification_plan") or {}
        result_policy = contract.get("result") or {}
        if (
            contract.get("reviewed") is not True
            or (contract.get("verification") or {}).get("status") != "passed"
            or any(
                not isinstance(contract.get(field), str)
                or len(contract[field].strip()) < 12
                for field in required_text
            )
            or not isinstance(contract.get("input_schema"), dict)
            or not isinstance(contract.get("output_schema"), dict)
            or not isinstance(contract.get("capabilities"), list)
            or not contract.get("capabilities")
            or risk.get("level")
            not in {
                "read",
                "reversible_write",
                "irreversible_write",
                "human_checkpoint",
            }
            or risk.get("confirmation")
            not in {"never", "always", "when_side_effecting"}
            or not isinstance(risk.get("reversible"), bool)
            or not isinstance(risk.get("idempotent"), bool)
            or not isinstance(authentication.get("required"), bool)
            or not isinstance(contract.get("pagination"), dict)
            or not isinstance(result_policy.get("max_items"), int)
            or not result_policy.get("freshness")
            or not result_policy.get("truncation")
            or not contract_errors
            or any(
                not isinstance(error, dict)
                or not error.get("code")
                or not error.get("message")
                or not isinstance(error.get("retryable"), bool)
                or not error.get("user_action")
                for error in contract_errors
            )
            or not examples
            or not verification_plan.get("assertions")
            or "success" not in (verification_plan.get("required_cases") or [])
        ):
            gates["tool_quality_verified"] = False
            errors.append(
                f"{name}: operation contract quality or review evidence is incomplete"
            )
            continue
        report_operation = report_operations.get(name) or {}
        if report_operation.get("contract_hash") != contract.get("contract_hash"):
            gates["tool_quality_verified"] = False
            errors.append(f"{name}: authoring report contract hash is stale")
            continue
        declaration_value = capsules.get(name)
        relative = (
            declaration_value.get("path")
            if isinstance(declaration_value, dict)
            else declaration_value
        )
        try:
            capsule_relative = _safe_relative(str(relative or ""), label="flow capsule")
        except AgentWebError as exc:
            gates["browserless_parity"] = False
            errors.append(f"{name}: {exc}")
            continue
        capsule_path = path.parent / capsule_relative if path else None
        if not capsule_path or not capsule_path.is_file():
            gates["browserless_parity"] = False
            errors.append(f"{name}: reviewed flow capsule is missing")
            continue
        try:
            capsule = json.loads(capsule_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            gates["browserless_parity"] = False
            errors.append(f"{name}: could not read reviewed flow capsule: {exc}")
            continue
        verification = capsule.get("verification") or {}
        recipe_hash = hashlib.sha256(
            json.dumps(
                capsule.get("recipe"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if (
            verification.get("status") != "passed"
            or verification.get("reviewed") is not True
            or verification.get("contract_hash") != contract.get("contract_hash")
            or verification.get("recipe_hash") != recipe_hash
        ):
            gates["browserless_parity"] = False
            errors.append(f"{name}: semantic evidence is stale or unreviewed")
            continue
        if verification.get("evidence_version") == 2:
            required_cases = set(
                (contract.get("verification_plan") or {}).get("required_cases")
                or ["success"]
            )
            observed_cases = set(verification.get("cases") or [])
            assertions = verification.get("assertions") or []
            runner = verification.get("runner") or {}
            if not required_cases.issubset(observed_cases):
                gates["browserless_parity"] = False
                errors.append(
                    f"{name}: semantic evidence is missing required executed cases"
                )
                continue
            if not assertions or any(
                not isinstance(assertion, dict)
                or not assertion.get("name")
                or assertion.get("passed") is not True
                for assertion in assertions
            ):
                gates["browserless_parity"] = False
                errors.append(
                    f"{name}: semantic evidence needs named passing assertions"
                )
                continue
            if (
                runner.get("name") != "agentweb_author_release_gate"
                or runner.get("version") != 1
                or "semantic_direct_replay" not in (runner.get("interfaces") or [])
            ):
                gates["browserless_parity"] = False
                errors.append(f"{name}: semantic evidence lacks runner provenance")
                continue
        verified.add(name)
    machine_capabilities = {
        name
        for name, item in capabilities.items()
        if isinstance(item, dict)
        and item.get("in_matrix")
        and item.get("state") == "verified"
    }
    covered = {
        capability
        for contract in contracts.values()
        if isinstance(contract, dict)
        for capability in contract.get("capabilities") or []
    }
    if machine_capabilities - covered:
        gates["browserless_parity"] = False
        errors.append("verified capabilities are missing reviewed operations")
    forbidden = commands.intersection(
        {"direct_request", "direct_workflow", "web_action", "web_inspect"}
    )
    if forbidden:
        gates["browserless_parity"] = False
        errors.append("generic request or browser commands cannot count as parity")
    return gates, verified, sorted(set(errors))


def validate_manifest(
    manifest: dict[str, Any], *, path: Path | None = None
) -> dict[str, Any]:
    """Validate the contract every distributable adapter must satisfy."""
    label = str(path) if path else str(manifest.get("name") or "manifest")
    problems: list[str] = []
    for field in ("name", "version", "description", "entrypoint", "base_url"):
        if not isinstance(manifest.get(field), str) or not manifest.get(field):
            problems.append(f"missing non-empty {field}")
    for field in ("name", "version"):
        try:
            safe_component(str(manifest.get(field) or ""), label=field)
        except ValueError as exc:
            problems.append(str(exc))
    base_url = urlparse(str(manifest.get("base_url") or ""))
    if base_url.scheme != "https" or not base_url.hostname:
        problems.append("base_url must be an absolute HTTPS URL")
    domains = manifest.get("allowed_domains")
    if (
        not isinstance(domains, list)
        or not domains
        or not all(isinstance(item, str) and item for item in domains)
    ):
        problems.append("allowed_domains must be a non-empty string array")
        domains = []
    elif any(
        item != item.lower()
        or item.startswith(".")
        or not re.fullmatch(r"[a-z0-9.-]+", item)
        for item in domains
    ):
        problems.append("allowed_domains must contain normalized lowercase hostnames")
    if (
        base_url.hostname
        and domains
        and not any(
            base_url.hostname == domain or base_url.hostname.endswith("." + domain)
            for domain in domains
        )
    ):
        problems.append("base_url host must be covered by allowed_domains")
    coverage = manifest.get("coverage")
    if not isinstance(coverage, dict):
        problems.append("coverage must be an object")
        coverage = {}
    if not isinstance(coverage.get("typed_fast_paths"), list):
        problems.append("coverage.typed_fast_paths must be an array")
    if not isinstance(coverage.get("website_parity"), str) or not coverage.get(
        "website_parity"
    ):
        problems.append("coverage.website_parity must explain the web backstop")
    if not isinstance(coverage.get("not_mapped"), list):
        problems.append("coverage.not_mapped must be an array")
    protocol_fallback = coverage.get("protocol_fallback")
    if protocol_fallback not in {None, "direct_workflow"}:
        problems.append(
            "coverage.protocol_fallback must be direct_workflow when present"
        )
    human_boundaries = coverage.get("human_boundaries", [])
    if not isinstance(human_boundaries, list):
        problems.append("coverage.human_boundaries must be an array when present")
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        problems.append("runtime must be an object")
        runtime = {}
    if not isinstance(runtime.get("browserless_replay"), bool):
        problems.append("runtime.browserless_replay must be a boolean")
    compatibility = manifest.get("compatibility")
    if compatibility is not None:
        version_pattern = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
        if not isinstance(compatibility, dict):
            problems.append("compatibility must be an object when present")
        else:
            minimum_runtime = compatibility.get("minimum_runtime")
            tested_runtimes = compatibility.get("tested_runtimes")
            if not isinstance(minimum_runtime, str) or not version_pattern.fullmatch(
                minimum_runtime
            ):
                problems.append(
                    "compatibility.minimum_runtime must be a semantic runtime version"
                )
            if not isinstance(tested_runtimes, list) or not tested_runtimes or any(
                not isinstance(value, str) or not version_pattern.fullmatch(value)
                for value in tested_runtimes or []
            ):
                problems.append(
                    "compatibility.tested_runtimes must list semantic runtime versions"
                )
            elif minimum_runtime not in tested_runtimes:
                problems.append(
                    "compatibility.tested_runtimes must include minimum_runtime"
                )
    url_routes = manifest.get("url_routes", [])
    if not isinstance(url_routes, list):
        problems.append("url_routes must be an array when present")
        url_routes = []
    for index, route in enumerate(url_routes):
        if not isinstance(route, dict) or not all(
            isinstance(route.get(field), str) and route.get(field)
            for field in ("path_regex", "operation")
        ):
            problems.append(
                f"url_routes[{index}] must declare path_regex and operation"
            )
            continue
        try:
            re.compile(str(route.get("host_regex") or ".*"))
            re.compile(str(route["path_regex"]))
        except re.error as exc:
            problems.append(f"url_routes[{index}] has an invalid regex: {exc}")
    auth = manifest.get("auth")
    if not isinstance(auth, dict):
        problems.append("auth must be an object")
        auth = {}
    strategy = auth.get("strategy")
    if strategy not in {
        "none",
        "cookie_session",
        "oauth2_pkce",
        "personal_access_token",
    }:
        problems.append(
            "auth.strategy must be none, cookie_session, oauth2_pkce, or personal_access_token"
        )
    commands = manifest.get("commands")
    if not isinstance(commands, dict) or not commands:
        problems.append("commands must be a non-empty object")
        commands = {}
    for name, command in commands.items():
        if not isinstance(command, dict):
            problems.append(f"commands.{name} must be an object")
            continue
        schema = command.get("input_schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            problems.append(f"commands.{name}.input_schema must be an object schema")
    for index, route in enumerate(url_routes):
        if isinstance(route, dict) and route.get("operation") not in commands:
            problems.append(
                f"url_routes[{index}] names unknown operation {route.get('operation')!r}"
            )
    setup_operation = auth.get("setup_operation")
    if setup_operation and setup_operation not in commands:
        problems.append(f"auth.setup_operation {setup_operation!r} is not a command")
    entrypoint = manifest.get("entrypoint")
    if isinstance(entrypoint, str):
        try:
            entrypoint_path = _safe_relative(entrypoint, label="entrypoint")
        except AgentWebError as exc:
            problems.append(str(exc))
        else:
            if path and not (path.parent / entrypoint_path).is_file():
                problems.append(f"entrypoint {entrypoint!r} does not exist")
    authoring_gates, authoring_verified, authoring_errors = (
        _validate_authoring_evidence(manifest, path, set(commands))
    )
    authoring_adapter = manifest.get("authoring") is not None
    declared_complete = bool(
        not problems
        and bool(runtime.get("browserless_replay"))
        and coverage.get("not_mapped") == []
        and (
            (
                authoring_adapter
                and all(authoring_gates.values())
                and not authoring_errors
            )
            or (not authoring_adapter and protocol_fallback == "direct_workflow")
        )
    )
    evidence_path = path.parent / "evidence.json" if path else None
    evidence: dict[str, Any] = {}
    evidence_error = None
    evidence_errors: list[str] = []
    if evidence_path and evidence_path.is_file():
        try:
            evidence = json.loads(evidence_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            evidence_error = f"could not read evidence.json: {exc}"
    verified_commands: set[str] = set()
    shape_verified_commands: set[str] = set()
    evidence_records = evidence.get("flows") if isinstance(evidence, dict) else None
    if evidence and evidence.get("schema_version") != 1:
        evidence_errors.append("evidence.json has an unsupported schema_version")
    if isinstance(evidence_records, list) and path:
        for record in evidence_records:
            if not isinstance(record, dict) or record.get("status") != "passed":
                continue
            error = _validate_evidence_record(
                record, bundle=path.parent, commands=set(commands)
            )
            if error:
                evidence_errors.append(error)
                continue
            if record.get("verification_level") == "semantic":
                verified_commands.add(str(record["command"]))
            else:
                shape_verified_commands.add(str(record["command"]))
    elif evidence_records is not None and not isinstance(evidence_records, list):
        evidence_errors.append("evidence.json flows must be an array")
    verified_commands.update(authoring_verified)
    evidence_errors.extend(authoring_errors)
    if evidence_errors and evidence_error is None:
        evidence_error = "; ".join(evidence_errors)
    required_commands = set(commands)
    unverified_commands = sorted(required_commands - verified_commands)
    parity_verified = bool(
        declared_complete
        and required_commands
        and not unverified_commands
        and not evidence_error
    )
    result = {
        "name": manifest.get("name"),
        "version": manifest.get("version"),
        "canonical_domain": manifest.get("canonical_domain")
        or (base_url.hostname or "").removeprefix("www."),
        "url_route_count": len(url_routes),
        "typed_commands": len(commands),
        "mapping_commands": 9 if manifest.get("base_url") else 0,
        "direct_protocol_commands": 4 if manifest.get("base_url") else 0,
        "escape_hatches": sorted(
            set(commands).intersection({"api_request", "api_get", "api_query"})
        ),
        "browserless_replay": bool(runtime.get("browserless_replay")),
        "auth_strategy": strategy,
        "runtime_browser_dependency": not bool(runtime.get("browserless_replay")),
        "not_mapped": coverage.get("not_mapped", []),
        "protocol_fallback": protocol_fallback,
        "human_boundaries": human_boundaries,
        "manifest_complete": declared_complete,
        "declared_protocol_complete": declared_complete,
        "parity_verified": parity_verified,
        "verified_commands": sorted(verified_commands),
        "shape_verified_commands": sorted(shape_verified_commands),
        "unverified_commands": unverified_commands,
        "evidence_path": str(evidence_path) if evidence_path else None,
        "evidence_error": evidence_error,
        "evidence_errors": evidence_errors,
        "protocol_exhaustive": parity_verified,
        "catalog_complete": authoring_gates["catalog_complete"]
        if authoring_adapter
        else parity_verified,
        "matrix_verified": authoring_gates["matrix_verified"]
        if authoring_adapter
        else parity_verified,
        "browserless_parity": authoring_gates["browserless_parity"]
        if authoring_adapter
        else parity_verified,
        "tool_quality_verified": authoring_gates["tool_quality_verified"]
        if authoring_adapter
        else parity_verified,
        "exhaustive": parity_verified,
        "problems": problems,
    }
    if problems:
        raise AgentWebError(f"Invalid adapter manifest {label}: " + "; ".join(problems))
    return result


def audit_registry(root: Path, site: str | None = None) -> dict[str, Any]:
    audits = []
    index_path = root / "index.json"
    if index_path.is_file():
        index = json.loads(index_path.read_text())
        manifest_paths = [
            root
            / "sites"
            / str(entry["name"])
            / str(entry["version"])
            / "manifest.json"
            for entry in index.get("sites", [])
        ]
    else:
        manifest_paths = sorted((root / "sites").glob("*/*/manifest.json"))
    for manifest_path in manifest_paths:
        manifest = json.loads(manifest_path.read_text())
        if site:
            requested = site.lower().strip()
            requested_host = (
                urlparse(
                    requested if "://" in requested else f"https://{requested}"
                ).hostname
                or requested
            )
            names = {
                str(manifest.get("name") or "").lower(),
                str(manifest.get("canonical_domain") or "").lower(),
                *(str(item).lower() for item in manifest.get("aliases") or []),
            }
            domain_match = any(
                requested_host == domain
                or requested_host.endswith("." + str(domain).lower())
                for domain in manifest.get("allowed_domains") or []
            )
            if requested not in names and not domain_match:
                continue
        audits.append(validate_manifest(manifest, path=manifest_path))
    if site and not audits:
        raise AgentWebError(f"No adapter named {site!r} exists in {root}")
    return {
        "root": str(root),
        "site_count": len(audits),
        "exhaustive": bool(audits) and all(item["exhaustive"] for item in audits),
        "sites": audits,
    }


def bundled_registry() -> Path:
    resource = importlib.resources.files("agentweb") / "builtin_registry"
    return Path(str(resource))


def _read_source(source: str, relative: str = "") -> bytes:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        if parsed.scheme != "https":
            raise AgentWebError("Remote registries must use HTTPS")
        url = urljoin(source if source.endswith("/") else source + "/", relative)
        with urlopen(
            Request(url, headers={"User-Agent": USER_AGENT}), timeout=30
        ) as response:
            return _read_limited(
                response, MAX_BUNDLE_FILE_BYTES, label=relative or "registry file"
            )
    root = Path(source).expanduser().resolve()
    target = (root / _safe_relative(relative, label="registry source path")).resolve()
    if root != target and root not in target.parents:
        raise AgentWebError(f"Registry source path escaped its root: {relative}")
    if target.stat().st_size > MAX_BUNDLE_FILE_BYTES:
        raise AgentWebError(f"{relative} exceeds AgentWeb's bundle file size limit")
    return target.read_bytes()


def _index_source(source: str) -> tuple[dict[str, Any], str]:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        if parsed.scheme != "https":
            raise AgentWebError("Remote registries must use HTTPS")
        index_url = (
            source
            if source.endswith("index.json")
            else urljoin(source + "/", "index.json")
        )
        with urlopen(
            Request(index_url, headers={"User-Agent": USER_AGENT}), timeout=30
        ) as response:
            data = _read_limited(response, MAX_INDEX_BYTES, label="registry index")
            index = json.loads(data)
        base = index_url.rsplit("/", 1)[0] + "/"
        return index, base
    root = Path(source).expanduser().resolve()
    if root.is_file():
        return json.loads(root.read_text()), str(root.parent)
    return json.loads((root / "index.json").read_text()), str(root)


class Registry:
    def __init__(self, paths: StatePaths) -> None:
        self.paths = paths

    def configured_source(self) -> str:
        override = os.environ.get("AGENTWEB_REGISTRY") or os.environ.get(
            "SITEPACK_REGISTRY"
        )
        if override:
            return override
        config = read_json(self.paths.registry_config, {}) or {}
        source = config.get("source")
        if not source or source == "builtin":
            return DEFAULT_REMOTE_REGISTRY
        parsed = urlparse(str(source))
        if parsed.scheme not in {"http", "https"}:
            candidate = Path(str(source)).expanduser()
            normalized = candidate.as_posix().rstrip("/")
            bundle = bundled_registry()
            try:
                already_bundle = candidate.resolve() == bundle.resolve()
            except OSError:
                already_bundle = False
            basename = normalized.rsplit("/", 1)[-1]
            # Older releases persisted the physical package path. Tool upgrades
            # replace that directory, and the AgentWeb rename also moved it.
            is_builtin_pointer = normalized.endswith(
                ("/sitepack/builtin_registry", "/agentweb/builtin_registry")
            )
            # A persisted source that points at *another* Python environment's
            # packaged builtin_registry (a stray pipx/venv/site-packages install
            # that ran once) must never win over the bundle shipping with the
            # code running now. Trusting such a stale sibling was the QA
            # "fresh install registers 6 of 12" bug: a broken external
            # environment overrode the verified bundle. A packaged registry
            # always follows its own package, so resolve it to this bundle --
            # whether it still exists or not.
            packaged_pointer = basename == "builtin_registry" and any(
                marker in normalized
                for marker in (
                    "/site-packages/",
                    "/dist-packages/",
                    "/pipx/",
                    "/.venv/",
                    "/venv/",
                )
            )
            missing_builtin = basename == "builtin_registry" and not candidate.exists()
            if already_bundle or (
                is_builtin_pointer or packaged_pointer or missing_builtin
            ):
                # Bundled-registry pointers (current or stale) follow the
                # hosted registry so installs see new adapters without a
                # tool upgrade; the bundle stays as the offline fallback.
                return DEFAULT_REMOTE_REGISTRY
        return str(source)

    def configured_public_key(self) -> str | None:
        override = os.environ.get("AGENTWEB_REGISTRY_PUBLIC_KEY") or os.environ.get(
            "SITEPACK_REGISTRY_PUBLIC_KEY"
        )
        if override:
            return override
        config = read_json(self.paths.registry_config, {}) or {}
        return config.get("trusted_public_key")

    def sync(
        self,
        source: str | None = None,
        *,
        trusted_public_key: str | None = None,
        prune: bool = False,
    ) -> dict[str, Any]:
        resolved = source or self.configured_source()
        if resolved == DEFAULT_REMOTE_REGISTRY:
            try:
                return self._sync(
                    resolved,
                    trusted_public_key=trusted_public_key
                    or DEFAULT_REMOTE_PUBLIC_KEY,
                    prune=prune,
                )
            except (AgentWebError, OSError) as exc:
                result = self._sync(str(bundled_registry()), prune=prune)
                result["fallback"] = (
                    "hosted registry unavailable; installed the bundled "
                    f"adapters instead ({exc})"
                )
                return result
        return self._sync(
            resolved, trusted_public_key=trusted_public_key, prune=prune
        )

    def _sync(
        self,
        source: str,
        *,
        trusted_public_key: str | None = None,
        prune: bool = False,
    ) -> dict[str, Any]:
        with SYNC_LOCK:
            parsed_source = urlparse(source)
            remote = parsed_source.scheme in {"http", "https"}
            index, base = _index_source(source)
            if not isinstance(index, dict) or index.get("schema_version") not in {1, 2}:
                raise AgentWebError("Unsupported registry schema")
            trusted_key = trusted_public_key or self.configured_public_key()
            signer = None
            if remote:
                if not trusted_key:
                    raise AgentWebError(
                        "Remote registries require a trusted Ed25519 public key; "
                        "pass --public-key or set AGENTWEB_REGISTRY_PUBLIC_KEY"
                    )
                signer = verify_signed_index(index, trusted_key)

            entries = index.get("sites")
            if not isinstance(entries, list):
                raise AgentWebError("Registry index sites must be an array")
            installed = read_json(self.paths.installed, {}) or {}
            installed.setdefault("sites", {})
            changed: list[str] = []
            seen_names: set[str] = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    raise AgentWebError("Registry entries must be objects")
                try:
                    name = safe_component(str(entry["name"]), label="registry site")
                    version = safe_component(
                        str(entry["version"]), label="registry version"
                    )
                except (KeyError, ValueError) as exc:
                    raise AgentWebError(str(exc)) from exc
                if name in seen_names:
                    raise AgentWebError(f"Registry contains duplicate site {name!r}")
                seen_names.add(name)
                previous_entry = installed["sites"].get(name) or {}
                destination = contained_path(self.paths.sites, name, version)
                expected_files = entry.get("files") or {}
                if not isinstance(expected_files, dict) or not expected_files:
                    raise AgentWebError(f"Registry entry {name} has no hashed files")
                safe_files: dict[str, str] = {}
                for relative, expected_hash in expected_files.items():
                    relative_path = _safe_relative(str(relative), label="registry path")
                    if not isinstance(expected_hash, str) or not HASH.fullmatch(
                        expected_hash
                    ):
                        raise AgentWebError(
                            f"Invalid SHA-256 for {name}/{version}/{relative}"
                        )
                    safe_files[str(relative_path)] = expected_hash
                if (
                    destination.exists()
                    and installed["sites"].get(name, {}).get("version") == version
                ):
                    bundle_matches = all(
                        (destination / relative).is_file()
                        and hashlib.sha256(
                            (destination / relative).read_bytes()
                        ).hexdigest()
                        == expected_hash
                        for relative, expected_hash in safe_files.items()
                    )
                    if bundle_matches:
                        continue
                temporary = Path(
                    tempfile.mkdtemp(prefix=f".{name}-{version}.", dir=self.paths.root)
                )
                try:
                    for relative, expected_hash in safe_files.items():
                        bundle_path = f"sites/{name}/{version}/{relative}"
                        data = _read_source(base, bundle_path)
                        actual_hash = hashlib.sha256(data).hexdigest()
                        if actual_hash != expected_hash:
                            raise AgentWebError(
                                f"Hash mismatch for {name}/{version}/{relative}"
                            )
                        target = (temporary / relative).resolve()
                        if temporary.resolve() not in target.parents:
                            raise AgentWebError(f"Unsafe registry path: {relative}")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                    manifest_path = temporary / "manifest.json"
                    if not manifest_path.is_file():
                        raise AgentWebError(
                            f"Registry bundle {name}/{version} has no manifest"
                        )
                    manifest = json.loads(manifest_path.read_text())
                    if (
                        manifest.get("name") != name
                        or manifest.get("version") != version
                    ):
                        raise AgentWebError(
                            f"Registry bundle identity does not match {name}/{version}"
                        )
                    validate_manifest(manifest, path=manifest_path)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    backup = destination.with_name(destination.name + ".previous")
                    if backup.exists():
                        shutil.rmtree(backup)
                    if destination.exists():
                        os.replace(destination, backup)
                    try:
                        os.replace(temporary, destination)
                    except Exception:
                        if backup.exists() and not destination.exists():
                            os.replace(backup, destination)
                        raise
                    if backup.exists():
                        shutil.rmtree(backup)
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary)
                installed["sites"][name] = {
                    "version": version,
                    "source": source,
                    "manifest": str(destination / "manifest.json"),
                    "files": safe_files,
                    "signer": signer,
                }
                previous_manifest = previous_entry.get("manifest")
                if previous_manifest:
                    previous_bundle = Path(str(previous_manifest)).parent.resolve()
                    sites_root = self.paths.sites.resolve()
                    if (
                        previous_bundle != destination.resolve()
                        and sites_root in previous_bundle.parents
                        and previous_bundle.is_dir()
                    ):
                        shutil.rmtree(previous_bundle)
                changed.append(name)

            # A registry is an additive source, not the complete universe of
            # AgentWeb adapters. In particular, installing the public core over
            # a richer private or third-party registry must not erase adapters
            # that the incoming registry does not know about. Destructive
            # mirroring remains available only as an explicit maintenance act.
            removed: list[str] = []
            if prune:
                removed = sorted(set(installed["sites"]) - seen_names)
                for name in removed:
                    stale_entry = installed["sites"].pop(name, None) or {}
                    stale_manifest = stale_entry.get("manifest")
                    if stale_manifest:
                        stale_bundle = Path(str(stale_manifest)).parent.resolve()
                        if (
                            self.paths.sites.resolve() in stale_bundle.parents
                            and stale_bundle.is_dir()
                        ):
                            shutil.rmtree(stale_bundle)
            write_json(self.paths.installed, installed)
            try:
                is_builtin = (
                    Path(source).expanduser().resolve() == bundled_registry().resolve()
                )
            except (OSError, RuntimeError):
                is_builtin = False
            config: dict[str, Any] = {
                "source": "builtin" if is_builtin else source,
                "last_sync_at": time.time(),
            }
            if trusted_key:
                key = _load_public_key(trusted_key)
                config["trusted_public_key"] = base64.b64encode(
                    key.public_bytes(
                        serialization.Encoding.Raw, serialization.PublicFormat.Raw
                    )
                ).decode("ascii")
            write_json(self.paths.registry_config, config)
            return {
                "source": source,
                "changed": changed,
                "removed": removed,
                "signer": signer,
                "available": sorted(installed["sites"]),
            }

    def installed(self) -> dict[str, Any]:
        return (read_json(self.paths.installed, {}) or {}).get("sites", {})

    def manifest(self, site: str) -> dict[str, Any]:
        entry = self.installed().get(site)
        if not entry:
            raise AgentWebError(f"Site {site!r} is not installed. Run `agentweb sync`.")
        return json.loads(Path(entry["manifest"]).read_text())


def generate_registry_keypair(private_path: Path, public_path: Path) -> dict[str, Any]:
    if private_path.exists() or public_path.exists():
        raise AgentWebError("Refusing to overwrite an existing registry signing key")
    key = Ed25519PrivateKey.generate()
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    private_path.chmod(0o600)
    public_path.chmod(0o644)
    return {
        "private_key": str(private_path),
        "public_key": str(public_path),
        "key_id": _public_key_id(key.public_key()),
    }


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise AgentWebError(f"Could not read Ed25519 signing key: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise AgentWebError("Registry signing key must be Ed25519")
    return key


def build_index(root: Path, *, signing_key: Path | None = None) -> dict[str, Any]:
    sites_root = root / "sites"
    entries = []
    manifests: dict[str, tuple[tuple[Any, ...], Path]] = {}
    for manifest_path in sorted(sites_root.glob("*/*/manifest.json")):
        manifest = json.loads(manifest_path.read_text())
        name = str(manifest.get("name") or manifest_path.parent.parent.name)
        version = str(manifest.get("version") or manifest_path.parent.name)
        version_key: tuple[Any, ...] = tuple(
            (1, int(part)) if part.isdigit() else (0, part)
            for part in re.split(r"[.-]", version)
        )
        current = manifests.get(name)
        if current is None or version_key > current[0]:
            manifests[name] = (version_key, manifest_path)
    for _, manifest_path in sorted(manifests.values(), key=lambda item: str(item[1])):
        version_dir = manifest_path.parent
        manifest = json.loads(manifest_path.read_text())
        validate_manifest(manifest, path=manifest_path)
        files: dict[str, str] = {}
        for path in sorted(version_dir.rglob("*")):
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            ):
                relative = str(path.relative_to(version_dir))
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(
            {"name": manifest["name"], "version": manifest["version"], "files": files}
        )
    index: dict[str, Any] = {
        "schema_version": 2 if signing_key else 1,
        "sites": entries,
    }
    if signing_key:
        key = _load_private_key(signing_key)
        index["signature"] = {
            "algorithm": "ed25519",
            "key_id": _public_key_id(key.public_key()),
            "value": base64.b64encode(key.sign(_canonical_index(index))).decode(
                "ascii"
            ),
        }
    (root / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    return index
