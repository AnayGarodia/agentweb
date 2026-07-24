"""Capture-once response oracles and keyless replay verification for typed ops.

A **response oracle** gives any typed operation a keyless, credential-free drift
check without a developer key. It records, from one real
successful execution of a typed operation, the redacted *shape* of the response
plus optional named assertions. Re-running the operation later and checking the
fresh envelope against the oracle turns that single capture into a permanent,
credential-free drift check: when the site changes its response, the oracle stops
matching and the operation is reported ``drift`` instead of silently returning
degraded data. A clean match earns the ``capture_verified`` tier.

Safety:

* Oracles never store response *values*, only field paths and types (plus the
  redacted input). Assertion paths record existence + type, not content.
* An oracle for a **mutating** operation records the *read-back* that confirms
  the effect, never the mutation itself. Replaying such an oracle never
  re-executes the mutation; the read-back operation is what gets re-run.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .capture import payload_shape, redact_value
from .sdk import AgentWebError

ORACLE_KIND = "agentweb_response_oracle"
ORACLE_SCHEMA_VERSION = 1

# Verification tiers this module can emit.
CAPTURE_VERIFIED = "capture_verified"
DRIFT = "drift"
STRUCTURAL_ONLY = "structural_only"
# Aggregate statuses used by a whole-directory oracle run.
SKIPPED = "skipped"
INCONCLUSIVE = "inconclusive"
ORACLE_SUFFIX = ".oracle.json"

_INDEX_TOKEN = re.compile(r"([^\[\].]+)|\[(\d+)\]")


def discover_oracles(directory: Path) -> list[Path]:
    """Return sorted ``*.oracle.json`` files under ``directory`` (recursive)."""
    return sorted(p for p in directory.rglob("*" + ORACLE_SUFFIX) if p.is_file())


def oracle_age_days(oracle: dict[str, Any], *, now: int | None = None) -> int | None:
    """Whole days since the oracle was captured, or ``None`` if unknown."""
    captured = oracle.get("captured_at_unix")
    if not isinstance(captured, (int, float)):
        return None
    now_unix = int(time.time()) if now is None else now
    return max(0, int((now_unix - int(captured)) // 86400))


def classify_oracle_replay(oracle: dict[str, Any], *, via_browser: bool) -> str:
    """Decide how a stored oracle should be replayed in a directory run.

    * ``mutating`` — records a read-back, never auto-replayed (would need a
      signed-in session, so it is skipped in a keyless run).
    * ``browser_required`` — captured browser-assisted but the run did not opt
      into a browser, so it is skipped.
    * ``browser`` — replay inside the authenticated Chrome via CDP.
    * ``browserless`` — replay over ordinary HTTP.
    """
    if oracle.get("mutating"):
        return "mutating"
    if oracle.get("execution") == "browser_assisted":
        return "browser" if via_browser else "browser_required"
    return "browserless"


def resolve_json_path(payload: Any, path: str) -> tuple[bool, Any]:
    """Resolve a small ``$.a.b[0].c`` (or ``$.a.b.0.c``) JSONPath subset.

    Returns ``(found, value)``; ``found`` is ``False`` when any segment is
    missing so callers can tell "absent" apart from "present but null".
    """
    cleaned = path.strip()
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    cleaned = cleaned.lstrip(".")
    current = payload
    for name, index in _INDEX_TOKEN.findall(cleaned):
        if name:
            if name.isdigit():
                position = int(name)
                if not isinstance(current, (list, tuple)) or position >= len(current):
                    return False, None
                current = current[position]
            elif isinstance(current, dict) and name in current:
                current = current[name]
            else:
                return False, None
        else:
            position = int(index)
            if not isinstance(current, (list, tuple)) or position >= len(current):
                return False, None
            current = current[position]
    return True, current


def build_response_oracle(
    site: str,
    operation: str,
    input_args: dict[str, Any],
    envelope: dict[str, Any],
    *,
    mutating: bool = False,
    assert_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Capture the redacted shape of one successful execution as a drift oracle.

    ``assert_paths`` are envelope-rooted JSONPaths (``$.data.name``) that must
    keep resolving to a non-null value of the same type on replay; each is
    validated against ``envelope`` now so a typo fails at capture time.
    """
    if not envelope.get("ok", True):
        raise AgentWebError(
            "Refusing to build an oracle from a failed execution",
            code="oracle_capture_failed",
        )
    assertions: list[dict[str, Any]] = []
    for path in assert_paths or []:
        found, value = resolve_json_path(envelope, path)
        if not found or value is None:
            raise AgentWebError(
                f"Assertion path {path!r} did not resolve in the captured response",
                code="oracle_assertion_unresolved",
            )
        assertions.append(
            {"name": path, "path": path, "type": type(value).__name__}
        )
    return {
        "kind": ORACLE_KIND,
        "schema_version": ORACLE_SCHEMA_VERSION,
        "site": site,
        "operation": operation,
        "input": redact_value(input_args),
        "captured_at_unix": int(time.time()),
        "mutating": bool(mutating),
        "observed": {
            "ok": bool(envelope.get("ok", True)),
            "operation": envelope.get("operation"),
            "data_shape": payload_shape(envelope.get("data")),
            "assertions": assertions,
        },
    }


