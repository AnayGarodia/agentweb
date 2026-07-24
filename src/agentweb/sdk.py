from __future__ import annotations

import base64
import gzip
import hashlib
import json
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
import warnings
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import dataclass
from http.cookiejar import Cookie, MozillaCookieJar
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
from curl_cffi import requests as curl_requests

from .storage import (
    Cache,
    StatePaths,
    atomic_write,
    contained_path,
    exclusive_path_lock,
    read_json,
    safe_component,
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)


def parse_xml_feed(body: bytes) -> dict[str, Any]:
    """Return a small, namespace-free representation of an Atom or RSS feed."""
    if len(body) > 10 * 1024 * 1024:
        raise AgentWebError(
            "The website returned an XML feed larger than AgentWeb's 10 MB parsing limit",
            code="feed_too_large",
            retryable=False,
        )
    declarations = body.upper()
    if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
        raise AgentWebError(
            "AgentWeb refused an XML feed containing a document type or entity declaration",
            code="unsafe_xml_feed",
            retryable=False,
        )
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise AgentWebError(
            "The website returned a malformed XML feed",
            code="invalid_xml_feed",
            retryable=True,
        ) from exc

    def local(node: ET.Element) -> str:
        return str(node.tag).rsplit("}", 1)[-1]

    def children(node: ET.Element, name: str) -> list[ET.Element]:
        return [child for child in node if local(child) == name]

    def first_text(node: ET.Element, *names: str) -> str | None:
        for name in names:
            match = next(iter(children(node, name)), None)
            if match is not None:
                value = " ".join("".join(match.itertext()).split())
                if value:
                    return value
        return None

    def feed_link(node: ET.Element) -> str | None:
        for link in children(node, "link"):
            href = str(link.get("href") or "").strip()
            text = " ".join("".join(link.itertext()).split())
            if href or text:
                return href or text
        return None

    root_name = local(root)
    channel = (
        next(iter(children(root, "channel")), root)
        if root_name == "rss"
        else root
    )
    entry_nodes = children(channel, "item" if root_name == "rss" else "entry")
    entries: list[dict[str, Any]] = []
    for node in entry_nodes:
        links = []
        for link in children(node, "link"):
            href = str(link.get("href") or "").strip()
            if not href:
                href = " ".join("".join(link.itertext()).split())
            if href:
                links.append(
                    {
                        key: value
                        for key, value in {
                            "url": href,
                            "rel": link.get("rel"),
                            "type": link.get("type"),
                            "title": link.get("title"),
                        }.items()
                        if value
                    }
                )
        authors = [
            first_text(author, "name") or " ".join("".join(author.itertext()).split())
            for author in children(node, "author")
        ]
        authors = [author for author in authors if author]
        if not authors:
            authors = [
                value
                for value in (first_text(node, "creator", "author"),)
                if value
            ]
        categories = [
            str(category.get("term") or " ".join(category.itertext())).strip()
            for category in children(node, "category")
        ]
        entry = {
            "id": first_text(node, "id", "guid"),
            "title": first_text(node, "title"),
            "summary": first_text(node, "summary", "description", "content"),
            "published": first_text(node, "published", "pubDate"),
            "updated": first_text(node, "updated"),
            "authors": authors,
            "categories": [item for item in categories if item],
            "primary_category": next(
                (
                    str(item.get("term") or "").strip()
                    for item in children(node, "primary_category")
                    if item.get("term")
                ),
                None,
            ),
            "comment": first_text(node, "comment"),
            "journal_reference": first_text(node, "journal_ref"),
            "doi": first_text(node, "doi"),
            "links": links,
        }
        entries.append(
            {
                key: value
                for key, value in entry.items()
                if value not in (None, [], "")
            }
        )

    metadata: dict[str, Any] = {
        "id": first_text(channel, "id"),
        "title": first_text(channel, "title"),
        "description": first_text(channel, "subtitle", "description"),
        "updated": first_text(channel, "updated", "lastBuildDate"),
        "url": feed_link(channel),
        "total_results": first_text(channel, "totalResults"),
        "start_index": first_text(channel, "startIndex"),
        "items_per_page": first_text(channel, "itemsPerPage"),
        "entries": entries,
    }
    for key in ("total_results", "start_index", "items_per_page"):
        value = metadata.get(key)
        if isinstance(value, str) and value.isdigit():
            metadata[key] = int(value)
    return {
        key: value for key, value in metadata.items() if value not in (None, "")
    }


class AgentWebError(RuntimeError):
    code = "agentweb_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        next_action: str | None = None,
        user_action: str | None = None,
        field: str | None = None,
        retry_after_seconds: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or self.code
        self.retryable = self.retryable if retryable is None else retryable
        self.next_action = next_action
        self.user_action = user_action
        self.field = field
        self.retry_after_seconds = retry_after_seconds
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "error": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }
        if self.next_action:
            value["next_operation"] = self.next_action
            value["next_action"] = self.next_action
        if self.user_action:
            value["user_action"] = self.user_action
        if self.field:
            value["field"] = self.field
        if self.retry_after_seconds is not None:
            value["retry_after_seconds"] = self.retry_after_seconds
        if self.details:
            value["details"] = self.details
        return value


def _binding_path(value: Any, path: str) -> Any:
    """Resolve the deliberately small JSON-path subset used by reviewed bindings."""
    current = value
    if path in {"", "$"}:
        return current
    if not isinstance(path, str) or not path.startswith("$."):
        raise AgentWebError(
            f"Binding path {path!r} must start with '$.'",
            code="operation_binding_invalid",
        )
    for part in path[2:].split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise AgentWebError(
                "The website returned an unexpected response shape, so AgentWeb "
                "could not safely normalize this operation. Retry once with a fresh request.",
                code="operation_output_mapping_failed",
                retryable=True,
                details={"binding_path": path},
            )
    return current


def map_operation_inputs(
    arguments: dict[str, Any], specification: dict[str, Any] | None
) -> dict[str, Any]:
    """Map reviewed agent-facing arguments to a captured recipe's variables."""
    mapped = dict(arguments)
    for target, rule in (specification or {}).items():
        if isinstance(rule, str):
            source = rule
            if source not in arguments:
                raise AgentWebError(
                    f"Missing operation input {source!r}",
                    code="missing_input",
                    field=source,
                )
            value = arguments[source]
        elif isinstance(rule, dict) and "constant" in rule:
            value = rule["constant"]
        elif isinstance(rule, dict) and isinstance(rule.get("source"), str):
            source = str(rule["source"])
            if source not in arguments:
                if "default" in rule:
                    value = rule["default"]
                else:
                    raise AgentWebError(
                        f"Missing operation input {source!r}",
                        code="missing_input",
                        field=source,
                    )
            else:
                value = arguments[source]
            translations = rule.get("values")
            if translations is not None:
                key = str(value)
                if not isinstance(translations, dict) or key not in translations:
                    raise AgentWebError(
                        f"Input {source!r} has no reviewed website mapping for {value!r}",
                        code="invalid_input",
                        field=source,
                    )
                value = translations[key]
        else:
            raise AgentWebError(
                f"Input binding for {target!r} is invalid",
                code="operation_binding_invalid",
                field=f"binding.variables.{target}",
            )
        mapped[str(target)] = value
    return mapped


