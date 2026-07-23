from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from .sdk import AgentWebError
from .storage import atomic_write


SENSITIVE_NAME = re.compile(
    r"(?:^|[-_])(authorization|cookie|password|passwd|secret|token|csrf|xsrf|otp|code|signature|session|credential|card|cvv|cvc)(?:$|[-_])",
    re.I,
)
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-csrf-token",
    "x-xsrf-token",
}

HUMAN_FIELD = re.compile(
    r"captcha|otp|one[-_]?time|verification[-_]?code|passcode", re.I
)
STATIC_RESOURCE = re.compile(
    r"\.(?:avif|css|gif|ico|jpe?g|mjs|png|svg|webp|woff2?|ttf)(?:$|\?)", re.I
)
CONSTANT_FIELD = re.compile(
    r"^(?:ajax[-_]?req|action|client|format|locale|mode|operation|operationname|output|type|version)$",
    re.I,
)
TELEMETRY_ENDPOINT = re.compile(
    r"/(?:gabo-receiver-service/(?:public/)?v\d+/events|events?|telemetry|metrics)(?:/|$)",
    re.I,
)
MUTATING_OPERATION = re.compile(
    r"^(?:add|change|create|delete|edit|follow|insert|move|publish|remove|reorder|replace|save|set|submit|unfollow|unsave|update|upload)",
    re.I,
)
SAFE_REQUEST_HEADERS = {
    "accept",
    "content-type",
    "origin",
    "referer",
    "x-requested-with",
}


def _contains_private(value: str, private_values: set[str]) -> bool:
    return any(
        value == private or (len(private) >= 3 and private in value)
        for private in private_values
    )


def _redact_scalar(name: str, value: Any) -> Any:
    if SENSITIVE_NAME.search(name):
        return "[redacted]"
    return value


def redact_value(value: Any, *, name: str = "") -> Any:
    if SENSITIVE_NAME.search(name):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(key): redact_value(item, name=str(key)) for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item, name=name) for item in value]
    return _redact_scalar(name, value)