def _empty_collection_at_item_path(data: Any, path: str) -> bool:
    """True when ``path`` only vanished because a captured list is now empty.

    A captured ``items.0.title`` legitimately disappears when ``items`` replays
    as ``[]``; that is not drift, just an empty result, so it must not fail.
    """
    parts = path.split(".")
    if "0" not in parts:
        return False
    value: Any = data
    for part in parts[: parts.index("0")]:
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return isinstance(value, list) and not value


def verify_response_oracle(
    oracle: dict[str, Any], envelope: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Validate an oracle and, when given a fresh envelope, check for drift."""
    errors: list[str] = []
    if (
        oracle.get("kind") != ORACLE_KIND
        or oracle.get("schema_version") != ORACLE_SCHEMA_VERSION
    ):
        errors.append("unsupported response oracle")
    site = oracle.get("site")
    operation = oracle.get("operation")
    if not isinstance(site, str) or not site:
        errors.append("oracle is missing site")
    if not isinstance(operation, str) or not operation:
        errors.append("oracle is missing operation")

    base = {"site": site, "operation": operation}
    if envelope is None:
        return {
            **base,
            "passed": not errors,
            "status": STRUCTURAL_ONLY,
            "errors": errors,
        }

    observed = oracle.get("observed") or {}
    mismatches: list[str] = []
    if observed.get("ok") and not envelope.get("ok", True):
        mismatches.append("operation no longer succeeds")

    fresh_data = envelope.get("data")
    fresh_paths = {field["path"] for field in payload_shape(fresh_data)}
    missing = sorted(
        str(field.get("path"))
        for field in observed.get("data_shape") or []
        if str(field.get("path")) not in fresh_paths
        and not _empty_collection_at_item_path(fresh_data, str(field.get("path")))
    )
    if missing:
        shown = ", ".join(missing[:5]) + ("…" if len(missing) > 5 else "")
        mismatches.append(f"response shape lost {len(missing)} captured field(s): {shown}")

    for assertion in observed.get("assertions") or []:
        path = str(assertion.get("path"))
        found, value = resolve_json_path(envelope, path)
        if not found or value is None:
            mismatches.append(f"assertion {assertion.get('name', path)!r} did not resolve")
        elif assertion.get("type") and type(value).__name__ != assertion["type"]:
            mismatches.append(
                f"assertion {assertion.get('name', path)!r} type changed: "
                f"captured {assertion['type']}, replayed {type(value).__name__}"
            )

    passed = not errors and not mismatches
    return {
        **base,
        "passed": passed,
        "status": CAPTURE_VERIFIED if passed else DRIFT,
        "errors": errors,
        "mismatches": mismatches,
    }
