from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from .registry import validate_manifest
from .sdk import AgentWebError


NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")


def create_adapter(
    root: Path,
    name: str,
    base_url: str,
    *,
    version: str = "0.1.0",
) -> dict[str, str]:
    """Create the smallest safe, distributable full-site adapter skeleton."""
    if not NAME_PATTERN.fullmatch(name):
        raise AgentWebError("site name must be 2-40 lowercase letters, digits, _ or -")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise AgentWebError("base_url must be an absolute HTTPS URL")
    destination = root / "sites" / name / version
    if destination.exists():
        raise AgentWebError(f"Refusing to overwrite existing adapter {destination}")
    manifest = {
        "name": name,
        "version": version,
        "description": f"Complete agent interface for {parsed.hostname}",
        "canonical_domain": parsed.hostname.removeprefix("www."),
        "aliases": [name],
        "url_routes": [],
        "entrypoint": "adapter.py",
        "base_url": base_url.rstrip("/"),
        "allowed_domains": [parsed.hostname],
        "cookie_domain": f".{parsed.hostname}",
        "auth": {"strategy": "none", "interaction": "not_required"},
        "runtime": {
            "browserless_replay": True,
            "auth_interaction": "not_yet_mapped",
        },
        "coverage": {
            "typed_fast_paths": ["home-page availability"],
            "website_parity": "Mapping mode can inspect the remaining website while direct request recipes are compiled. Browser commands are never counted as runtime parity.",
            "not_mapped": ["Every action beyond the generated public home-page probe"],
            "constraints": [
                "Site permissions, rate limits, and anti-automation rules still apply"
            ],
        },
        "commands": {
            "home": {
                "description": "Fetch the public home page without starting a browser.",
                "cli": {"positionals": []},
                "input_schema": {"type": "object", "properties": {}},
            }
        },
    }
    adapter_source = f"""from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = {name!r}
    base_url = {base_url.rstrip("/")!r}
    allowed_domains = ({parsed.hostname!r},)
    recipes = {{
        "home": {{"method": "GET", "path": "/", "cache_ttl": 60}}
    }}
"""
    destination.mkdir(parents=True)
    manifest_path = destination / "manifest.json"
    adapter_path = destination / "adapter.py"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    adapter_path.write_text(adapter_source)
    validate_manifest(manifest, path=manifest_path)
    return {
        "site": name,
        "version": version,
        "directory": str(destination),
        "manifest": str(manifest_path),
        "adapter": str(adapter_path),
        "next": f"Implement typed methods, add declarative url_routes for canonical resource URLs, declare every unavoidable gap, then run `agentweb audit {parsed.hostname}` and `agentweb registry-build {root}`.",
    }