def redact_url(url: str, private_values: set[str] | None = None) -> str:
    parsed = urlparse(url)
    private = private_values or set()
    query = [
        (
            name,
            "[redacted]"
            if SENSITIVE_NAME.search(name) or _contains_private(value, private)
            else value,
        )
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    safe_segments = [
        quote("[redacted]", safe="")
        if _contains_private(unquote(segment), private)
        else segment
        for segment in parsed.path.split("/")
    ]
    return urlunparse(
        parsed._replace(
            path="/".join(safe_segments), query=urlencode(query, doseq=True)
        )
    )


def redact_headers(
    headers: dict[str, Any] | None, private_values: set[str] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in (headers or {}).items():
        lowered = str(name).lower()
        if lowered in SENSITIVE_HEADERS or SENSITIVE_NAME.search(lowered):
            result[str(name)] = "[redacted]"
            continue
        safe_value = value
        if isinstance(safe_value, str):
            if _contains_private(safe_value, private_values or set()):
                safe_value = "[redacted]"
        result[str(name)] = safe_value
    return result


def _redact_private_values(value: Any, private_values: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            name: _redact_private_values(item, private_values)
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_private_values(item, private_values) for item in value]
    if isinstance(value, str) and _contains_private(value, private_values):
        return "[redacted]"
    return value


def redact_post_data(value: str | None, private_values: set[str] | None = None) -> Any:
    if not value:
        return None
    if len(value) > 100_000:
        return {"format": "omitted", "bytes": len(value.encode("utf-8"))}
    try:
        redacted = redact_value(json.loads(value))
        return {
            "format": "json",
            "value": _redact_private_values(redacted, private_values or set()),
        }
    except (json.JSONDecodeError, TypeError):
        pass
    pairs = parse_qsl(value, keep_blank_values=True)
    if pairs and urlencode(pairs, doseq=True) == value.replace("%20", "+"):
        return {
            "format": "form",
            "value": [
                [
                    name,
                    "[redacted]"
                    if SENSITIVE_NAME.search(name)
                    or _contains_private(item, private_values or set())
                    else item,
                ]
                for name, item in pairs
            ],
        }
    # Arbitrary bodies may contain private messages or credentials. The mapper
    # needs the shape and digest, not a copy of potentially sensitive content.
    return {
        "format": "opaque",
        "bytes": len(value.encode("utf-8")),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


def _body_text(raw: dict[str, Any], max_body_bytes: int) -> tuple[str, int, bool]:
    value = str(raw.get("body") or "")
    if raw.get("base64Encoded"):
        try:
            payload = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            payload = b""
    else:
        payload = value.encode("utf-8", errors="replace")
    original_bytes = len(payload)
    truncated = original_bytes > max_body_bytes
    return (
        payload[:max_body_bytes].decode("utf-8", errors="replace"),
        original_bytes,
        truncated,
    )


def capture_response_bodies(
    client: Any,
    events: list[dict[str, Any]],
    *,
    allowed_domains: list[str],
    max_body_bytes: int = 1_000_000,
    max_bodies: int = 50,
) -> dict[str, dict[str, Any]]:
    """Read bounded textual response bodies before the CDP target is closed.

    The raw bodies are returned only in memory. ``compile_network_trace`` turns
    them into field/shape summaries and never writes their content to disk.
    """
    candidates: list[str] = []
    seen: set[str] = set()
    for event in events:
        if event.get("method") != "Network.responseReceived":
            continue
        params = event.get("params") or {}
        response = params.get("response") or {}
        request_id = str(params.get("requestId") or "")
        url = str(response.get("url") or "")
        mime = str(response.get("mimeType") or "").lower()
        textual = mime.startswith("text/") or any(
            value in mime
            for value in ("json", "xml", "javascript", "x-www-form-urlencoded")
        )
        if (
            request_id
            and request_id not in seen
            and _allowed(url, allowed_domains)
            and textual
            and len(candidates) < max_bodies
        ):
            candidates.append(request_id)
            seen.add(request_id)
    bodies: dict[str, dict[str, Any]] = {}
    for request_id in candidates:
        try:
            raw = client.call("Network.getResponseBody", {"requestId": request_id})
            text, original_bytes, truncated = _body_text(raw or {}, max_body_bytes)
        except (AgentWebError, OSError):
            continue
        bodies[request_id] = {
            "text": text,
            "bytes": original_bytes,
            "truncated": truncated,
        }
    return bodies


def _json_fields(value: Any, *, limit: int = 500) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []

    def visit(item: Any, path: str) -> None:
        if len(fields) >= limit:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                visit(child, f"{path}.{key}" if path else str(key))
        elif isinstance(item, list):
            if item:
                visit(item[0], f"{path}.0" if path else "0")
        else:
            name = path.rsplit(".", 1)[-1]
            fields.append(
                {
                    "path": path,
                    "type": type(item).__name__,
                    "sensitive": bool(SENSITIVE_NAME.search(name)),
                }
            )

    visit(value, "")
    return fields


def summarize_response_body(
    body: dict[str, Any] | None, mime_type: str | None
) -> dict[str, Any] | None:
    """Describe a response body without retaining response values."""
    if not body:
        return None
    text = str(body.get("text") or "")
    base = {
        "bytes": int(body.get("bytes") or len(text.encode("utf-8"))),
        "truncated": bool(body.get("truncated")),
    }
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if payload is not None:
        return {**base, "format": "json", "fields": _json_fields(payload)}
    lowered_mime = str(mime_type or "").lower()
    if "html" in lowered_mime or "<html" in text[:1000].lower():
        soup = BeautifulSoup(text, "html.parser")
        fields = []
        for node in soup.select("input[name], meta[name], meta[property]")[:500]:
            name = str(node.get("name") or node.get("property") or "")
            if not name:
                continue
            selector = (
                f'{node.name}[name="{name}"]'
                if node.get("name")
                else f'{node.name}[property="{name}"]'
            )
            attribute = "value" if node.name == "input" else "content"
            fields.append(
                {
                    "path": name,
                    "source": "html",
                    "selector": selector,
                    "attribute": attribute,
                    "sensitive": bool(SENSITIVE_NAME.search(name)),
                }
            )
        # Some sites bootstrap short-lived request tokens inside inline JSON
        # rather than a form or meta tag. Record only the key and a reusable
        # extraction pattern; never retain the token value itself.
        seen_paths = {str(field["path"]) for field in fields}
        for match in re.finditer(
            r'["\'](?P<name>[A-Za-z0-9_.-]*(?:csrf|xsrf|token)[A-Za-z0-9_.-]*)["\']\s*:\s*["\'][^"\']+["\']',
            text,
            re.I,
        ):
            name = match.group("name")
            if name in seen_paths or len(fields) >= 500:
                continue
            seen_paths.add(name)
            fields.append(
                {
                    "path": name,
                    "source": "html_regex",
                    "pattern": rf'["\']{re.escape(name)}["\']\s*:\s*["\']([^"\']+)["\']',
                    "sensitive": True,
                }
            )
        return {**base, "format": "html", "fields": fields}
    pairs = parse_qsl(text, keep_blank_values=True)
    if pairs and urlencode(pairs, doseq=True) == text.replace("%20", "+"):
        return {
            **base,
            "format": "form",
            "fields": [
                {
                    "path": name,
                    "type": "str",
                    "sensitive": bool(SENSITIVE_NAME.search(name)),
                }
                for name, _value in pairs[:500]
            ],
        }
    return {
        **base,
        "format": "opaque",
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _allowed(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def compile_network_trace(
    events: list[dict[str, Any]],
    *,
    site: str,
    profile: str,
    allowed_domains: list[str],
    action_steps: list[dict[str, Any]],
    page_before: dict[str, Any] | None,
    page_after: dict[str, Any] | None,
    response_bodies: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    private_values = {
        str(step[field])
        for step in action_steps
        if isinstance(step, dict)
        for field in ("value", "prompt_text")
        if step.get(field) not in (None, "")
    }
    requests: dict[str, dict[str, Any]] = {}
    ordered: list[str] = []
    for event in events:
        method = event.get("method")
        params = event.get("params") or {}
        request_id = str(params.get("requestId") or "")
        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            url = str(request.get("url") or "")
            if not request_id or not _allowed(url, allowed_domains):
                continue
            if request_id not in requests:
                ordered.append(request_id)
            item = requests.setdefault(request_id, {})
            item.update(
                {
                    "request_id": request_id,
                    "resource_type": params.get("type"),
                    "initiator_type": (params.get("initiator") or {}).get("type"),
                    "method": request.get("method"),
                    "url": redact_url(url, private_values),
                    "headers": redact_headers(request.get("headers"), private_values),
                    "post_data": redact_post_data(
                        request.get("postData"), private_values
                    ),
                    "timestamp": params.get("timestamp"),
                }
            )
            redirect = params.get("redirectResponse")
            if redirect:
                item["redirect_from"] = {
                    "status": redirect.get("status"),
                    "url": redact_url(str(redirect.get("url") or ""), private_values),
                }
        elif method == "Network.responseReceived" and request_id in requests:
            response = params.get("response") or {}
            requests[request_id]["response"] = {
                "status": response.get("status"),
                "url": redact_url(str(response.get("url") or ""), private_values),
                "mime_type": response.get("mimeType"),
                "protocol": response.get("protocol"),
                "from_disk_cache": response.get("fromDiskCache"),
                "from_service_worker": response.get("fromServiceWorker"),
                "headers": redact_headers(response.get("headers"), private_values),
            }
            body_summary = summarize_response_body(
                (response_bodies or {}).get(request_id), response.get("mimeType")
            )
            if body_summary:
                requests[request_id]["response_body"] = body_summary
        elif method == "Network.loadingFinished" and request_id in requests:
            requests[request_id]["encoded_data_length"] = params.get(
                "encodedDataLength"
            )
        elif method == "Network.loadingFailed" and request_id in requests:
            requests[request_id]["failure"] = {
                "error_text": params.get("errorText"),
                "canceled": params.get("canceled"),
                "blocked_reason": params.get("blockedReason"),
            }

    def page_summary(page: dict[str, Any] | None) -> dict[str, Any] | None:
        if not page:
            return None
        return {
            "url": redact_url(str(page.get("url") or ""), private_values),
            "title_present": bool(page.get("title")),
            "control_count": page.get("control_count"),
        }

    safe_steps = []
    for raw_step in action_steps:
        step = redact_value(raw_step)
        if isinstance(step, dict):
            for field in ("value", "prompt_text", "path"):
                if field in step:
                    step[field] = "[redacted]"
        safe_steps.append(step)

    trace = {
        "schema_version": 2,
        "kind": "agentweb_redacted_network_trace",
        "site": site,
        "profile": profile,
        "captured_at_unix": time.time(),
        "redaction": {
            "secret_headers": True,
            "secret_fields": True,
            "opaque_body_content": False,
            "response_bodies": "shape_only",
        },
        "action_steps": safe_steps,
        "page_before": page_summary(page_before),
        "page_after": page_summary(page_after),
        "request_count": len(ordered),
        "requests": [requests[request_id] for request_id in ordered],
    }
    trace["compiler"] = analyze_network_trace(trace)
    return trace


def _body_fields(post_data: Any) -> list[tuple[str, Any]]:
    if not isinstance(post_data, dict):
        return []
    value = post_data.get("value")
    if post_data.get("format") == "form" and isinstance(value, list):
        return [
            (str(pair[0]), pair[1])
            for pair in value
            if isinstance(pair, list) and len(pair) == 2
        ]
    if post_data.get("format") == "json" and isinstance(value, dict):
        return [(str(name), item) for name, item in value.items()]
    return []


def _request_field_value(
    name: str, value: Any, *, variable: str | None = None
) -> tuple[Any, bool, bool]:
    """Return recipe value, whether it is input, and whether it is human-bound."""
    variable = variable or (
        re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower() or "value"
    )
    human = bool(HUMAN_FIELD.search(name))
    sensitive = bool(SENSITIVE_NAME.search(name))
    if human or sensitive or value == "[redacted]":
        return f"{{{variable}}}", True, human
    if name.lower() in {"p", "path", "route"} and "/" in str(value):
        return value, False, False
    if CONSTANT_FIELD.fullmatch(name) or str(value).lower() in {
        "true",
        "false",
        "null",
    }:
        return value, False, False
    return f"{{{variable}}}", True, False


def _canonical_secret_name(name: str) -> str:
    value = re.sub(r"^(?:x[-_])?(?:amzn|api)[-_]", "", name, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _response_extraction(request: dict[str, Any], name: str) -> dict[str, Any] | None:
    body = request.get("response_body") or {}
    for field in body.get("fields") or []:
        path = str(field.get("path") or "")
        if _canonical_secret_name(path.rsplit(".", 1)[-1]) != _canonical_secret_name(
            name
        ):
            continue
        if field.get("source") == "html":
            return {
                "source": "html",
                "selector": field["selector"],
                "attribute": field["attribute"],
            }
        if field.get("source") == "html_regex":
            return {"source": "regex", "pattern": field["pattern"]}
        if body.get("format") == "json":
            return {"source": "json", "path": path}
        if body.get("format") == "form":
            return {
                "source": "regex",
                "pattern": rf"(?:^|&){re.escape(name)}=([^&]*)",
            }
    response_headers = (request.get("response") or {}).get("headers") or {}
    if any(str(header).lower() == name.lower() for header in response_headers):
        return {"source": "header", "name": name}
    return None


def _is_replay_candidate(request: dict[str, Any]) -> bool:
    url = str(request.get("url") or "")
    host = (urlparse(url).hostname or "").lower()
    if host in {"unagi.amazon.com", "unagi-na.amazon.com"}:
        return False
    resource_type = str(request.get("resource_type") or "")
    if str(request.get("method") or "").upper() == "OPTIONS":
        return False
    mime = str((request.get("response") or {}).get("mime_type") or "").lower()
    if STATIC_RESOURCE.search(url):
        return False
    if resource_type in {"Image", "Font", "Media", "Stylesheet"}:
        return False
    if any(value in mime for value in ("image/", "font/", "text/css")):
        return False
    return resource_type in {"Document", "Fetch", "XHR"} or bool(
        request.get("post_data")
    )


def _graphql_operation(request: dict[str, Any]) -> str | None:
    post_data = request.get("post_data") or {}
    value = post_data.get("value") if post_data.get("format") == "json" else None
    if not isinstance(value, dict):
        return None
    operation = value.get("operationName")
    return str(operation) if operation else None


def _request_classification(request: dict[str, Any]) -> str:
    url = str(request.get("url") or "")
    if TELEMETRY_ENDPOINT.search(urlparse(url).path):
        return "telemetry"
    operation = _graphql_operation(request)
    if operation:
        return "mutation" if MUTATING_OPERATION.search(operation) else "read"
    method = str(request.get("method") or "GET").upper()
    return "read" if method in {"GET", "HEAD"} else "mutation"


def _step_name(method: str, parsed: Any, index: int) -> str:
    path = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    route = dict(parse_qsl(parsed.query, keep_blank_values=True)).get("p")
    raw = route or path or f"request_{index}"
    value = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    return value[:60] or f"request_{index}"


def analyze_network_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """Compile a redacted browser trace into endpoint and recipe drafts.

    Drafts intentionally keep user-entered values as placeholders and identify
    CAPTCHA/OTP fields as human inputs. They are reviewable adapter-authoring
    output, never automatically installed or replayed.
    """
    replayable = [
        request
        for request in trace.get("requests") or []
        if isinstance(request, dict) and _is_replay_candidate(request)
    ]
    telemetry_omitted = sum(
        1 for request in replayable if _request_classification(request) == "telemetry"
    )
    candidates = [
        request
        for request in replayable
        if _request_classification(request) != "telemetry"
    ]
    endpoints: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    required_inputs: set[str] = set()
    human_inputs: set[str] = set()
    unresolved: set[str] = set()
    dependencies: list[dict[str, Any]] = []
    base_host = urlparse(
        str((trace.get("page_before") or {}).get("url") or "")
    ).hostname or (
        urlparse(str(candidates[0].get("url") or "")).hostname if candidates else None
    )
    session_cookies = False
    variable_names: dict[tuple[str, int], str] = {}
    assigned_variables: dict[str, tuple[str, int]] = {}

    def variable_for(name: str, occurrence: int = 1) -> str:
        key = (name, occurrence)
        if key in variable_names:
            return variable_names[key]
        base = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower() or "value"
        candidate = base if occurrence == 1 else f"{base}_{occurrence}"
        if candidate in assigned_variables and assigned_variables[candidate] != key:
            candidate = f"{candidate}_{hashlib.sha256(name.encode()).hexdigest()[:6]}"
        while candidate in assigned_variables and assigned_variables[candidate] != key:
            candidate += "_2"
        assigned_variables[candidate] = key
        variable_names[key] = candidate
        return candidate

    for index, request in enumerate(candidates, 1):
        parsed = urlparse(str(request.get("url") or ""))
        method = str(request.get("method") or "GET").upper()
        path = parsed.path or "/"
        if parsed.hostname and base_host and parsed.hostname != base_host:
            path = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
        step: dict[str, Any] = {
            "name": _step_name(method, parsed, index),
            "method": method,
            "path": path,
        }
        classification = _request_classification(request)
        graphql_operation = _graphql_operation(request)
        step["classification"] = classification
        if graphql_operation:
            step["graphql_operation"] = graphql_operation
        query: dict[str, Any] = {}
        request_fields: list[tuple[str, str]] = []
        field_occurrences: dict[str, int] = {}
        for name, value in parse_qsl(parsed.query, keep_blank_values=True):
            field_occurrences[name] = field_occurrences.get(name, 0) + 1
            variable = variable_for(name, field_occurrences[name])
            rendered, is_input, human = _request_field_value(
                name, value, variable=variable
            )
            if name in query:
                query[name] = (
                    [*query[name], rendered]
                    if isinstance(query[name], list)
                    else [query[name], rendered]
                )
            else:
                query[name] = rendered
            request_fields.append((name, variable))
            if is_input:
                required_inputs.add(variable)
            if human:
                human_inputs.add(variable)
        if query:
            step["query"] = query

        body_fields = _body_fields(request.get("post_data"))
        if body_fields:
            rendered_body: dict[str, Any] = {}
            for name, value in body_fields:
                field_occurrences[name] = field_occurrences.get(name, 0) + 1
                variable = variable_for(name, field_occurrences[name])
                rendered, is_input, human = _request_field_value(
                    name, value, variable=variable
                )
                if name in rendered_body:
                    rendered_body[name] = (
                        [*rendered_body[name], rendered]
                        if isinstance(rendered_body[name], list)
                        else [rendered_body[name], rendered]
                    )
                else:
                    rendered_body[name] = rendered
                request_fields.append((name, variable))
                if is_input:
                    required_inputs.add(variable)
                if human:
                    human_inputs.add(variable)
            body_key = (
                "json"
                if (request.get("post_data") or {}).get("format") == "json"
                else "form"
            )
            step[body_key] = rendered_body
        elif request.get("post_data"):
            unresolved.add(f"{step['name']}: opaque request body")

        headers = request.get("headers") or {}
        response_headers = (request.get("response") or {}).get("headers") or {}
        uses_session = any(str(name).lower() == "cookie" for name in headers) or any(
            str(name).lower() == "set-cookie" for name in response_headers
        )
        session_cookies = session_cookies or uses_session
        recipe_headers = {
            str(name): value
            for name, value in headers.items()
            if str(name).lower() in SAFE_REQUEST_HEADERS and value != "[redacted]"
        }
        secret_header_fields: list[tuple[str, str]] = []
        for header_name, header_value in headers.items():
            if header_value != "[redacted]" or not SENSITIVE_NAME.search(
                str(header_name)
            ):
                continue
            variable = variable_for(_canonical_secret_name(str(header_name)) or "token")
            recipe_headers[str(header_name)] = f"{{{variable}}}"
            secret_header_fields.append((str(header_name), variable))
            required_inputs.add(variable)
        if recipe_headers:
            step["headers"] = recipe_headers
        if classification == "mutation":
            step["mutating"] = True
            if not graphql_operation and re.search(
                r"get|fetch|find|query|search|status|captcha",
                str(step["name"]),
                re.I,
            ):
                unresolved.add(
                    f"{step['name']}: non-GET request defaults to mutating; verify whether confirmation is required"
                )

        for field_name, variable in request_fields + secret_header_fields:
            if not SENSITIVE_NAME.search(field_name) or HUMAN_FIELD.search(field_name):
                continue
            source_index = None
            extraction = None
            for prior_index in range(len(steps) - 1, -1, -1):
                extraction = _response_extraction(candidates[prior_index], field_name)
                if extraction:
                    source_index = prior_index
                    break
            if extraction is not None and source_index is not None:
                steps[source_index].setdefault("extract", {})[variable] = extraction
                if not any(
                    item.endswith(f"source for {field_name}") for item in unresolved
                ):
                    required_inputs.discard(variable)
                dependencies.append(
                    {
                        "field": field_name,
                        "produced_by": steps[source_index]["name"],
                        "consumed_by": step["name"],
                        "source": extraction["source"],
                    }
                )
            else:
                unresolved.add(f"{step['name']}: source for {field_name}")

        response = request.get("response") or {}
        endpoints.append(
            {
                "name": step["name"],
                "method": method,
                "host": parsed.hostname,
                "path": parsed.path or "/",
                "query_fields": [
                    name
                    for name, _value in parse_qsl(parsed.query, keep_blank_values=True)
                ],
                "request_fields": [name for name, _variable in request_fields],
                "status": response.get("status"),
                "mime_type": response.get("mime_type"),
                "resource_type": request.get("resource_type"),
                "session_cookies": uses_session,
                "classification": classification,
                "graphql_operation": graphql_operation,
            }
        )
        steps.append(step)

    return {
        "endpoint_count": len(endpoints),
        "endpoints": endpoints,
        "primary_candidates": endpoints,
        "telemetry_omitted": telemetry_omitted,
        "recipe_draft": {
            "steps": steps,
            "required_inputs": sorted(required_inputs),
            "human_inputs": sorted(human_inputs),
            "dependencies": dependencies,
            "session_cookies": session_cookies,
        },
        "review_required": sorted(unresolved)
        + [
            "Draft only: verify endpoint semantics, mutation risk, pagination, and output normalization before publishing an adapter."
        ],
    }


def build_flow_capsule(
    trace: dict[str, Any],
    *,
    operation: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Turn a sanitized capture into a portable, reviewable replay artifact.

    Capsules contain request structure and response shape, never captured body
    values.  They deliberately start as drafts: a live replay must pass before
    a registry publisher may treat them as evidence.
    """
    if trace.get("kind") not in {
        "agentweb_redacted_network_trace",
        "sitepack_redacted_network_trace",
    }:
        raise AgentWebError("Flow capsules require an AgentWeb redacted network trace")
    compiler = analyze_network_trace(trace)
    recipe = compiler["recipe_draft"]
    requests = [
        item
        for item in (trace.get("requests") or [])
        if isinstance(item, dict)
        and _is_replay_candidate(item)
        and _request_classification(item) != "telemetry"
    ]
    if base_url:
        configured = urlparse(base_url)
        configured_origin = (configured.scheme.lower(), configured.netloc.lower())
        for step, request in zip(recipe.get("steps") or [], requests, strict=False):
            parsed = urlparse(str(request.get("url") or ""))
            request_origin = (parsed.scheme.lower(), parsed.netloc.lower())
            if request_origin != configured_origin:
                step["path"] = urlunparse(
                    (parsed.scheme, parsed.netloc, parsed.path or "/", "", "", "")
                )
    observed_steps = []
    for index, request in enumerate(requests):
        response = request.get("response") or {}
        observed_steps.append(
            {
                "name": (
                    (compiler.get("endpoints") or [{}])[index].get("name")
                    if index < len(compiler.get("endpoints") or [])
                    else f"step_{index + 1}"
                ),
                "status": response.get("status"),
                "mime_type": response.get("mime_type"),
            }
        )
    final_shape = None
    if requests:
        final_shape = requests[-1].get("response_body")
    review = list(compiler.get("review_required") or [])
    return {
        "kind": "agentweb_flow_capsule",
        "schema_version": 1,
        "site": trace.get("site"),
        "operation": operation or "captured_workflow",
        "captured_at_unix": trace.get("captured_at_unix"),
        "recipe": recipe,
        "observed": {
            "steps": observed_steps,
            "final_response_shape": final_shape,
            "page_transition": {
                "before": (trace.get("page_before") or {}).get("url"),
                "after": (trace.get("page_after") or {}).get("url"),
            },
        },
        "risk": {
            "mutating": any(bool(step.get("mutating")) for step in recipe["steps"]),
            "human_inputs": recipe.get("human_inputs") or [],
            "uses_session_cookies": bool(recipe.get("session_cookies")),
        },
        "verification": {
            "status": "draft",
            "review_required": review,
        },
    }


def _payload_shape(value: Any, path: str = "") -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    if isinstance(value, dict):
        for name, item in value.items():
            fields.extend(_payload_shape(item, f"{path}.{name}" if path else str(name)))
    elif isinstance(value, list):
        if value:
            fields.extend(_payload_shape(value[0], f"{path}.0" if path else "0"))
    else:
        fields.append({"path": path, "type": type(value).__name__})
    return fields[:500]


def verify_flow_capsule(
    capsule: dict[str, Any], result: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Validate a capsule and, when supplied, compare a direct replay result."""
    errors: list[str] = []
    if (
        capsule.get("kind")
        not in {
            "agentweb_flow_capsule",
            "sitepack_flow_capsule",
        }
        or capsule.get("schema_version") != 1
    ):
        errors.append("unsupported flow capsule")
    site = capsule.get("site")
    recipe = capsule.get("recipe") or {}
    steps = recipe.get("steps") or []
    if not isinstance(site, str) or not site:
        errors.append("capsule is missing site")
    if not isinstance(steps, list) or not steps:
        errors.append("capsule has no replay steps")
    encoded = json.dumps(capsule)
    if re.search(r'(?i)(authorization|cookie)"\s*:\s*"(?!\[redacted\]|\{)', encoded):
        errors.append("capsule may contain a captured credential")
    required = set(recipe.get("required_inputs") or [])
    human = set(recipe.get("human_inputs") or [])
    if not human.issubset(required):
        errors.append("human inputs must also be declared as required inputs")
    if result is None:
        return {
            "passed": not errors,
            "structural_only": True,
            "errors": errors,
            "site": site,
            "operation": capsule.get("operation"),
        }

    expected_steps = (capsule.get("observed") or {}).get("steps") or []
    actual_steps = result.get("steps") or []
    mismatches: list[str] = []
    if len(expected_steps) != len(actual_steps):
        mismatches.append(
            f"step count changed: captured {len(expected_steps)}, replayed {len(actual_steps)}"
        )
    for index, (expected, actual) in enumerate(zip(expected_steps, actual_steps), 1):
        if expected.get("status") is not None and expected.get("status") != actual.get(
            "status"
        ):
            mismatches.append(
                f"step {index} status changed: captured {expected.get('status')}, replayed {actual.get('status')}"
            )
    expected_shape = (capsule.get("observed") or {}).get("final_response_shape") or {}
    if expected_shape.get("format") == "json":
        expected_fields = {
            (field.get("path"), field.get("type"))
            for field in expected_shape.get("fields") or []
            if not field.get("sensitive")
        }
        actual_fields = {
            (field["path"], field["type"])
            for field in _payload_shape(result.get("data"))
        }
        # Output contracts validate concrete types after replay. Capsule drift
        # detection should only establish that captured fields still exist;
        # treating a nullable field as a missing string made valid older GST
        # results fail before their reviewed default mapping could run.
        actual_paths = {path for path, _type in actual_fields}

        def empty_collection_at_item_path(path: str) -> bool:
            parts = path.split(".")
            if "0" not in parts:
                return False
            value: Any = result.get("data")
            for part in parts[: parts.index("0")]:
                if not isinstance(value, dict) or part not in value:
                    return False
                value = value[part]
            return isinstance(value, list) and not value

        missing = sorted(
            (path, field_type)
            for path, field_type in expected_fields
            if path not in actual_paths and not empty_collection_at_item_path(str(path))
        )
        if missing:
            mismatches.append(f"response shape lost {len(missing)} captured field(s)")
    return {
        "passed": not errors and not mismatches,
        "structural_only": False,
        "errors": errors,
        "mismatches": mismatches,
        "site": site,
        "operation": capsule.get("operation"),
        "steps": actual_steps,
    }


def write_trace(directory: Path, name: str, trace: dict[str, Any]) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    if not safe_name:
        safe_name = "capture"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{int(time.time() * 1000)}-{safe_name}.json"
    atomic_write(
        path,
        (json.dumps(trace, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode=0o600,
    )
    return path
