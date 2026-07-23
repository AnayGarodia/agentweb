from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import tarfile
from datetime import date
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlparse

from agentweb.sdk import AgentWebError, RequestRecipeAdapter


_PACKAGE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]*$")
_PERIOD = re.compile(
    r"^(?:last-day|last-week|last-month|last-year|[0-9]{4}-[0-9]{2}-[0-9]{2}:[0-9]{4}-[0-9]{2}-[0-9]{2})$"
)


class _NpmHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self.text: list[str] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"script", "style", "noscript", "svg"}:
            self._hidden_depth += 1
        if tag == "a" and self._hidden_depth == 0:
            self._href = str(values.get("href") or "")
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            label = " ".join(" ".join(self._link_text).split())
            self.links.append({"href": self._href, "label": label})
            self._href = None
            self._link_text = []
        if tag in {"script", "style", "noscript", "svg"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden_depth:
            return
        value = " ".join(data.split())
        if not value:
            return
        self.text.append(value)
        if self._href is not None:
            self._link_text.append(value)


class Adapter(RequestRecipeAdapter):
    site_name = "npm"
    base_url = "https://www.npmjs.com"
    allowed_domains = (
        "npmjs.com",
        "registry.npmjs.org",
        "api.npmjs.org",
        "status.npmjs.org",
        "docs.npmjs.com",
    )
    recipes = {"home": {"method": "GET", "path": "/", "cache_ttl": 60}}

    @staticmethod
    def _result(
        action: str,
        data: dict[str, Any],
        *,
        pagination: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        transport: str = "typed_adapter",
    ) -> dict[str, Any]:
        return {
            "operation": f"npm.{action}",
            "data": data,
            "state_change": {
                "changed": False,
                "reversible": False,
                "idempotent": True,
            },
            "pagination": pagination or {"supported": False},
            "warnings": warnings or [],
            "verification": {
                "verified": True,
                "reviewed": True,
                "transport": transport,
            },
        }

    @staticmethod
    def _package(value: str) -> str:
        candidate = value.strip().lower()
        if len(candidate) > 214 or not _PACKAGE.fullmatch(candidate):
            raise AgentWebError(
                "Package must be an npm name such as react or @scope/package",
                code="invalid_package_name",
                field="package",
                retryable=False,
                user_action="Use the exact package name shown by npm search.",
                next_action="npm.search_packages",
            )
        return candidate

    @staticmethod
    def _version(value: str) -> str:
        candidate = value.strip()
        if len(candidate) > 128 or not _VERSION.fullmatch(candidate):
            raise AgentWebError(
                "Version must be an exact npm version or distribution tag such as latest",
                code="invalid_version",
                field="version",
                retryable=False,
                user_action="Use a version or tag returned by get_package or list_versions.",
                next_action="npm.list_versions",
            )
        return candidate

    @staticmethod
    def _package_path(package: str) -> str:
        return quote(package, safe="")

    def _response(
        self,
        method: str,
        url: str,
        *,
        action: str,
        arguments: dict[str, Any],
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        accept: str = "application/json",
        impersonate: bool = False,
        cache_ttl: int = 60,
    ):
        response = self.session().request(
            method,
            url,
            params=params,
            json_body=json_body,
            headers={"Accept": accept},
            cache_action=action,
            cache_arguments=arguments,
            cache_ttl=cache_ttl if method == "GET" else 0,
            impersonate="chrome" if impersonate else None,
            allowed_redirect_domains=self.allowed_domains,
            timeout_seconds=30,
        )
        if response.status < 400:
            return response
        package = str(arguments.get("package") or "")
        if response.status == 404:
            raise AgentWebError(
                f"npm could not find {package or 'the requested resource'}",
                code="package_not_found" if package else "resource_not_found",
                field="package" if package else None,
                retryable=False,
                user_action="Check the name with search_packages and retry with a public package.",
                next_action="npm.search_packages",
            )
        if response.status == 429:
            raise AgentWebError(
                "npm is rate limiting this client",
                code="npm_rate_limited",
                retryable=True,
                retry_after_seconds=10,
                user_action="Wait at least ten seconds before retrying the same operation.",
            )
        raise AgentWebError(
            f"npm returned HTTP {response.status}",
            code="npm_unavailable",
            retryable=response.status >= 500,
            user_action="Retry later if npm is temporarily unavailable.",
            next_action="npm.get_registry_status",
        )

    def _json(
        self,
        method: str,
        url: str,
        *,
        action: str,
        arguments: dict[str, Any],
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        accept: str = "application/json",
        cache_ttl: int = 60,
    ) -> dict[str, Any]:
        response = self._response(
            method,
            url,
            action=action,
            arguments=arguments,
            params=params,
            json_body=json_body,
            accept=accept,
            cache_ttl=cache_ttl,
        )
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentWebError(
                "npm returned malformed JSON",
                code="invalid_npm_response",
                retryable=True,
                user_action="Retry once; check npm status if the error continues.",
                next_action="npm.get_registry_status",
            ) from exc
        if not isinstance(payload, dict):
            raise AgentWebError(
                "npm returned an unexpected JSON response",
                code="invalid_npm_response",
                retryable=True,
                user_action="Retry once; check npm status if the error continues.",
                next_action="npm.get_registry_status",
            )
        return payload

    @staticmethod
    def _person(value: Any) -> dict[str, Any] | None:
        if isinstance(value, str):
            return {"name": value}
        if not isinstance(value, dict):
            return None
        result = {
            key: value.get(key)
            for key in ("name", "url", "username")
            if value.get(key) not in (None, "")
        }
        return result or None

    @staticmethod
    def _url(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return value.get("url")
        return None

    @classmethod
    def _version_record(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        dependencies = {
            "runtime": value.get("dependencies") or {},
            "development": value.get("devDependencies") or {},
            "peer": value.get("peerDependencies") or {},
            "optional": value.get("optionalDependencies") or {},
            "bundled": value.get("bundledDependencies")
            or value.get("bundleDependencies")
            or [],
        }
        dist = value.get("dist") if isinstance(value.get("dist"), dict) else {}
        record = {
            "name": value.get("name"),
            "version": value.get("version"),
            "description": value.get("description"),
            "deprecated": value.get("deprecated"),
            "license": value.get("license"),
            "author": cls._person(value.get("author")),
            "maintainers": [
                person
                for person in (cls._person(item) for item in value.get("maintainers") or [])
                if person
            ],
            "keywords": value.get("keywords") or [],
            "homepage": value.get("homepage"),
            "repository": cls._url(value.get("repository")),
            "bugs": cls._url(value.get("bugs")),
            "engines": value.get("engines") or {},
            "os": value.get("os") or [],
            "cpu": value.get("cpu") or [],
            "exports": value.get("exports"),
            "main": value.get("main"),
            "module": value.get("module"),
            "types": value.get("types") or value.get("typings"),
            "dependencies": dependencies,
            "peer_dependencies_meta": value.get("peerDependenciesMeta") or {},
            "dist": {
                key: dist.get(key)
                for key in (
                    "tarball",
                    "shasum",
                    "integrity",
                    "fileCount",
                    "unpackedSize",
                    "signatures",
                    "attestations",
                )
                if dist.get(key) not in (None, "", [], {})
            },
        }
        return {key: item for key, item in record.items() if item not in (None, "")}

    @staticmethod
    def _page(
        items: list[Any], *, cursor: str | None, limit: int
    ) -> tuple[list[Any], dict[str, Any]]:
        try:
            offset = int(cursor or 0)
        except ValueError as exc:
            raise AgentWebError(
                "Cursor must be the numeric value returned by the previous page",
                code="invalid_cursor",
                field="cursor",
                retryable=False,
                user_action="Omit cursor to restart pagination.",
            ) from exc
        if offset < 0:
            raise AgentWebError("Cursor cannot be negative", code="invalid_cursor")
        page = items[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(items) else None
        return page, {
            "supported": True,
            "cursor": str(offset),
            "next_cursor": next_cursor,
            "limit": limit,
            "returned": len(page),
            "total": len(items),
        }

    def search_packages(
        self,
        query: str,
        limit: int = 20,
        offset: int = 0,
        quality: float | None = None,
        popularity: float | None = None,
        maintenance: float | None = None,
    ) -> dict[str, Any]:
        arguments = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "quality": quality,
            "popularity": popularity,
            "maintenance": maintenance,
        }
        params: dict[str, Any] = {"text": query, "size": limit, "from": offset}
        for name, value in (
            ("quality", quality),
            ("popularity", popularity),
            ("maintenance", maintenance),
        ):
            if value is not None:
                params[name] = value
        payload = self._json(
            "GET",
            "https://registry.npmjs.org/-/v1/search",
            action="search_packages",
            arguments=arguments,
            params=params,
        )
        objects = payload.get("objects") if isinstance(payload, dict) else []
        packages = []
        for item in objects if isinstance(objects, list) else []:
            if not isinstance(item, dict):
                continue
            package = item.get("package") if isinstance(item.get("package"), dict) else {}
            score = item.get("score") if isinstance(item.get("score"), dict) else {}
            detail = score.get("detail") if isinstance(score.get("detail"), dict) else {}
            packages.append(
                {
                    "name": package.get("name"),
                    "version": package.get("version"),
                    "description": package.get("description"),
                    "keywords": package.get("keywords") or [],
                    "date": package.get("date"),
                    "links": package.get("links") or {},
                    "publisher": self._person(package.get("publisher")),
                    "maintainers": [
                        person
                        for person in (
                            self._person(value) for value in package.get("maintainers") or []
                        )
                        if person
                    ],
                    "score": score.get("final"),
                    "search_score": item.get("searchScore"),
                    "quality": detail.get("quality"),
                    "popularity": detail.get("popularity"),
                    "maintenance": detail.get("maintenance"),
                }
            )
        total = int(payload.get("total") or 0) if isinstance(payload, dict) else 0
        next_cursor = str(offset + len(packages)) if offset + len(packages) < total else None
        return self._result(
            "search_packages",
            {"query": query, "packages": packages, "total": total},
            pagination={
                "supported": True,
                "cursor": str(offset),
                "next_cursor": next_cursor,
                "limit": limit,
                "returned": len(packages),
                "total": total,
            },
        )

    def get_package(self, package: str) -> dict[str, Any]:
        package = self._package(package)
        payload = self._json(
            "GET",
            f"https://registry.npmjs.org/{self._package_path(package)}",
            action="get_package",
            arguments={"package": package},
            cache_ttl=300,
        )
        versions = payload.get("versions") if isinstance(payload, dict) else {}
        tags = payload.get("dist-tags") if isinstance(payload, dict) else {}
        latest_name = tags.get("latest") if isinstance(tags, dict) else None
        latest = versions.get(latest_name) if isinstance(versions, dict) else None
        readme = str(payload.get("readme") or "") if isinstance(payload, dict) else ""
        data = {
            "name": payload.get("name") or package,
            "description": payload.get("description"),
            "dist_tags": tags or {},
            "latest": self._version_record(latest),
            "version_count": len(versions) if isinstance(versions, dict) else 0,
            "created": (payload.get("time") or {}).get("created")
            if isinstance(payload.get("time"), dict)
            else None,
            "modified": (payload.get("time") or {}).get("modified")
            if isinstance(payload.get("time"), dict)
            else None,
            "maintainers": [
                person
                for person in (
                    self._person(value) for value in payload.get("maintainers") or []
                )
                if person
            ],
            "keywords": payload.get("keywords") or [],
            "license": payload.get("license"),
            "homepage": payload.get("homepage"),
            "repository": self._url(payload.get("repository")),
            "bugs": self._url(payload.get("bugs")),
            "readme_preview": readme[:4000],
            "readme_truncated": len(readme) > 4000,
        }
        return self._result(
            "get_package",
            {key: value for key, value in data.items() if value is not None},
        )

    def get_version(self, package: str, version: str = "latest") -> dict[str, Any]:
        package = self._package(package)
        version = self._version(version)
        payload = self._json(
            "GET",
            f"https://registry.npmjs.org/{self._package_path(package)}/{quote(version, safe='')}",
            action="get_version",
            arguments={"package": package, "version": version},
            cache_ttl=300,
        )
        return self._result("get_version", self._version_record(payload))

    def list_versions(
        self,
        package: str,
        cursor: str | None = None,
        limit: int = 50,
        include_prerelease: bool = True,
    ) -> dict[str, Any]:
        package = self._package(package)
        payload = self._json(
            "GET",
            f"https://registry.npmjs.org/{self._package_path(package)}",
            action="list_versions",
            arguments={"package": package},
            cache_ttl=300,
        )
        versions = payload.get("versions") if isinstance(payload, dict) else {}
        times = payload.get("time") if isinstance(payload, dict) else {}
        records = []
        for name, value in (versions.items() if isinstance(versions, dict) else []):
            if not include_prerelease and "-" in name:
                continue
            item = self._version_record(value)
            item["published"] = times.get(name) if isinstance(times, dict) else None
            records.append(item)
        records.sort(key=lambda item: str(item.get("published") or ""), reverse=True)
        page, pagination = self._page(records, cursor=cursor, limit=limit)
        return self._result(
            "list_versions", {"package": package, "versions": page}, pagination=pagination
        )

    def get_readme(self, package: str, max_chars: int = 20000) -> dict[str, Any]:
        package = self._package(package)
        payload = self._json(
            "GET",
            f"https://registry.npmjs.org/{self._package_path(package)}",
            action="get_readme",
            arguments={"package": package, "max_chars": max_chars},
            cache_ttl=300,
        )
        readme = str(payload.get("readme") or "") if isinstance(payload, dict) else ""
        return self._result(
            "get_readme",
            {
                "package": package,
                "filename": payload.get("readmeFilename") or "README.md",
                "content": readme[:max_chars],
                "characters": len(readme),
                "truncated": len(readme) > max_chars,
            },
            warnings=["This package does not publish a README."] if not readme else [],
        )

    def list_dependencies(
        self,
        package: str,
        version: str = "latest",
        kind: str = "all",
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        record = self.get_version(package, version)["data"]
        groups = record.get("dependencies") or {}
        names = [kind] if kind != "all" else ["runtime", "development", "peer", "optional", "bundled"]
        items: list[dict[str, Any]] = []
        for group in names:
            values = groups.get(group) or {}
            if isinstance(values, dict):
                items.extend(
                    {"kind": group, "name": name, "requirement": requirement}
                    for name, requirement in values.items()
                )
            elif isinstance(values, list):
                items.extend({"kind": group, "name": name} for name in values)
        items.sort(key=lambda item: (str(item["kind"]), str(item["name"])))
        page, pagination = self._page(items, cursor=cursor, limit=limit)
        return self._result(
            "list_dependencies",
            {"package": record.get("name"), "version": record.get("version"), "dependencies": page},
            pagination=pagination,
        )

    def list_dependents(
        self, package: str, cursor: str | None = None, limit: int = 36
    ) -> dict[str, Any]:
        package = self._package(package)
        try:
            offset = int(cursor or 0)
        except ValueError as exc:
            raise AgentWebError("Cursor must be numeric", code="invalid_cursor") from exc
        response = self._response(
            "GET",
            f"https://www.npmjs.com/browse/depended/{quote(package, safe='@/')}",
            action="list_dependents",
            arguments={"package": package, "cursor": cursor, "limit": limit},
            params={"offset": offset} if offset else None,
            accept="text/html,application/xhtml+xml",
            impersonate=True,
            cache_ttl=120,
        )
        parser = _NpmHTML()
        parser.feed(response.body.decode("utf-8", "replace"))
        names = []
        for link in parser.links:
            href = link["href"]
            if not href.startswith("/package/"):
                continue
            name = unquote(href.removeprefix("/package/").split("?", 1)[0])
            if name != package and name not in names:
                names.append(name)
        text = " ".join(parser.text)
        match = re.search(r"Dependents\s*\(([0-9,]+)\)", text)
        total = int(match.group(1).replace(",", "")) if match else None
        if total is None:
            overview = self._response(
                "GET",
                f"https://www.npmjs.com/package/{quote(package, safe='@/')}",
                action="list_dependents_total",
                arguments={"package": package},
                accept="text/html,application/xhtml+xml",
                impersonate=True,
                cache_ttl=300,
            )
            overview_parser = _NpmHTML()
            overview_parser.feed(overview.body.decode("utf-8", "replace"))
            overview_text = " ".join(overview_parser.text)
            overview_match = re.search(
                r"([0-9,]+)\s+Dependents", overview_text, re.IGNORECASE
            )
            if overview_match:
                total = int(overview_match.group(1).replace(",", ""))
        page_was_truncated = len(names) > limit
        names = names[:limit]
        next_cursor = str(offset + len(names)) if page_was_truncated else None
        if next_cursor is None:
            for link in parser.links:
                href = link["href"]
                matched = re.search(r"[?&]offset=([0-9]+)", href)
                if matched and int(matched.group(1)) > offset:
                    next_cursor = matched.group(1)
                    break
        return self._result(
            "list_dependents",
            {"package": package, "dependents": names, "total": total},
            pagination={
                "supported": True,
                "cursor": str(offset),
                "next_cursor": next_cursor,
                "limit": limit,
                "returned": len(names),
                "total": total,
            },
            transport="typed_adapter_browser_identity",
        )

    def list_maintainer_packages(
        self, username: str, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        username = username.strip().lstrip("~").lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", username):
            raise AgentWebError(
                "Username is not a valid npm account name",
                code="invalid_username",
                field="username",
                retryable=False,
            )
        result = self.search_packages(f"maintainer:{username}", limit, offset)
        result["operation"] = "npm.list_maintainer_packages"
        result["data"]["username"] = username
        return result

    def get_download_count(self, package: str, period: str = "last-week") -> dict[str, Any]:
        package = self._package(package)
        if not _PERIOD.fullmatch(period):
            raise AgentWebError(
                "Period must be last-day, last-week, last-month, last-year, or start:end dates",
                code="invalid_period",
                field="period",
                retryable=False,
            )
        payload = self._json(
            "GET",
            f"https://api.npmjs.org/downloads/point/{period}/{self._package_path(package)}",
            action="get_download_count",
            arguments={"package": package, "period": period},
            cache_ttl=300,
        )
        return self._result("get_download_count", payload if isinstance(payload, dict) else {})

    def get_download_history(
        self,
        package: str,
        start: str,
        end: str,
        cursor: str | None = None,
        limit: int = 90,
    ) -> dict[str, Any]:
        package = self._package(package)
        try:
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)
        except ValueError as exc:
            raise AgentWebError(
                "start and end must be ISO dates in YYYY-MM-DD form",
                code="invalid_date_range",
                retryable=False,
            ) from exc
        if end_date < start_date or (end_date - start_date).days > 366:
            raise AgentWebError(
                "Download history must cover a forward range of no more than 366 days",
                code="invalid_date_range",
                retryable=False,
            )
        payload = self._json(
            "GET",
            f"https://api.npmjs.org/downloads/range/{start}:{end}/{self._package_path(package)}",
            action="get_download_history",
            arguments={"package": package, "start": start, "end": end},
            cache_ttl=300,
        )
        downloads = payload.get("downloads") if isinstance(payload, dict) else []
        page, pagination = self._page(
            downloads if isinstance(downloads, list) else [], cursor=cursor, limit=limit
        )
        return self._result(
            "get_download_history",
            {
                "package": payload.get("package") or package,
                "start": payload.get("start") or start,
                "end": payload.get("end") or end,
                "downloads": page,
            },
            pagination=pagination,
        )

    def get_provenance(self, package: str, version: str = "latest") -> dict[str, Any]:
        record = self.get_version(package, version)["data"]
        package = str(record.get("name") or self._package(package))
        resolved_version = str(record.get("version") or version)
        payload = self._json(
            "GET",
            f"https://registry.npmjs.org/-/npm/v1/attestations/{self._package_path(package)}@{quote(resolved_version, safe='')}",
            action="get_provenance",
            arguments={"package": package, "version": resolved_version},
            cache_ttl=300,
        )
        attestations = []
        for item in payload.get("attestations") or [] if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            bundle = item.get("bundle") if isinstance(item.get("bundle"), dict) else {}
            verification = bundle.get("verificationMaterial") if isinstance(bundle.get("verificationMaterial"), dict) else {}
            logs = []
            for entry in verification.get("tlogEntries") or []:
                if not isinstance(entry, dict):
                    continue
                logs.append(
                    {
                        "log_index": entry.get("logIndex"),
                        "integrated_time": entry.get("integratedTime"),
                        "log_id": (entry.get("logId") or {}).get("keyId")
                        if isinstance(entry.get("logId"), dict)
                        else None,
                    }
                )
            attestations.append(
                {
                    "predicate_type": item.get("predicateType"),
                    "media_type": bundle.get("mediaType"),
                    "transparency_log_entries": logs,
                    "has_certificate": bool(verification.get("certificate")),
                    "has_public_key": bool(verification.get("publicKey")),
                }
            )
        return self._result(
            "get_provenance",
            {
                "package": package,
                "version": resolved_version,
                "attestations": attestations,
                "verified_attestation_count": len(attestations),
            },
            warnings=["This version does not publish npm provenance attestations."]
            if not attestations
            else [],
        )

    def audit_versions(self, packages: dict[str, list[str]]) -> dict[str, Any]:
        if not packages or len(packages) > 100:
            raise AgentWebError(
                "packages must contain between one and one hundred package-to-version lists",
                code="invalid_audit_input",
                field="packages",
                retryable=False,
            )
        normalized: dict[str, list[str]] = {}
        for package, versions in packages.items():
            name = self._package(package)
            if not isinstance(versions, list) or not versions or len(versions) > 100:
                raise AgentWebError(
                    "Each audit package needs one to one hundred versions",
                    code="invalid_audit_input",
                    field="packages",
                    retryable=False,
                )
            normalized[name] = [self._version(value) for value in versions]
        payload = self._json(
            "POST",
            "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk",
            action="audit_versions",
            arguments={"packages": sorted(normalized)},
            json_body=normalized,
            cache_ttl=0,
        )
        advisories = []
        for package, values in (payload.items() if isinstance(payload, dict) else []):
            for item in values if isinstance(values, list) else []:
                if not isinstance(item, dict):
                    continue
                advisories.append(
                    {
                        key: value
                        for key, value in {
                            "package": package,
                            "id": item.get("id"),
                            "title": item.get("title"),
                            "severity": item.get("severity"),
                            "vulnerable_versions": item.get("vulnerable_versions"),
                            "patched_versions": item.get("patched_versions"),
                            "url": item.get("url"),
                            "cves": item.get("cves") or [],
                        }.items()
                        if value not in (None, "")
                    }
                )
        return self._result(
            "audit_versions",
            {
                "packages": sorted(normalized),
                "advisories": advisories,
                "advisory_count": len(advisories),
            },
        )

    def _tarball(self, package: str, version: str) -> tuple[dict[str, Any], bytes]:
        record = self.get_version(package, version)["data"]
        url = str((record.get("dist") or {}).get("tarball") or "")
        if not url:
            raise AgentWebError(
                "This package version does not publish a tarball URL",
                code="tarball_not_available",
                retryable=False,
                next_action="npm.get_version",
            )
        response = self._response(
            "GET",
            url,
            action="download_tarball",
            arguments={"package": package, "version": version},
            accept="application/octet-stream",
            cache_ttl=300,
        )
        if len(response.body) > 50 * 1024 * 1024:
            raise AgentWebError(
                "The compressed package exceeds AgentWeb's 50 MB inspection limit",
                code="tarball_too_large",
                retryable=False,
            )
        return record, response.body

    @staticmethod
    def _members(body: bytes) -> list[tarfile.TarInfo]:
        try:
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
                members = archive.getmembers()
        except (tarfile.TarError, OSError) as exc:
            raise AgentWebError(
                "npm returned a malformed package tarball",
                code="invalid_tarball",
                retryable=True,
            ) from exc
        if len(members) > 20000:
            raise AgentWebError(
                "The package contains more than AgentWeb's 20,000-file inspection limit",
                code="tarball_too_large",
                retryable=False,
            )
        return members

    @staticmethod
    def _member_name(value: str) -> str:
        name = value.removeprefix("./").removeprefix("package/")
        path = PurePosixPath(name)
        if not name or path.is_absolute() or ".." in path.parts:
            raise AgentWebError(
                "The tarball contains an unsafe path",
                code="unsafe_tarball_path",
                retryable=False,
            )
        return str(path)

    def list_package_files(
        self,
        package: str,
        version: str = "latest",
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        package = self._package(package)
        version = self._version(version)
        record, body = self._tarball(package, version)
        files = []
        for member in self._members(body):
            name = self._member_name(member.name)
            files.append(
                {
                    "path": name,
                    "bytes": member.size,
                    "type": "file" if member.isfile() else "directory" if member.isdir() else "link" if member.issym() or member.islnk() else "other",
                    "mode": oct(member.mode),
                }
            )
        files.sort(key=lambda item: str(item["path"]))
        page, pagination = self._page(files, cursor=cursor, limit=limit)
        return self._result(
            "list_package_files",
            {
                "package": record.get("name") or package,
                "version": record.get("version") or version,
                "compressed_bytes": len(body),
                "files": page,
            },
            pagination=pagination,
        )

    def read_package_file(
        self,
        package: str,
        path: str,
        version: str = "latest",
        max_bytes: int = 200000,
    ) -> dict[str, Any]:
        package = self._package(package)
        version = self._version(version)
        requested = self._member_name(path)
        record, body = self._tarball(package, version)
        try:
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
                matched = next(
                    (
                        member
                        for member in archive.getmembers()
                        if self._member_name(member.name) == requested
                    ),
                    None,
                )
                if not matched or not matched.isfile():
                    raise AgentWebError(
                        "The requested regular file is not present in this package version",
                        code="package_file_not_found",
                        field="path",
                        retryable=False,
                        next_action="npm.list_package_files",
                    )
                if matched.size > max_bytes:
                    raise AgentWebError(
                        "The requested package file exceeds the selected byte limit",
                        code="package_file_too_large",
                        field="max_bytes",
                        retryable=False,
                        next_action="npm.download_tarball",
                    )
                source = archive.extractfile(matched)
                content = source.read(max_bytes + 1) if source else b""
        except AgentWebError:
            raise
        except (tarfile.TarError, OSError) as exc:
            raise AgentWebError(
                "npm returned a malformed package tarball",
                code="invalid_tarball",
                retryable=True,
            ) from exc
        try:
            rendered = content.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            rendered = base64.b64encode(content).decode("ascii")
            encoding = "base64"
        return self._result(
            "read_package_file",
            {
                "package": record.get("name") or package,
                "version": record.get("version") or version,
                "path": requested,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "encoding": encoding,
                "content": rendered,
            },
        )

    def download_tarball(self, package: str, version: str = "latest") -> dict[str, Any]:
        package = self._package(package)
        version = self._version(version)
        record = self.get_version(package, version)["data"]
        resolved = str(record.get("version") or version)
        url = str((record.get("dist") or {}).get("tarball") or "")
        if not url:
            raise AgentWebError(
                "This package version does not publish a tarball URL",
                code="tarball_not_available",
                retryable=False,
            )
        filename = f"{package.replace('/', '-').replace('@', '')}-{resolved}.tgz"
        result = self.direct_workflow(
            [{"name": "tarball", "path": url, "response_mode": "download", "filename": filename}],
            variables={},
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else result.get("result")
        return self._result("download_tarball", data or {}, transport="installed_flow_capsule")

    def read_site_resource(self, url: str, max_chars: int = 30000) -> dict[str, Any]:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(
            host == domain or host.endswith("." + domain)
            for domain in ("npmjs.com", "docs.npmjs.com")
        ):
            raise AgentWebError(
                "URL must be an HTTPS npmjs.com or docs.npmjs.com page",
                code="invalid_resource_url",
                field="url",
                retryable=False,
            )
        response = self._response(
            "GET",
            url,
            action="read_site_resource",
            arguments={"url": url, "max_chars": max_chars},
            accept="text/html,application/xhtml+xml",
            impersonate=host.endswith("npmjs.com") and host != "docs.npmjs.com",
            cache_ttl=300,
        )
        parser = _NpmHTML()
        parser.feed(response.body.decode("utf-8", "replace"))
        text = "\n".join(parser.text)
        links = []
        for link in parser.links:
            if link["href"] and link not in links:
                links.append(link)
            if len(links) >= 100:
                break
        return self._result(
            "read_site_resource",
            {
                "url": response.url,
                "text": text[:max_chars],
                "characters": len(text),
                "truncated": len(text) > max_chars,
                "links": links,
            },
        )

    def get_registry_status(self) -> dict[str, Any]:
        payload = self._json(
            "GET",
            "https://status.npmjs.org/api/v2/summary.json",
            action="get_registry_status",
            arguments={},
            cache_ttl=60,
        )
        status = payload.get("status") if isinstance(payload, dict) else {}
        components = []
        for item in payload.get("components") or [] if isinstance(payload, dict) else []:
            if isinstance(item, dict):
                components.append(
                    {
                        "name": item.get("name"),
                        "status": item.get("status"),
                        "updated_at": item.get("updated_at"),
                    }
                )
        incidents = []
        for item in payload.get("incidents") or [] if isinstance(payload, dict) else []:
            if isinstance(item, dict):
                incidents.append(
                    {
                        "name": item.get("name"),
                        "status": item.get("status"),
                        "impact": item.get("impact"),
                        "shortlink": item.get("shortlink"),
                        "updated_at": item.get("updated_at"),
                    }
                )
        return self._result(
            "get_registry_status",
            {
                "indicator": status.get("indicator") if isinstance(status, dict) else None,
                "description": status.get("description") if isinstance(status, dict) else None,
                "components": components,
                "incidents": incidents,
            },
        )
