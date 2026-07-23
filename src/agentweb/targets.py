from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import re
from urllib.parse import parse_qs, unquote, urlparse

from .sdk import AgentWebError


@dataclass(frozen=True)
class ResolvedTarget:
    site: str
    domain: str
    url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"site": self.site, "domain": self.domain}
        if self.url:
            value["url"] = self.url
        return value


def normalized_host(value: str) -> str:
    candidate = value.strip().lower()
    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    return (parsed.hostname or "").rstrip(".")


def canonical_domain(manifest: dict[str, Any]) -> str:
    explicit = str(manifest.get("canonical_domain") or "").strip().lower()
    if explicit:
        return explicit
    host = normalized_host(str(manifest.get("base_url") or ""))
    return host.removeprefix("www.")


def target_url(value: str) -> str | None:
    candidate = value.strip()
    if "://" not in candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AgentWebError(
            f"Target URL {value!r} is not a valid HTTP(S) URL",
            code="invalid_target",
            field="target",
        )
    return candidate


def host_matches(host: str, allowed: str) -> bool:
    allowed = allowed.lower().lstrip(".")
    return host == allowed or host.endswith("." + allowed)


def extract_resource(
    manifest: dict[str, Any], url: str
) -> tuple[str, dict[str, Any]] | None:
    """Apply adapter-declared URL routes to select the narrowest typed operation."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    subject = unquote(parsed.path)
    host = parsed.hostname or ""
    for route in manifest.get("url_routes") or []:
        host_match = re.fullmatch(str(route.get("host_regex") or ".*"), host)
        path_match = re.fullmatch(str(route.get("path_regex") or ""), subject)
        if not host_match or not path_match:
            continue
        groups = {**host_match.groupdict(), **path_match.groupdict()}
        arguments: dict[str, Any] = {}
        valid = True
        for name, rule in (route.get("arguments") or {}).items():
            if rule.get("url") is True:
                value: Any = url
            elif "value" in rule:
                value: Any = rule["value"]
            elif "query" in rule:
                value = (query.get(str(rule["query"])) or [None])[0]
            else:
                value = groups.get(str(rule.get("group") or name))
            if value is None:
                valid = False
                break
            if rule.get("transform") == "integer":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    valid = False
                    break
            elif rule.get("transform") == "underscores_to_spaces":
                value = str(value).replace("_", " ")
            arguments[str(name)] = value
        if valid:
            return str(route["operation"]), arguments
    return None