def normalize_operation_output(data: Any, specification: dict[str, Any] | None) -> Any:
    """Turn terse site response fields into a reviewed, agent-facing result shape."""
    if not specification:
        return data
    empty_when = specification.get("empty_when")
    if isinstance(empty_when, dict):
        try:
            observed_empty_value = _binding_path(
                data, str(empty_when.get("path") or "")
            )
        except AgentWebError:
            observed_empty_value = object()
        if observed_empty_value == empty_when.get("equals"):
            data = empty_when.get("value", [])
    selected = _binding_path(data, str(specification.get("source") or "$"))
    source_transform = specification.get("source_transform")
    if source_transform == "json_decode":
        if not isinstance(selected, str):
            raise AgentWebError(
                "Reviewed output mapping expected JSON encoded as text",
                code="operation_output_mapping_failed",
                retryable=True,
            )
        try:
            selected = json.loads(selected)
        except json.JSONDecodeError as exc:
            raise AgentWebError(
                "Website returned invalid JSON inside its response",
                code="operation_output_mapping_failed",
                retryable=True,
            ) from exc
    elif source_transform is not None:
        raise AgentWebError(
            f"Unsupported reviewed source transform {source_transform!r}",
            code="operation_binding_invalid",
            field="binding.output.source_transform",
        )

    def mapped_field(value: Any, rule: Any) -> Any:
        if isinstance(rule, str):
            return _binding_path(value, rule)
        if not isinstance(rule, dict) or not isinstance(rule.get("path"), str):
            raise AgentWebError(
                "Reviewed output field mapping is invalid",
                code="operation_binding_invalid",
                field="binding.output",
            )
        try:
            selected_value = _binding_path(value, rule["path"])
        except AgentWebError as exc:
            if exc.code == "operation_output_mapping_failed" and "default" in rule:
                return deepcopy(rule["default"])
            raise
        if selected_value is None and "default" in rule:
            return deepcopy(rule["default"])
        nested_fields = rule.get("fields")
        if isinstance(nested_fields, dict):
            if not isinstance(selected_value, dict):
                raise AgentWebError(
                    "Reviewed output mapping expected a nested object",
                    code="operation_output_mapping_failed",
                    retryable=True,
                )
            return {
                str(name): mapped_field(selected_value, nested_rule)
                for name, nested_rule in nested_fields.items()
            }
        item_fields = rule.get("item_fields")
        if isinstance(item_fields, dict):
            if not isinstance(selected_value, list):
                raise AgentWebError(
                    "Reviewed output mapping expected a nested list",
                    code="operation_output_mapping_failed",
                    retryable=True,
                )
            return [
                {
                    str(name): mapped_field(item, nested_rule)
                    for name, nested_rule in item_fields.items()
                }
                for item in selected_value
            ]
        transform = rule.get("transform")
        if transform == "html_to_text":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
                text = BeautifulSoup(
                    str(selected_value or ""), "html.parser"
                ).get_text(" ", strip=True)
            return " ".join(text.split())
        if transform == "yn_boolean":
            normalized = str(selected_value or "").strip().upper()
            if normalized not in {"Y", "N"}:
                raise AgentWebError(
                    "Website returned an unexpected yes/no value",
                    code="operation_output_mapping_failed",
                    retryable=True,
                )
            return normalized == "Y"
        if transform == "url_filename":
            return Path(urlparse(str(selected_value or "")).path).name
        if transform is not None:
            raise AgentWebError(
                f"Unsupported reviewed output transform {transform!r}",
                code="operation_binding_invalid",
                field="binding.output",
            )
        return selected_value

    fields = specification.get("fields")
    if isinstance(fields, dict):
        return {
            str(name): mapped_field(selected, rule) for name, rule in fields.items()
        }
    collection = specification.get("collection")
    item_fields = specification.get("item_fields")
    if isinstance(collection, str) and isinstance(item_fields, dict):
        if not isinstance(selected, list):
            raise AgentWebError(
                "Reviewed output mapping expected the website to return a list",
                code="operation_output_mapping_failed",
                retryable=True,
            )
        return {
            collection: [
                {
                    str(name): mapped_field(item, rule)
                    for name, rule in item_fields.items()
                }
                for item in selected
            ]
        }
    raise AgentWebError(
        "Reviewed output binding must define fields or collection plus item_fields",
        code="operation_binding_invalid",
        field="binding.output",
    )


def match_operation_response_error(
    data: Any, specifications: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    """Match a reviewed website error carried inside an otherwise successful response."""
    for specification in specifications or []:
        try:
            observed = _binding_path(data, str(specification.get("path") or ""))
        except AgentWebError:
            continue
        if observed != specification.get("equals"):
            continue
        details: dict[str, Any] = {}
        for detail_name, detail_path in (specification.get("details") or {}).items():
            try:
                details[str(detail_name)] = _binding_path(data, str(detail_path))
            except AgentWebError:
                continue
        return {
            "code": str(specification.get("code") or "website_response_error"),
            "details": details,
        }
    return None


def paginate_operation_output(
    data: Any,
    specification: dict[str, Any] | None,
    arguments: dict[str, Any],
) -> tuple[Any, dict[str, Any], list[str]]:
    """Apply reviewed local pagination after normalizing a website collection."""
    page = (specification or {}).get("pagination")
    collection = (specification or {}).get("collection")
    if not isinstance(page, dict) or not isinstance(collection, str):
        return data, {"supported": False}, []
    if not isinstance(data, dict) or not isinstance(data.get(collection), list):
        raise AgentWebError(
            "Reviewed output pagination did not find its collection",
            code="operation_output_mapping_failed",
            retryable=True,
        )

    limit_input = str(page.get("limit_input") or "limit")
    cursor_input = str(page.get("cursor_input") or "cursor")
    default_limit = int(page.get("default_limit") or 50)
    max_limit = int(page.get("max_limit") or 100)
    requested_limit = arguments.get(limit_input, default_limit)
    if not isinstance(requested_limit, int) or isinstance(requested_limit, bool):
        raise AgentWebError(
            f"Input {limit_input!r} must be an integer",
            code="invalid_input",
            field=limit_input,
        )
    limit = min(requested_limit, max_limit)
    if limit < 1:
        raise AgentWebError(
            f"Input {limit_input!r} must be at least 1",
            code="invalid_input",
            field=limit_input,
        )

    offset = 0
    cursor = arguments.get(cursor_input)
    if cursor not in {None, ""}:
        try:
            padded = str(cursor) + "=" * (-len(str(cursor)) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode("ascii")
            prefix, raw_offset = decoded.split(":", 1)
            if prefix != "offset":
                raise ValueError
            offset = int(raw_offset)
            if offset < 0:
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            raise AgentWebError(
                "The pagination cursor is invalid or no longer usable",
                code="invalid_cursor",
                field=cursor_input,
                user_action="Start again without a cursor.",
            ) from None

    items = data[collection]
    selected = items[offset : offset + limit]
    next_offset = offset + len(selected)
    has_more = next_offset < len(items)
    next_cursor = None
    if has_more:
        next_cursor = (
            base64.urlsafe_b64encode(f"offset:{next_offset}".encode("ascii"))
            .decode("ascii")
            .rstrip("=")
        )
    paged = dict(data)
    paged[collection] = selected
    pagination = {
        "supported": True,
        "cursor": str(cursor) if cursor not in {None, ""} else None,
        "next_cursor": next_cursor,
        "limit": limit,
        "returned": len(selected),
        "total": len(items),
        "has_more": has_more,
    }
    warnings = (
        [
            f"Returned {len(selected)} of {len(items)} {collection}; "
            "call the same operation with next_cursor to continue."
        ]
        if has_more
        else []
    )
    return paged, pagination, warnings


# Compatibility for adapters compiled before the AgentWeb rename. New code
# should import AgentWebError; old adapter bundles continue to load unchanged.
SitepackError = AgentWebError


class AuthenticationRequired(AgentWebError):
    code = "authentication_required"


class ConfigurationRequired(AgentWebError):
    def __init__(self, message: str, *, operation: str) -> None:
        super().__init__(message, next_action=operation)
        self.operation = operation


class UpstreamError(AgentWebError):
    code = "upstream_error"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        details = {}
        if status is not None:
            details["status"] = status
        if retry_after_seconds is not None:
            details["retry_after_seconds"] = retry_after_seconds
        super().__init__(message, retryable=retryable, details=details)


class UnsupportedOperation(AgentWebError):
    code = "unsupported_operation"


class OperationCancelled(AgentWebError):
    code = "cancelled"


class CancellationToken:
    def __init__(self, event: threading.Event | None = None) -> None:
        self.event = event or threading.Event()

    def cancel(self) -> None:
        self.event.set()

    def check(self) -> None:
        if self.event.is_set():
            raise OperationCancelled("The AgentWeb operation was cancelled")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _redirect_url(
    current_url: str,
    location: str,
    *,
    allowed_domains: tuple[str, ...],
) -> str:
    target = urljoin(current_url, location)
    current = urlparse(current_url)
    parsed = urlparse(target)
    host = (parsed.hostname or "").lower()
    if not host or parsed.scheme not in {"http", "https"}:
        raise AgentWebError("Redirect target must be an HTTP(S) URL")
    if current.scheme == "https" and parsed.scheme != "https":
        raise AgentWebError("AgentWeb refused an HTTPS redirect downgrade")
    if not any(
        host == domain or host.endswith("." + domain) for domain in allowed_domains
    ):
        raise AgentWebError(
            f"AgentWeb refused a redirect outside the allowed domains: {host}"
        )
    return target


def parse_query_pairs(
    values: list[str] | dict[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    entries: list[tuple[str, Any]] = []
    if isinstance(values, dict):
        entries = list(values.items())
    else:
        for value in values or []:
            if "=" not in value:
                raise AgentWebError(f"Query parameter {value!r} must use key=value")
            key_part, value_part = value.split("=", 1)
            entries.append((key_part, value_part))
    for key, item in entries:
        key = key.strip()
        # Rails-style APIs nest query names in brackets, e.g. conditions[term].
        stripped = key.replace("_", "").replace("-", "").replace("[", "").replace("]", "")
        if not key or not stripped.isalnum():
            raise AgentWebError(f"Query parameter name {key!r} is invalid")
        existing = result.get(key)
        if existing is None:
            result[key] = item
        elif isinstance(existing, list):
            existing.append(item)
        else:
            result[key] = [existing, item]
    return result


def bounded_data(
    value: Any, *, max_items: int = 50, max_string: int = 4000, depth: int = 0
) -> tuple[Any, bool]:
    if depth > 8:
        return "[maximum nesting reached]", True
    if isinstance(value, str):
        if len(value) <= max_string:
            return value, False
        return value[: max(max_string - 1, 0)].rstrip() + "…", True
    if isinstance(value, list):
        output = []
        truncated = len(value) > max_items
        for item in value[:max_items]:
            mapped, changed = bounded_data(
                item, max_items=max_items, max_string=max_string, depth=depth + 1
            )
            output.append(mapped)
            truncated = truncated or changed
        return output, truncated
    if isinstance(value, dict):
        output_map: dict[str, Any] = {}
        entries = list(value.items())
        truncated = len(entries) > max_items
        for key, item in entries[:max_items]:
            mapped, changed = bounded_data(
                item, max_items=max_items, max_string=max_string, depth=depth + 1
            )
            output_map[str(key)] = mapped
            truncated = truncated or changed
        return output_map, truncated
    return value, False


def enforce_data_budget(
    value: Any, *, max_total_chars: int = 20000
) -> tuple[Any, bool, int]:
    """Bound the complete JSON response, not merely each recursive branch."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    original_chars = len(encoded)
    if original_chars <= max_total_chars:
        return value, False, original_chars
    reserve = 220
    preview_limit = max(max_total_chars - reserve, 0)
    preview = encoded[:preview_limit]
    while preview and ord(preview[-1]) in range(0xD800, 0xE000):
        preview = preview[:-1]
    return (
        {
            "format": "truncated_json_preview",
            "preview": preview,
            "original_chars": original_chars,
            "returned_chars_limit": max_total_chars,
            "note": "The complete nested response exceeded max_total_chars.",
        },
        True,
        original_chars,
    )


def redact_sensitive_values(value: Any, sensitive_values: list[Any]) -> Any:
    """Remove extracted credentials from a payload before it leaves AgentWeb."""
    if isinstance(value, dict):
        return {
            str(key): redact_sensitive_values(item, sensitive_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_values(item, sensitive_values) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive_values(item, sensitive_values) for item in value]
    for secret in sensitive_values:
        if (
            secret is not None
            and secret != ""
            and type(value) is type(secret)
            and value == secret
        ):
            return "[redacted]"
    return value


@dataclass
class Response:
    status: int
    url: str
    headers: dict[str, str]
    body: bytes
    elapsed_ms: float
    from_cache: bool = False
    transport: str = "urllib"

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class HttpSession:
    def __init__(
        self,
        paths: StatePaths,
        site: str,
        profile: str,
        *,
        fresh: bool = False,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self.paths = paths
        self.site = site
        self.profile = profile
        self.fresh = fresh
        self.cancellation = cancellation or CancellationToken()
        self.cookie_path = paths.cookie_file(site, profile)
        self.cookies = MozillaCookieJar(str(self.cookie_path))
        if self.cookie_path.exists():
            try:
                self.cookies.load(ignore_discard=True, ignore_expires=True)
            except Exception as exc:
                raise AgentWebError(f"Could not read cookie jar: {exc}") from exc
        self._loaded_cookie_keys = {
            (cookie.domain, cookie.path, cookie.name) for cookie in self.cookies
        }
        self.opener = build_opener(HTTPCookieProcessor(self.cookies), _NoRedirect())
        self.cache = Cache(paths.cache_db)
        self.browser_identity = (
            read_json(paths.profile_dir(site, profile) / "browser_identity.json", {})
            or {}
        )

    def save_cookies(self) -> None:
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.cookie_path.with_name(f".{self.cookie_path.name}.lock")
        try:
            with exclusive_path_lock(lock_path):
                merged = MozillaCookieJar()
                if self.cookie_path.exists():
                    try:
                        merged.load(
                            str(self.cookie_path),
                            ignore_discard=True,
                            ignore_expires=True,
                        )
                    except (OSError, ValueError):
                        # The current in-memory jar was loaded successfully. If a
                        # stale external file is malformed, replace it atomically.
                        merged = MozillaCookieJar()
                current_keys = {
                    (cookie.domain, cookie.path, cookie.name) for cookie in self.cookies
                }
                for domain, path, name in self._loaded_cookie_keys - current_keys:
                    try:
                        merged.clear(domain, path, name)
                    except KeyError:
                        pass
                for cookie in self.cookies:
                    merged.set_cookie(cookie)
                merged.clear_expired_cookies()
                descriptor, temporary = tempfile.mkstemp(
                    prefix=f".{self.cookie_path.name}.",
                    dir=self.cookie_path.parent,
                )
                os.close(descriptor)
                try:
                    merged.save(
                        temporary,
                        ignore_discard=True,
                        ignore_expires=True,
                    )
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, self.cookie_path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                self.cookies = merged
                self._loaded_cookie_keys = {
                    (cookie.domain, cookie.path, cookie.name) for cookie in self.cookies
                }
                self.cookies.filename = str(self.cookie_path)
                self.opener = build_opener(
                    HTTPCookieProcessor(self.cookies), _NoRedirect()
                )
        except TimeoutError as exc:
            raise AgentWebError(
                "Timed out while another AgentWeb process was updating this profile's cookies",
                code="cookie_store_busy",
                retryable=True,
            ) from exc

    def _has_request_credentials(
        self, url: str, supplied_headers: dict[str, str] | None
    ) -> bool:
        if any(
            name.lower() in {"authorization", "proxy-authorization", "cookie"}
            for name in (supplied_headers or {})
        ):
            return True
        probe = Request(url)
        self.cookies.add_cookie_header(probe)
        return probe.get_header("Cookie") is not None

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        form: dict[str, Any] | list[tuple[str, Any]] | None = None,
        json_body: Any | None = None,
        body: bytes | str | None = None,
        files: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
        referer: str | None = None,
        cache_action: str | None = None,
        cache_arguments: dict[str, Any] | None = None,
        cache_ttl: int = 0,
        impersonate: str | None = None,
        allowed_redirect_domains: tuple[str, ...] | None = None,
        timeout_seconds: float = 30,
    ) -> Response:
        self.cancellation.check()
        if not 0.25 <= timeout_seconds <= 60:
            raise AgentWebError("timeout_seconds must be between 0.25 and 60")
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        supplied_bodies = sum(value is not None for value in (json_body, body, files))
        if supplied_bodies > 1 or (
            files is None and form is not None and supplied_bodies
        ):
            raise AgentWebError(
                "form, json_body, body, and files are mutually exclusive except form fields with files"
            )
        data = None
        content_type = None
        if files is not None:
            boundary = "agentweb-" + secrets.token_hex(16)
            chunks: list[bytes] = []

            def add_line(value: str) -> None:
                chunks.append(value.encode("utf-8"))

            form_items = form.items() if isinstance(form, dict) else (form or [])
            for name, value in form_items:
                values = value if isinstance(value, list) else [value]
                for item in values:
                    add_line(f"--{boundary}\r\n")
                    safe_name = str(name).replace('"', "")
                    add_line(
                        f'Content-Disposition: form-data; name="{safe_name}"\r\n\r\n'
                    )
                    add_line(str(item) + "\r\n")
            for specification in files:
                source = Path(str(specification.get("path") or "")).expanduser()
                if not source.is_file():
                    raise AgentWebError(f"Upload file does not exist: {source}")
                if source.stat().st_size > 100 * 1024 * 1024:
                    raise AgentWebError(
                        f"Upload file exceeds AgentWeb's 100 MB direct-upload limit: {source}"
                    )
                field = str(specification.get("field") or "file").replace('"', "")
                filename = str(specification.get("filename") or source.name).replace(
                    '"', ""
                )
                content_type = str(
                    specification.get("content_type")
                    or mimetypes.guess_type(filename)[0]
                    or "application/octet-stream"
                )
                add_line(f"--{boundary}\r\n")
                add_line(
                    f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                )
                add_line(f"Content-Type: {content_type}\r\n\r\n")
                chunks.append(source.read_bytes())
                add_line("\r\n")
            add_line(f"--{boundary}--\r\n")
            data = b"".join(chunks)
            content_type = f"multipart/form-data; boundary={boundary}"
        elif form is not None:
            data = urlencode(form, doseq=True).encode("utf-8")
            content_type = "application/x-www-form-urlencoded"
        elif json_body is not None:
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            content_type = "application/json"
        elif body is not None:
            data = body.encode("utf-8") if isinstance(body, str) else body
            content_type = "application/octet-stream"
        cache_key = None
        if (
            method.upper() == "GET"
            and cache_action
            and cache_ttl > 0
            and not self._has_request_credentials(url, headers)
        ):
            cache_key = self.cache.key(
                self.site,
                cache_action,
                cache_arguments or {},
                profile=self.profile,
            )
            if not self.fresh:
                cached = self.cache.get(cache_key)
                if cached is not None:
                    envelope = json.loads(cached)
                    return Response(
                        status=envelope["status"],
                        url=envelope["url"],
                        headers=envelope["headers"],
                        body=envelope["body"].encode("latin1"),
                        elapsed_ms=0.0,
                        from_cache=True,
                        transport=envelope.get("transport", "urllib"),
                    )
        response = self._perform_request(
            method=method,
            url=url,
            data=data,
            content_type=content_type,
            headers=headers,
            referer=referer,
            impersonate=impersonate,
            allowed_redirect_domains=allowed_redirect_domains,
            timeout_seconds=timeout_seconds,
        )
        self._cache_store(cache_key, cache_ttl, response)
        return response

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
        request_headers = {
            "User-Agent": str(self.browser_identity.get("user_agent") or USER_AGENT),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache" if self.fresh else "max-age=0",
        }
        if referer:
            request_headers["Referer"] = referer
        if data is not None:
            request_headers["Content-Type"] = content_type or "application/octet-stream"
            request_headers["Origin"] = (
                f"{url.split('/', 3)[0]}//{url.split('/', 3)[2]}"
            )
        request_headers.update(headers or {})
        started = time.perf_counter()
        transport = "urllib"
        initial = urlparse(url)
        redirect_domains = tuple(
            domain.lstrip(".").lower()
            for domain in (
                allowed_redirect_domains or ((initial.hostname or "").lower(),)
            )
            if domain
        )
        if not redirect_domains:
            raise AgentWebError("Request URL has no redirect allowlist")
        current_url = url
        current_method = method.upper()
        current_data = data
        current_headers = dict(request_headers)
        if impersonate:
            transport = f"curl_cffi:{impersonate}"
            browser: Any = curl_requests.Session()
            for cookie in self.cookies:
                browser.cookies.set(
                    cookie.name,
                    cookie.value or "",
                    domain=cookie.domain,
                    path=cookie.path or "/",
                )
            for redirect_count in range(11):
                self.cancellation.check()
                try:
                    raw = browser.request(
                        current_method,  # type: ignore[arg-type]
                        current_url,
                        data=current_data,
                        headers=current_headers,
                        timeout=timeout_seconds,
                        allow_redirects=False,
                        impersonate=impersonate,  # type: ignore[arg-type]
                    )
                except Exception as exc:
                    host = urlparse(current_url).hostname or self.site
                    raise AgentWebError(
                        f"Browser-compatible connection to {host} failed: {exc}. The saved session was preserved; retry the operation."
                    ) from exc
                if raw.status_code not in {301, 302, 303, 307, 308}:
                    break
                if redirect_count == 10:
                    raise AgentWebError(
                        "AgentWeb stopped a redirect loop after 10 hops"
                    )
                location = raw.headers.get("Location")
                if not location:
                    break
                next_url = _redirect_url(
                    current_url, location, allowed_domains=redirect_domains
                )
                if urlparse(next_url).netloc != urlparse(current_url).netloc:
                    current_headers = {
                        name: value
                        for name, value in current_headers.items()
                        if name.lower()
                        not in {
                            "authorization",
                            "proxy-authorization",
                            "origin",
                            "referer",
                        }
                    }
                if raw.status_code == 303 or (
                    raw.status_code in {301, 302} and current_method == "POST"
                ):
                    current_method = "GET"
                    current_data = None
                    current_headers.pop("Content-Type", None)
                current_url = next_url
            for cookie in browser.cookies.jar:
                self.cookies.set_cookie(cookie)
            response_body = bytes(raw.content)
            raw_status = int(raw.status_code)
            raw_url = str(raw.url)
            raw_headers = dict(raw.headers.items())
        else:
            for redirect_count in range(11):
                self.cancellation.check()
                request = Request(
                    current_url,
                    data=current_data,
                    headers=current_headers,
                    method=current_method,
                )
                try:
                    raw = self.opener.open(request, timeout=timeout_seconds)
                except HTTPError as exc:
                    raw = exc
                except (URLError, OSError) as exc:
                    host = urlparse(current_url).hostname or self.site
                    reason = getattr(exc, "reason", exc)
                    raise AgentWebError(
                        f"Network connection to {host} failed: {reason}. The saved session was preserved; retry the operation."
                    ) from exc
                if raw.status not in {301, 302, 303, 307, 308}:
                    break
                if redirect_count == 10:
                    raise AgentWebError(
                        "AgentWeb stopped a redirect loop after 10 hops"
                    )
                location = raw.headers.get("Location")
                if not location:
                    break
                next_url = _redirect_url(
                    current_url, location, allowed_domains=redirect_domains
                )
                if urlparse(next_url).netloc != urlparse(current_url).netloc:
                    current_headers = {
                        name: value
                        for name, value in current_headers.items()
                        if name.lower()
                        not in {
                            "authorization",
                            "proxy-authorization",
                            "origin",
                            "referer",
                        }
                    }
                if raw.status == 303 or (
                    raw.status in {301, 302} and current_method == "POST"
                ):
                    current_method = "GET"
                    current_data = None
                    current_headers.pop("Content-Type", None)
                current_url = next_url
            response_body = raw.read()
            if raw.headers.get("Content-Encoding") == "gzip":
                response_body = gzip.decompress(response_body)
            raw_status = raw.status
            raw_url = raw.url
            raw_headers = dict(raw.headers.items())
        elapsed = (time.perf_counter() - started) * 1000
        response = Response(
            status=raw_status,
            url=raw_url,
            headers=raw_headers,
            body=response_body,
            elapsed_ms=elapsed,
            transport=transport,
        )
        self.save_cookies()
        return response

    def _cache_store(
        self, cache_key: str | None, cache_ttl: int, response: Response
    ) -> None:
        if cache_key and response.status == 200:
            self.cache.put(
                cache_key,
                self.site,
                json.dumps(
                    {
                        "status": response.status,
                        "url": response.url,
                        "headers": response.headers,
                        "body": response.body.decode("latin1"),
                        "transport": response.transport,
                    }
                ).encode(),
                cache_ttl,
            )

    def import_netscape_cookies(self, source: Path) -> int:
        incoming = MozillaCookieJar(str(source))
        incoming.load(ignore_discard=True, ignore_expires=True)
        count = 0
        for cookie in incoming:
            self.cookies.set_cookie(cookie)
            count += 1
        self.save_cookies()
        return count

    def import_cookie_header(self, header: str, domain: str) -> int:
        parsed = SimpleCookie()
        parsed.load(header.strip())
        count = 0
        rest: dict[str, Any] = {"HttpOnly": None}
        for name, morsel in parsed.items():
            cookie = Cookie(
                version=0,
                name=name,
                value=morsel.value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest=rest,
                rfc2109=False,
            )
            self.cookies.set_cookie(cookie)
            count += 1
        self.save_cookies()
        return count

    def cookie_summary(self) -> dict[str, Any]:
        cookies = list(self.cookies)
        return {
            "profile": self.profile,
            "count": len(cookies),
            "domains": sorted({cookie.domain for cookie in cookies}),
            "path": str(self.cookie_path),
            "mode": oct(os.stat(self.cookie_path).st_mode & 0o777)
            if self.cookie_path.exists()
            else None,
        }


@dataclass
class AdapterContext:
    paths: StatePaths
    profile: str = "default"
    fresh: bool = False
    cancellation: CancellationToken | None = None

    def session(self, site: str) -> HttpSession:
        return HttpSession(
            self.paths,
            site,
            self.profile,
            fresh=self.fresh,
            cancellation=self.cancellation,
        )


class SiteAdapter:
    """Small shared base for typed site adapters.

    A new adapter normally only sets ``site_name`` and implements public methods.
    The runtime-facing dispatch and persistent HTTP session are deliberately kept
    here so adapters cannot accidentally expose private helpers as commands.
    """

    site_name = ""
    base_url = ""
    allowed_domains: tuple[str, ...] = ()

    def __init__(self, context: AdapterContext) -> None:
        if not self.site_name:
            raise AgentWebError("Adapter must define site_name")
        self.context = context
        self._session: HttpSession | None = None

    def session(self) -> HttpSession:
        if self._session is None:
            self._session = self.context.session(self.site_name)
        return self._session

    def session_freshness(
        self, authenticated: bool, *, state: str | None = None
    ) -> dict[str, Any]:
        """Describe what was verified now, without pretending cookies have a reliable TTL."""
        now = int(time.time())
        expiries = sorted(
            int(cookie.expires)
            for cookie in self.session().cookies
            if cookie.expires is not None and int(cookie.expires) > now
        )
        earliest = expiries[0] if expiries else None
        return {
            "state": state
            or ("verified_now" if authenticated else "signed_out_or_expired"),
            "checked_at_unix": now,
            "earliest_cookie_expiry_unix": earliest,
            "seconds_until_earliest_cookie_expiry": earliest - now if earliest else None,
            "expiry_is_predictable": bool(earliest),
            "recheck_before_account_write": True,
            "note": "Websites can revoke sessions before cookie expiry; this status is a live check, not a promise of future validity.",
        }

    def call(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        method = getattr(self, action, None)
        if method is None or action.startswith("_") or not callable(method):
            raise AgentWebError(
                f"{self.site_name.title()} action {action!r} is not implemented"
            )
        result = method(**arguments)
        if not isinstance(result, dict):
            raise AgentWebError(
                f"{self.site_name}.{action} returned a non-object result"
            )
        return result

    @staticmethod
    def require_confirm(confirm: bool, operation: str) -> None:
        if not confirm:
            raise AgentWebError(
                f"{operation} changes remote state; inspect the inputs and repeat with confirm=true"
            )

    def direct_headers(self, url: str) -> dict[str, str]:
        """Adapter hook for authentication headers on its direct protocol surface."""
        return {}

    def direct_impersonation(self) -> str | None:
        """Adapter hook for sites that bind sessions to a browser-like transport."""
        return None

    def direct_variables(self) -> dict[str, Any]:
        """Adapter hook for non-public values used by compiled multi-step recipes."""
        return {}

    def _direct_url(self, path: str) -> str:
        if not self.base_url or not self.allowed_domains:
            raise AgentWebError(
                f"{self.site_name} does not declare its direct protocol hosts"
            )
        url = urljoin(self.base_url.rstrip("/") + "/", path)
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(
            host == domain or host.endswith("." + domain)
            for domain in self.allowed_domains
        ):
            raise AgentWebError(
                f"Direct request for {self.site_name} escaped its HTTPS allowlist"
            )
        return url

    @staticmethod
    def _direct_header_map(
        values: list[str] | dict[str, Any] | None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        entries: list[tuple[str, Any]] = []
        if isinstance(values, dict):
            entries = list(values.items())
        else:
            for value in values or []:
                if "=" not in value:
                    raise AgentWebError(f"Header {value!r} must use name=value")
                name_part, value_part = value.split("=", 1)
                entries.append((name_part, value_part))
        for name, item in entries:
            name = name.strip()
            if not re.fullmatch(r"[A-Za-z0-9-]+", name):
                raise AgentWebError(f"Header name {name!r} is invalid")
            if name.lower() in {
                "host",
                "cookie",
                "content-length",
                "transfer-encoding",
            }:
                raise AgentWebError(f"Header {name!r} is managed by AgentWeb")
            headers[name] = str(item)
        return headers

    @staticmethod
    def _redacted_url(url: str) -> str:
        parsed = urlparse(url)
        sensitive = {"auth", "token", "key", "signature", "sig", "hmac", "code"}
        query = [
            (name, "[redacted]" if name.lower() in sensitive else value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    @classmethod
    def _redact_secret_fields(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [cls._redact_secret_fields(item) for item in value]
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                sensitive = normalized in {
                    "auth", "token", "access_token", "refresh_token", "csrf",
                    "csrf_token", "hmac", "secret", "signature", "password", "cookie",
                } or normalized.endswith(("_token", "_secret", "_password", "_hmac"))
                output[key] = "[redacted]" if sensitive else cls._redact_secret_fields(item)
            return output
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return cls._redacted_url(value)
        return value

    @staticmethod
    def _human_challenge(response: Response) -> bool:
        header_challenge = any(
            name.lower() in {"x-amzn-waf-action", "cf-mitigated"}
            and value.lower() in {"challenge", "captcha"}
            for name, value in response.headers.items()
        )
        if header_challenge:
            return True
        text = response.text.lower()
        if "verify you are human" in text or "one-time password" in text:
            return True
        if "<html" not in text:
            return False
        soup = BeautifulSoup(response.text, "html.parser")
        return (
            soup.select_one(
                ".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], "
                "iframe[src*='captcha'], #challenge-container, input[autocomplete='one-time-code']"
            )
            is not None
        )

    def _direct_response(
        self,
        *,
        path: str,
        method: str,
        query: list[str] | dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        json_body: Any | None = None,
        body: str | None = None,
        body_encoding: str = "utf8",
        files: list[dict[str, Any]] | None = None,
        headers: list[str] | dict[str, Any] | None = None,
    ) -> Response:
        method = method.upper()
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise AgentWebError("method must be GET, HEAD, POST, PUT, PATCH, or DELETE")
        url = self._direct_url(path)
        request_headers = self.direct_headers(url)
        request_headers.update(self._direct_header_map(headers))
        raw_body: bytes | str | None = body
        if body is not None and body_encoding == "base64":
            try:
                raw_body = base64.b64decode(body, validate=True)
            except ValueError as exc:
                raise AgentWebError("body is not valid base64") from exc
        elif body_encoding != "utf8":
            raise AgentWebError("body_encoding must be utf8 or base64")
        return self.session().request(
            method,
            url,
            params=parse_query_pairs(query),
            form=form,
            json_body=json_body,
            body=raw_body,
            files=files,
            headers=request_headers,
            cache_ttl=0,
            impersonate=self.direct_impersonation(),
            allowed_redirect_domains=self.allowed_domains,
        )

    def direct_request(
        self,
        path: str,
        method: str = "GET",
        query: list[str] | None = None,
        form: dict[str, Any] | None = None,
        json_body: Any | None = None,
        body: str | None = None,
        body_encoding: str = "utf8",
        files: list[dict[str, Any]] | None = None,
        headers: list[str] | None = None,
        confirm: bool = False,
        max_items: int = 100,
        max_string: int = 10000,
        max_total_chars: int = 50000,
    ) -> dict[str, Any]:
        method = method.upper()
        mutating = method not in {"GET", "HEAD"}
        if mutating:
            self.require_confirm(confirm, f"{self.site_name}.direct_request")
        response = self._direct_response(
            path=path,
            method=method,
            query=query,
            form=form,
            json_body=json_body,
            body=body,
            body_encoding=body_encoding,
            files=files,
            headers=headers,
        )
        try:
            payload: Any = json.loads(response.text)
        except json.JSONDecodeError:
            payload = response.text
        payload = self._redact_secret_fields(payload)
        payload, truncated = bounded_data(
            payload, max_items=max_items, max_string=max_string
        )
        payload, total_truncated, original_chars = enforce_data_budget(
            payload, max_total_chars=max_total_chars
        )
        safe_headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() not in {"set-cookie", "authorization", "proxy-authenticate"}
        }
        return {
            "operation": f"{self.site_name}.direct_request",
            "method": method,
            "status": response.status,
            "url": self._redacted_url(response.url),
            "headers": safe_headers,
            "data": payload,
            "state_changed": mutating,
            "human_challenge_detected": self._human_challenge(response),
            "truncated": truncated or total_truncated,
            "original_chars": original_chars,
            "elapsed_ms": round(response.elapsed_ms, 2),
        }

    def inspect_page(
        self,
        path: str = "/",
        query: str | None = None,
        limit: int = 80,
        max_text_chars: int = 10000,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 300:
            raise AgentWebError("limit must be between 1 and 300")
        response = self._direct_response(path=path, method="GET")
        soup = BeautifulSoup(response.text, "html.parser")
        for node in soup.select("script, style, noscript, template"):
            node.decompose()
        page_text = " ".join(soup.get_text(" ", strip=True).split())
        lowered_query = (query or "").lower()
        controls: list[dict[str, Any]] = []
        for index, form in enumerate(soup.select("form")):
            fields = []
            for node in form.select("input[name], textarea[name], select[name]"):
                field: dict[str, Any] = {
                    "name": node.get("name"),
                    "type": node.get("type") or node.name,
                    "required": node.has_attr("required"),
                }
                if node.name == "select":
                    field["options"] = [
                        {
                            "value": option.get("value"),
                            "label": option.get_text(" ", strip=True),
                        }
                        for option in node.select("option")[:50]
                    ]
                fields.append(field)
            item = {
                "kind": "form",
                "selector": f"form:nth-of-type({index + 1})",
                "action": self._redacted_url(
                    urljoin(response.url, str(form.get("action") or response.url))
                ),
                "method": str(form.get("method") or "GET").upper(),
                "fields": fields,
                "text": " ".join(form.get_text(" ", strip=True).split())[:1000],
            }
            if not lowered_query or lowered_query in json.dumps(item).lower():
                controls.append(item)
        for link in soup.select("a[href]"):
            item = {
                "kind": "link",
                "text": " ".join(link.get_text(" ", strip=True).split())[:500],
                "href": self._redacted_url(
                    urljoin(response.url, str(link.get("href") or ""))
                ),
            }
            if not lowered_query or lowered_query in json.dumps(item).lower():
                controls.append(item)
            if len(controls) >= limit:
                break
        return {
            "operation": f"{self.site_name}.inspect_page",
            "status": response.status,
            "url": self._redacted_url(response.url),
            "title": soup.title.get_text(" ", strip=True) if soup.title else None,
            "text": page_text[:max_text_chars],
            "text_truncated": len(page_text) > max_text_chars,
            "controls": controls[:limit],
            "human_challenge_detected": self._human_challenge(response),
            "elapsed_ms": round(response.elapsed_ms, 2),
        }

    def submit_form(
        self,
        page_path: str,
        selector: str,
        fields: dict[str, Any],
        confirm: bool = False,
    ) -> dict[str, Any]:
        self.require_confirm(confirm, f"{self.site_name}.submit_form")
        preflight = self._direct_response(path=page_path, method="GET")
        soup = BeautifulSoup(preflight.text, "html.parser")
        form = soup.select_one(selector)
        if form is None:
            raise AgentWebError(
                f"{self.site_name} page did not contain form selector {selector!r}"
            )
        values: dict[str, Any] = {}
        for node in form.select("input[name], textarea[name], select[name]"):
            name = str(node.get("name"))
            if node.name == "select":
                selected = node.select_one("option[selected]") or node.select_one(
                    "option"
                )
                values[name] = selected.get("value") if selected else ""
            elif node.get("type") in {"checkbox", "radio"} and not node.has_attr(
                "checked"
            ):
                continue
            else:
                values[name] = node.get("value") or node.get_text() or ""
        values.update(fields)
        method = str(form.get("method") or "POST").upper()
        action = str(form.get("action") or page_path)
        response = self._direct_response(
            path=action,
            method=method,
            query=[f"{key}={value}" for key, value in values.items()]
            if method in {"GET", "HEAD"}
            else None,
            form=values if method not in {"GET", "HEAD"} else None,
            headers=[f"Referer={preflight.url}"],
        )
        return {
            "operation": f"{self.site_name}.submit_form",
            "status": response.status,
            "url": self._redacted_url(response.url),
            "state_changed": method not in {"GET", "HEAD"},
            "hidden_fields_exposed": False,
            "elapsed_ms": round(preflight.elapsed_ms + response.elapsed_ms, 2),
        }

    def direct_workflow(
        self,
        steps: list[dict[str, Any]],
        variables: dict[str, Any] | None = None,
        confirm: bool = False,
        max_items: int = 100,
        max_string: int = 10000,
        max_total_chars: int = 50000,
    ) -> dict[str, Any]:
        if not steps:
            raise AgentWebError("direct_workflow requires at least one step")
        mutating = any(
            bool(
                step.get(
                    "mutating",
                    str(step.get("method") or "GET").upper() not in {"GET", "HEAD"},
                )
            )
            for step in steps
        )
        if mutating:
            self.require_confirm(confirm, f"{self.site_name}.direct_workflow")
        values = {**self.direct_variables(), **(variables or {})}
        metadata: list[dict[str, Any]] = []
        response: Response | None = None
        payload: Any = None
        exposed: dict[str, Any] = {}
        sensitive_values: list[Any] = []
        for index, raw_step in enumerate(steps):
            step = RequestRecipeAdapter._render(raw_step, values)
            response = self._direct_response(
                path=str(step.get("path") or ""),
                method=str(step.get("method") or "GET"),
                query=step.get("query"),
                form=step.get("form"),
                json_body=step.get("json"),
                body=step.get("body"),
                body_encoding=str(step.get("body_encoding") or "utf8"),
                files=step.get("files"),
                headers=step.get("headers"),
            )
            try:
                payload = json.loads(response.text)
            except json.JSONDecodeError:
                payload = response.text
            if step.get("response_mode") == "download":
                filename = safe_component(
                    str(
                        step.get("filename")
                        or Path(urlparse(response.url).path).name
                        or "download.bin"
                    ),
                    label="download filename",
                )
                profile = safe_component(self.context.profile, label="profile")
                download_path = contained_path(
                    self.context.paths.root / "downloads",
                    safe_component(self.site_name, label="site"),
                    profile,
                    filename,
                )
                atomic_write(download_path, response.body, mode=0o600)
                payload = {
                    "filename": filename,
                    "path": str(download_path),
                    "bytes": len(response.body),
                    "sha256": hashlib.sha256(response.body).hexdigest(),
                    "content_type": response.headers.get("content-type")
                    or response.headers.get("Content-Type")
                    or "application/octet-stream",
                }
            elif step.get("response_mode") == "html_links":
                selector = str(step.get("selector") or "a[href]")
                context_selector = str(step.get("context_selector") or "")
                context_text_selector = str(step.get("context_text_selector") or "")
                link_attributes = step.get("link_attributes") or ["href"]
                link_replacements = step.get("link_replacements") or {}
                soup = BeautifulSoup(response.text, "html.parser")
                context_nodes = (
                    {id(node): node for node in soup.select(context_selector)}
                    if context_selector
                    else {}
                )
                links: list[dict[str, str]] = []
                seen: set[tuple[str, str]] = set()
                for node in soup.select(selector):
                    href = next(
                        (
                            str(node.get(attribute) or "").strip()
                            for attribute in link_attributes
                            if str(node.get(attribute) or "").strip()
                        ),
                        "",
                    )
                    if not href:
                        continue
                    for before, after in link_replacements.items():
                        href = href.replace(str(before), str(after))
                    url = urljoin(response.url, href)
                    if urlparse(url).scheme not in {"http", "https"}:
                        continue
                    text = " ".join(node.get_text(" ", strip=True).split())
                    key = (text, url)
                    if key in seen:
                        continue
                    seen.add(key)
                    item = {"text": text, "url": url}
                    if context_selector:
                        context = next(
                            (
                                context_nodes[id(parent)]
                                for parent in node.parents
                                if id(parent) in context_nodes
                            ),
                            None,
                        )
                        context_text = (
                            context.select_one(context_text_selector)
                            if context is not None and context_text_selector
                            else context
                        )
                        item["context"] = (
                            " ".join(context_text.get_text(" ", strip=True).split())
                            if context_text
                            else ""
                        )
                    links.append(item)
                payload = links
            elif step.get("response_mode") == "xml_feed":
                payload = parse_xml_feed(response.body)
            for name, specification in (raw_step.get("extract") or {}).items():
                value = RequestRecipeAdapter._extract_value(
                    specification, response, payload
                )
                values[name] = value
                sensitive_name = bool(
                    re.search(r"token|secret|auth|hmac|password|cookie", name, re.I)
                )
                if sensitive_name:
                    sensitive_values.append(value)
                if specification.get("expose") is True and not sensitive_name:
                    exposed[name] = value
            metadata.append(
                {
                    "name": raw_step.get("name") or f"step_{index + 1}",
                    "method": str(step.get("method") or "GET").upper(),
                    "status": response.status,
                    "url": self._redacted_url(response.url),
                    "elapsed_ms": round(response.elapsed_ms, 2),
                }
            )
        assert response is not None
        payload = redact_sensitive_values(payload, sensitive_values)
        payload, truncated = bounded_data(
            payload, max_items=max_items, max_string=max_string
        )
        payload, total_truncated, original_chars = enforce_data_budget(
            payload, max_total_chars=max_total_chars
        )
        return {
            "operation": f"{self.site_name}.direct_workflow",
            "steps": metadata,
            "data": payload,
            "extracted": exposed,
            "state_changed": mutating,
            "truncated": truncated or total_truncated,
            "original_chars": original_chars,
        }


class RequestRecipeAdapter(SiteAdapter):
    """Browserless adapter whose captured requests are expressed as small recipes.

    This is the fast path for adding sites: define request recipes for straightforward
    endpoints, and add Python methods only for flows that need token/session logic.
    """

    base_url = ""
    allowed_domains: tuple[str, ...] = ()
    recipes: dict[str, dict[str, Any]] = {}

    def recipe_variables(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Adapter hook for credentials or computed values used by many recipes."""
        return dict(arguments)

    @classmethod
    def _render(cls, value: Any, arguments: dict[str, Any]) -> Any:
        if isinstance(value, str):
            exact = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
            if exact:
                key = exact.group(1)
                if key not in arguments:
                    raise AgentWebError(f"Missing recipe input {key!r}")
                return arguments[key]
            try:
                return value.format_map(arguments)
            except KeyError as exc:
                raise AgentWebError(f"Missing recipe input {exc.args[0]!r}") from exc
        if isinstance(value, list):
            return [cls._render(item, arguments) for item in value]
        if isinstance(value, dict):
            output = {}
            for key, item in value.items():
                rendered = cls._render(item, arguments)
                if rendered is not None:
                    output[key] = rendered
            return output
        return value

    def _recipe_url(self, path_template: str, variables: dict[str, Any]) -> str:
        try:
            rendered_path = path_template.format_map(
                {key: quote(str(value), safe="") for key, value in variables.items()}
            )
        except KeyError as exc:
            raise AgentWebError(f"Missing recipe input {exc.args[0]!r}") from exc
        url = urljoin(self.base_url.rstrip("/") + "/", rendered_path)
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(
            host == domain or host.endswith("." + domain)
            for domain in self.allowed_domains
        ):
            raise AgentWebError(
                f"Recipe for {self.site_name} escaped its allowed domains"
            )
        return url

    @staticmethod
    def _json_path(payload: Any, path: str) -> Any:
        value = payload
        for part in path.split(".") if path else []:
            if isinstance(value, list):
                try:
                    value = value[int(part)]
                except (ValueError, IndexError) as exc:
                    raise AgentWebError(
                        f"Recipe JSON path {path!r} did not match"
                    ) from exc
            elif isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise AgentWebError(f"Recipe JSON path {path!r} did not match")
        return value

    @classmethod
    def _extract_value(
        cls, spec: dict[str, Any], response: Response, payload: Any
    ) -> Any:
        source = str(spec.get("source") or "json")
        if source == "json":
            return cls._json_path(payload, str(spec.get("path") or ""))
        if source == "header":
            name = str(spec.get("name") or "").lower()
            value = next(
                (item for key, item in response.headers.items() if key.lower() == name),
                None,
            )
            if value is None:
                raise AgentWebError(f"Recipe response did not contain header {name!r}")
            return value
        if source == "regex":
            pattern = str(spec.get("pattern") or "")
            match = re.search(pattern, response.text, re.DOTALL)
            if not match:
                raise AgentWebError(
                    f"Recipe response did not match extraction pattern {pattern!r}"
                )
            return match.group(int(spec.get("group") or 1))
        if source == "html":
            selector = str(spec.get("selector") or "")
            node = BeautifulSoup(response.text, "html.parser").select_one(selector)
            if node is None:
                raise AgentWebError(
                    f"Recipe response did not match HTML selector {selector!r}"
                )
            attribute = spec.get("attribute")
            if attribute:
                attr_value = node.get(str(attribute))
                if attr_value is None:
                    raise AgentWebError(
                        f"Recipe HTML selector {selector!r} had no {attribute!r} attribute"
                    )
                if isinstance(attr_value, list):
                    return " ".join(attr_value)
                return attr_value
            return node.get_text(" ", strip=True)
        if source == "text":
            return response.text
        raise AgentWebError(f"Unsupported recipe extraction source {source!r}")

    def call(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if action not in self.recipes:
            return super().call(action, arguments)
        recipe = self.recipes[action]
        steps = recipe.get("steps") or [recipe]
        if not isinstance(steps, list) or not steps:
            raise AgentWebError(f"Recipe for {self.site_name}.{action} has no steps")
        mutating = any(
            bool(
                step.get(
                    "mutating",
                    str(step.get("method") or "GET").upper() not in {"GET", "HEAD"},
                )
            )
            for step in steps
        )
        if mutating:
            self.require_confirm(
                bool(arguments.get("confirm")), f"{self.site_name}.{action}"
            )
        variables = self.recipe_variables(arguments)
        exposed: dict[str, Any] = {}
        sensitive_values: list[Any] = []
        response: Response | None = None
        payload: Any = None
        step_meta: list[dict[str, Any]] = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise AgentWebError(f"Recipe step {index + 1} must be an object")
            method = str(step.get("method") or "GET").upper()
            rendered = self._render(
                {
                    key: value
                    for key, value in step.items()
                    if key not in {"path", "extract", "steps"}
                },
                variables,
            )
            url = self._recipe_url(str(step.get("path") or ""), variables)
            response = self.session().request(
                method,
                url,
                params=rendered.get("query"),
                form=rendered.get("form"),
                json_body=rendered.get("json"),
                headers=rendered.get("headers"),
                cache_action=f"{action}:{index}" if method == "GET" else None,
                cache_arguments={
                    key: value for key, value in arguments.items() if key != "confirm"
                },
                cache_ttl=int(step.get("cache_ttl") or recipe.get("cache_ttl") or 0),
            )
            expected = step.get("expected_status")
            allowed_statuses = (
                {int(item) for item in expected}
                if isinstance(expected, list)
                else {int(expected)}
                if expected is not None
                else set(range(200, 400))
            )
            if response.status not in allowed_statuses:
                host = (urlparse(response.url).hostname or "").lower()
                raise AgentWebError(
                    f"{self.site_name}.{action} step {index + 1} returned HTTP {response.status} from {host}"
                )
            try:
                payload = json.loads(response.text)
            except json.JSONDecodeError:
                payload = response.text
            for name, raw_spec in (step.get("extract") or {}).items():
                if not isinstance(raw_spec, dict):
                    raise AgentWebError(f"Recipe extraction {name!r} must be an object")
                value = self._extract_value(raw_spec, response, payload)
                variables[name] = value
                sensitive_name = bool(
                    re.search(
                        r"token|secret|auth|hmac|password|cookie|csrf|xsrf", name, re.I
                    )
                )
                if sensitive_name:
                    sensitive_values.append(value)
                if raw_spec.get("expose") is True and not sensitive_name:
                    exposed[name] = value
            step_meta.append(
                {
                    "name": step.get("name") or f"step_{index + 1}",
                    "method": method,
                    "status": response.status,
                    "elapsed_ms": round(response.elapsed_ms, 2),
                    "from_cache": response.from_cache,
                }
            )
        assert response is not None
        try:
            output = self._json_path(payload, str(recipe.get("result_path") or ""))
        except AgentWebError:
            raise
        output = redact_sensitive_values(output, sensitive_values)
        output, truncated = bounded_data(
            output,
            max_items=int(recipe.get("max_items") or 100),
            max_string=int(recipe.get("max_string") or 10000),
        )
        payload, total_truncated, original_chars = enforce_data_budget(
            output, max_total_chars=int(recipe.get("max_total_chars") or 50000)
        )
        return {
            "operation": f"{self.site_name}.{action}",
            "status": response.status,
            "url": self._redacted_url(response.url),
            "data": payload,
            "extracted": exposed,
            "steps": step_meta,
            "state_changed": mutating,
            "truncated": truncated or total_truncated,
            "original_chars": original_chars,
            "elapsed_ms": round(response.elapsed_ms, 2),
            "from_cache": response.from_cache,
        }
