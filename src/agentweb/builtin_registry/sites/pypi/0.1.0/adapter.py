from __future__ import annotations

import json
import re
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

from agentweb.sdk import AgentWebError, RequestRecipeAdapter

_PROJECT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._!+\-]*$")
_NORMALIZE = re.compile(r"[-_.]+")
_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
_SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"


class _PyPIHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self.text: list[str] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"script", "style", "noscript", "svg", "template"}:
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
        if tag in {"script", "style", "noscript", "svg", "template"} and self._hidden_depth:
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
    site_name = "pypi"
    base_url = "https://pypi.org"
    allowed_domains = ("pypi.org", "files.pythonhosted.org")
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
            "operation": f"pypi.{action}",
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

    @classmethod
    def _project(cls, value: str) -> str:
        candidate = value.strip()
        if len(candidate) > 214 or not _PROJECT.fullmatch(candidate):
            raise AgentWebError(
                "Project must be a PyPI name such as requests or ruamel.yaml",
                code="invalid_project_name",
                field="project",
                retryable=False,
                user_action="Use the exact project name shown on its PyPI page.",
                next_action="pypi.list_all_projects",
            )
        return _NORMALIZE.sub("-", candidate).lower()

    @staticmethod
    def _version(value: str) -> str:
        candidate = value.strip()
        if len(candidate) > 128 or not _VERSION.fullmatch(candidate):
            raise AgentWebError(
                "Version must be an exact PEP 440 release such as 2.31.0",
                code="invalid_version",
                field="version",
                retryable=False,
                user_action="Use a version returned by get_project or list_versions.",
                next_action="pypi.list_versions",
            )
        return candidate

    def _response(
        self,
        method: str,
        url: str,
        *,
        action: str,
        arguments: dict[str, Any],
        params: dict[str, Any] | None = None,
        accept: str = "application/json",
        cache_ttl: int = 300,
        allow_not_found: bool = False,
    ) -> Any:
        response = self.session().request(
            method,
            url,
            params=params,
            headers={"Accept": accept},
            cache_action=action,
            cache_arguments=arguments,
            cache_ttl=cache_ttl if method == "GET" else 0,
            allowed_redirect_domains=self.allowed_domains,
        )
        if response.status < 400:
            return response
        if response.status == 404:
            if allow_not_found:
                return response
            project = str(arguments.get("project") or "")
            raise AgentWebError(
                f"PyPI could not find {project or 'the requested resource'}",
                code="project_not_found" if project else "resource_not_found",
                field="project" if project else None,
                retryable=False,
                user_action="Check the name against list_all_projects and retry.",
                next_action="pypi.list_all_projects",
            )
        if response.status == 429:
            raise AgentWebError(
                "PyPI is rate limiting this client",
                code="pypi_rate_limited",
                retryable=True,
                retry_after_seconds=10,
                user_action="Wait at least ten seconds before retrying the same operation.",
            )
        raise AgentWebError(
            f"PyPI returned HTTP {response.status}",
            code="pypi_unavailable",
            retryable=response.status >= 500,
            user_action="Retry later if PyPI is temporarily unavailable.",
        )

    def _json(
        self,
        url: str,
        *,
        action: str,
        arguments: dict[str, Any],
        params: dict[str, Any] | None = None,
        accept: str = "application/json",
        cache_ttl: int = 300,
    ) -> dict[str, Any]:
        response = self._response(
            "GET",
            url,
            action=action,
            arguments=arguments,
            params=params,
            accept=accept,
            cache_ttl=cache_ttl,
        )
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentWebError(
                "PyPI returned malformed JSON",
                code="invalid_pypi_response",
                retryable=True,
                user_action="Retry once; check PyPI status if the error continues.",
            ) from exc
        if not isinstance(payload, dict):
            raise AgentWebError(
                "PyPI returned an unexpected JSON response",
                code="invalid_pypi_response",
                retryable=True,
                user_action="Retry once; check PyPI status if the error continues.",
            )
        return payload

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

    @staticmethod
    def _project_json_url(project: str) -> str:
        return f"https://pypi.org/pypi/{quote(project, safe='')}/json"

    def _project_payload(self, project: str, action: str) -> dict[str, Any]:
        return self._json(
            self._project_json_url(project),
            action=action,
            arguments={"project": project},
            cache_ttl=300,
        )

    @staticmethod
    def _info(payload: dict[str, Any]) -> dict[str, Any]:
        info = payload.get("info")
        return info if isinstance(info, dict) else {}

    @staticmethod
    def _clean(record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if value not in (None, "", [], {})}

    @classmethod
    def _file_record(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        digests = value.get("digests") if isinstance(value.get("digests"), dict) else {}
        return cls._clean(
            {
                "filename": value.get("filename"),
                "packagetype": value.get("packagetype"),
                "python_version": value.get("python_version"),
                "requires_python": value.get("requires_python"),
                "size": value.get("size"),
                "upload_time": value.get("upload_time_iso_8601") or value.get("upload_time"),
                "url": value.get("url"),
                "yanked": bool(value.get("yanked")),
                "yanked_reason": value.get("yanked_reason"),
                "sha256": digests.get("sha256"),
                "blake2b_256": digests.get("blake2b_256"),
                "has_core_metadata": bool(value.get("core-metadata")),
            }
        )

    # ------------------------------------------------------------------ metadata

    def get_project(self, project: str) -> dict[str, Any]:
        project = self._project(project)
        payload = self._project_payload(project, "get_project")
        info = self._info(payload)
        releases = payload.get("releases") if isinstance(payload.get("releases"), dict) else {}
        urls = payload.get("urls") if isinstance(payload.get("urls"), list) else []
        ownership = payload.get("ownership") if isinstance(payload.get("ownership"), dict) else {}
        vulns = payload.get("vulnerabilities") if isinstance(payload.get("vulnerabilities"), list) else []
        data = self._clean(
            {
                "name": info.get("name") or project,
                "version": info.get("version"),
                "summary": info.get("summary"),
                "author": info.get("author"),
                "author_email": info.get("author_email"),
                "maintainer": info.get("maintainer"),
                "license": info.get("license_expression") or info.get("license"),
                "keywords": [
                    keyword
                    for keyword in re.split(r"[,\s]+", str(info.get("keywords") or ""))
                    if keyword
                ],
                "classifiers": info.get("classifiers") or [],
                "requires_python": info.get("requires_python"),
                "home_page": info.get("home_page"),
                "project_urls": info.get("project_urls") or {},
                "package_url": info.get("package_url"),
                "release_count": len(releases),
                "latest_files": [self._file_record(item) for item in urls],
                "yanked": bool(info.get("yanked")),
                "vulnerability_count": len(vulns),
                "owners": [
                    {"user": role.get("user"), "role": role.get("role")}
                    for role in ownership.get("roles") or []
                    if isinstance(role, dict)
                ],
                "last_serial": payload.get("last_serial"),
            }
        )
        return self._result("get_project", data)

    def get_release(self, project: str, version: str) -> dict[str, Any]:
        project = self._project(project)
        version = self._version(version)
        payload = self._json(
            f"https://pypi.org/pypi/{quote(project, safe='')}/{quote(version, safe='')}/json",
            action="get_release",
            arguments={"project": project, "version": version},
            cache_ttl=300,
        )
        info = self._info(payload)
        urls = payload.get("urls") if isinstance(payload.get("urls"), list) else []
        vulns = payload.get("vulnerabilities") if isinstance(payload.get("vulnerabilities"), list) else []
        data = self._clean(
            {
                "name": info.get("name") or project,
                "version": info.get("version") or version,
                "summary": info.get("summary"),
                "requires_python": info.get("requires_python"),
                "license": info.get("license_expression") or info.get("license"),
                "yanked": bool(info.get("yanked")),
                "yanked_reason": info.get("yanked_reason"),
                "requires_dist": info.get("requires_dist") or [],
                "files": [self._file_record(item) for item in urls],
                "file_count": len(urls),
                "vulnerability_count": len(vulns),
            }
        )
        return self._result("get_release", data)

    def _releases(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        releases = payload.get("releases") if isinstance(payload.get("releases"), dict) else {}
        records: list[dict[str, Any]] = []
        for version, files in releases.items():
            file_list = files if isinstance(files, list) else []
            upload_time = None
            yanked = False
            for item in file_list:
                if isinstance(item, dict):
                    upload_time = upload_time or item.get("upload_time_iso_8601") or item.get("upload_time")
                    yanked = yanked or bool(item.get("yanked"))
            records.append(
                {
                    "version": version,
                    "upload_time": upload_time,
                    "file_count": len(file_list),
                    "yanked": yanked,
                    "is_prerelease": bool(re.search(r"[abc]|rc|dev", str(version))),
                }
            )
        records.sort(key=lambda item: str(item.get("upload_time") or ""), reverse=True)
        return records

    def list_releases(
        self,
        project: str,
        cursor: str | None = None,
        limit: int = 50,
        include_prerelease: bool = True,
    ) -> dict[str, Any]:
        project = self._project(project)
        payload = self._project_payload(project, "list_releases")
        records = [
            record
            for record in self._releases(payload)
            if include_prerelease or not record["is_prerelease"]
        ]
        page, pagination = self._page(records, cursor=cursor, limit=limit)
        return self._result(
            "list_releases", {"project": project, "releases": page}, pagination=pagination
        )

    def list_files(self, project: str, version: str) -> dict[str, Any]:
        result = self.get_release(project, version)
        data = result["data"]
        return self._result(
            "list_files",
            {
                "project": data.get("name"),
                "version": data.get("version"),
                "files": data.get("files") or [],
            },
        )

    def get_description(
        self, project: str, version: str | None = None, max_chars: int = 20000
    ) -> dict[str, Any]:
        project = self._project(project)
        if version is not None:
            version = self._version(version)
            result = self.get_release(project, version)
            payload = self._json(
                f"https://pypi.org/pypi/{quote(project, safe='')}/{quote(version, safe='')}/json",
                action="get_description",
                arguments={"project": project, "version": version},
                cache_ttl=300,
            )
        else:
            payload = self._project_payload(project, "get_description")
        info = self._info(payload)
        description = str(info.get("description") or "")
        return self._result(
            "get_description",
            {
                "project": info.get("name") or project,
                "version": info.get("version"),
                "content_type": info.get("description_content_type") or "text/x-rst",
                "content": description[:max_chars],
                "characters": len(description),
                "truncated": len(description) > max_chars,
            },
            warnings=["This release does not publish a long description."]
            if not description
            else [],
        )

    def list_dependencies(
        self,
        project: str,
        version: str | None = None,
        extra: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        project = self._project(project)
        if version is not None:
            version = self._version(version)
            payload = self._json(
                f"https://pypi.org/pypi/{quote(project, safe='')}/{quote(version, safe='')}/json",
                action="list_dependencies",
                arguments={"project": project, "version": version},
                cache_ttl=300,
            )
        else:
            payload = self._project_payload(project, "list_dependencies")
        info = self._info(payload)
        requirements = info.get("requires_dist") if isinstance(info.get("requires_dist"), list) else []
        items: list[dict[str, Any]] = []
        for requirement in requirements:
            if not isinstance(requirement, str):
                continue
            marker = None
            spec = requirement
            if ";" in requirement:
                spec, marker = (part.strip() for part in requirement.split(";", 1))
            match = _REQUIREMENT_NAME.match(spec)
            if not match:
                continue
            requirement_extra = None
            if marker:
                extra_match = re.search(r"extra\s*==\s*['\"]([^'\"]+)['\"]", marker)
                requirement_extra = extra_match.group(1) if extra_match else None
            if extra is not None and requirement_extra != extra:
                continue
            if extra is None and requirement_extra is not None:
                continue
            items.append(
                self._clean(
                    {
                        "name": match.group(1),
                        "specifier": spec[match.end():].strip(),
                        "extra": requirement_extra,
                        "marker": marker,
                        "raw": requirement,
                    }
                )
            )
        items.sort(key=lambda item: str(item["name"]).lower())
        page, pagination = self._page(items, cursor=cursor, limit=limit)
        return self._result(
            "list_dependencies",
            {
                "project": info.get("name") or project,
                "version": info.get("version"),
                "requires_python": info.get("requires_python"),
                "dependencies": page,
            },
            pagination=pagination,
        )

    def get_vulnerabilities(
        self, project: str, version: str | None = None
    ) -> dict[str, Any]:
        project = self._project(project)
        if version is not None:
            version = self._version(version)
            payload = self._json(
                f"https://pypi.org/pypi/{quote(project, safe='')}/{quote(version, safe='')}/json",
                action="get_vulnerabilities",
                arguments={"project": project, "version": version},
                cache_ttl=300,
            )
        else:
            payload = self._project_payload(project, "get_vulnerabilities")
        info = self._info(payload)
        raw = payload.get("vulnerabilities") if isinstance(payload.get("vulnerabilities"), list) else []
        advisories = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            advisories.append(
                self._clean(
                    {
                        "id": item.get("id"),
                        "aliases": item.get("aliases") or [],
                        "summary": item.get("summary"),
                        "details": str(item.get("details") or "")[:2000],
                        "fixed_in": item.get("fixed_in") or [],
                        "link": item.get("link"),
                        "source": item.get("source"),
                        "withdrawn": item.get("withdrawn"),
                    }
                )
            )
        return self._result(
            "get_vulnerabilities",
            {
                "project": info.get("name") or project,
                "version": info.get("version"),
                "advisories": advisories,
                "advisory_count": len(advisories),
            },
            warnings=["No known vulnerabilities are recorded for this release."]
            if not advisories
            else [],
        )

    def get_ownership(self, project: str) -> dict[str, Any]:
        project = self._project(project)
        payload = self._project_payload(project, "get_ownership")
        info = self._info(payload)
        ownership = payload.get("ownership") if isinstance(payload.get("ownership"), dict) else {}
        roles = [
            {"user": role.get("user"), "role": role.get("role")}
            for role in ownership.get("roles") or []
            if isinstance(role, dict)
        ]
        return self._result(
            "get_ownership",
            {
                "project": info.get("name") or project,
                "organization": ownership.get("organization"),
                "owners": roles,
                "owner_count": len(roles),
            },
        )

    # ------------------------------------------------------------------ index

    def list_versions(
        self,
        project: str,
        cursor: str | None = None,
        limit: int = 100,
        include_prerelease: bool = True,
    ) -> dict[str, Any]:
        project = self._project(project)
        payload = self._json(
            f"https://pypi.org/simple/{quote(project, safe='')}/",
            action="list_versions",
            arguments={"project": project},
            accept=_SIMPLE_JSON,
            cache_ttl=300,
        )
        versions = payload.get("versions") if isinstance(payload.get("versions"), list) else []
        selected = [
            version
            for version in versions
            if isinstance(version, str)
            and (include_prerelease or not re.search(r"[abc]|rc|dev", version))
        ]
        selected.reverse()
        status = payload.get("project-status") if isinstance(payload.get("project-status"), dict) else {}
        page, pagination = self._page(selected, cursor=cursor, limit=limit)
        return self._result(
            "list_versions",
            {
                "project": payload.get("name") or project,
                "status": status.get("status"),
                "versions": page,
            },
            pagination=pagination,
        )

    def list_all_projects(
        self, prefix: str | None = None, cursor: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        payload = self._json(
            "https://pypi.org/simple/",
            action="list_all_projects",
            arguments={},
            accept=_SIMPLE_JSON,
            cache_ttl=3600,
        )
        projects = payload.get("projects") if isinstance(payload.get("projects"), list) else []
        names = [
            str(item.get("name"))
            for item in projects
            if isinstance(item, dict) and item.get("name")
        ]
        if prefix:
            normalized = _NORMALIZE.sub("-", prefix.strip()).lower()
            names = [name for name in names if _NORMALIZE.sub("-", name).lower().startswith(normalized)]
        if limit > 500:
            limit = 500
        page, pagination = self._page(names, cursor=cursor, limit=limit)
        return self._result(
            "list_all_projects",
            {"prefix": prefix, "projects": page},
            pagination=pagination,
        )

    # ------------------------------------------------------------------ integrity

    def get_provenance(
        self, project: str, version: str, filename: str
    ) -> dict[str, Any]:
        project = self._project(project)
        version = self._version(version)
        safe_filename = filename.strip()
        if not safe_filename or "/" in safe_filename or ".." in safe_filename:
            raise AgentWebError(
                "filename must be an exact distribution filename from list_files",
                code="invalid_filename",
                field="filename",
                retryable=False,
                next_action="pypi.list_files",
            )
        response = self._response(
            "GET",
            f"https://pypi.org/integrity/{quote(project, safe='')}/{quote(version, safe='')}/{quote(safe_filename, safe='')}/provenance",
            action="get_provenance",
            arguments={"project": project, "version": version, "filename": safe_filename},
            cache_ttl=300,
            allow_not_found=True,
        )
        if response.status == 404:
            return self._result(
                "get_provenance",
                {
                    "project": project,
                    "version": version,
                    "filename": safe_filename,
                    "attestation_bundles": [],
                    "has_provenance": False,
                },
                warnings=["This distribution file does not publish PEP 740 provenance."],
            )
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentWebError(
                "PyPI returned malformed provenance JSON",
                code="invalid_pypi_response",
                retryable=True,
            ) from exc
        bundles = payload.get("attestation_bundles") if isinstance(payload, dict) else []
        summary = []
        for bundle in bundles if isinstance(bundles, list) else []:
            if not isinstance(bundle, dict):
                continue
            publisher = bundle.get("publisher") if isinstance(bundle.get("publisher"), dict) else {}
            attestations = bundle.get("attestations") if isinstance(bundle.get("attestations"), list) else []
            summary.append(
                self._clean(
                    {
                        "publisher_kind": publisher.get("kind"),
                        "repository": publisher.get("repository"),
                        "workflow": publisher.get("workflow"),
                        "attestation_count": len(attestations),
                    }
                )
            )
        return self._result(
            "get_provenance",
            {
                "project": project,
                "version": version,
                "filename": safe_filename,
                "attestation_bundles": summary,
                "has_provenance": bool(summary),
            },
        )

    def get_core_metadata(
        self, project: str, version: str, filename: str, max_bytes: int = 200000
    ) -> dict[str, Any]:
        project = self._project(project)
        version = self._version(version)
        files = self.get_release(project, version)["data"].get("files") or []
        matched = next(
            (item for item in files if isinstance(item, dict) and item.get("filename") == filename.strip()),
            None,
        )
        if matched is None:
            raise AgentWebError(
                "filename must be an exact distribution filename from list_files",
                code="invalid_filename",
                field="filename",
                retryable=False,
                next_action="pypi.list_files",
            )
        if not matched.get("has_core_metadata"):
            raise AgentWebError(
                "This distribution file does not publish PEP 658 core metadata",
                code="core_metadata_unavailable",
                retryable=False,
                next_action="pypi.download_distribution",
            )
        response = self._response(
            "GET",
            f"{matched['url']}.metadata",
            action="get_core_metadata",
            arguments={"project": project, "version": version, "filename": filename},
            accept="text/plain",
            cache_ttl=300,
        )
        body = response.body[:max_bytes]
        text = body.decode("utf-8", "replace")
        return self._result(
            "get_core_metadata",
            {
                "project": project,
                "version": version,
                "filename": matched.get("filename"),
                "content": text,
                "bytes": len(response.body),
                "truncated": len(response.body) > max_bytes,
            },
        )

    def download_distribution(
        self, project: str, version: str, filename: str
    ) -> dict[str, Any]:
        project = self._project(project)
        version = self._version(version)
        files = self.get_release(project, version)["data"].get("files") or []
        matched = next(
            (item for item in files if isinstance(item, dict) and item.get("filename") == filename.strip()),
            None,
        )
        if matched is None:
            raise AgentWebError(
                "filename must be an exact distribution filename from list_files",
                code="invalid_filename",
                field="filename",
                retryable=False,
                next_action="pypi.list_files",
            )
        result = self.direct_workflow(
            [
                {
                    "name": "distribution",
                    "path": str(matched["url"]),
                    "response_mode": "download",
                    "filename": str(matched["filename"]),
                }
            ],
            variables={},
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else result.get("result")
        return self._result(
            "download_distribution",
            data or {},
            transport="installed_flow_capsule",
        )

    # ------------------------------------------------------------------ feeds

    def _rss_items(
        self, url: str, *, action: str, arguments: dict[str, Any]
    ) -> list[dict[str, Any]]:
        response = self._response(
            "GET",
            url,
            action=action,
            arguments=arguments,
            accept="application/rss+xml,application/xml",
            cache_ttl=120,
        )
        try:
            root = ElementTree.fromstring(response.body)
        except ElementTree.ParseError as exc:
            raise AgentWebError(
                "PyPI returned a malformed RSS feed",
                code="invalid_pypi_response",
                retryable=True,
            ) from exc
        items: list[dict[str, Any]] = []
        for item in root.iter("item"):
            record: dict[str, Any] = {}
            for child in item:
                text = (child.text or "").strip()
                if child.tag == "title":
                    record["title"] = text
                elif child.tag == "link":
                    record["link"] = text
                elif child.tag == "description":
                    record["description"] = text[:500]
                elif child.tag == "author":
                    record["author"] = text
                elif child.tag == "pubDate":
                    record["published"] = text
                    try:
                        record["published_iso"] = parsedate_to_datetime(text).isoformat()
                    except (TypeError, ValueError):
                        pass
            items.append(self._clean(record))
        return items

    def list_recent_packages(
        self, cursor: str | None = None, limit: int = 40
    ) -> dict[str, Any]:
        items = self._rss_items(
            "https://pypi.org/rss/packages.xml",
            action="list_recent_packages",
            arguments={},
        )
        page, pagination = self._page(items, cursor=cursor, limit=limit)
        return self._result(
            "list_recent_packages", {"packages": page}, pagination=pagination
        )

    def list_recent_updates(
        self, cursor: str | None = None, limit: int = 40
    ) -> dict[str, Any]:
        items = self._rss_items(
            "https://pypi.org/rss/updates.xml",
            action="list_recent_updates",
            arguments={},
        )
        page, pagination = self._page(items, cursor=cursor, limit=limit)
        return self._result(
            "list_recent_updates", {"updates": page}, pagination=pagination
        )

    def get_project_release_feed(
        self, project: str, cursor: str | None = None, limit: int = 40
    ) -> dict[str, Any]:
        project = self._project(project)
        items = self._rss_items(
            f"https://pypi.org/rss/project/{quote(project, safe='')}/releases.xml",
            action="get_project_release_feed",
            arguments={"project": project},
        )
        page, pagination = self._page(items, cursor=cursor, limit=limit)
        return self._result(
            "get_project_release_feed",
            {"project": project, "releases": page},
            pagination=pagination,
        )

    # ------------------------------------------------------------------ misc

    def get_stats(self, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        payload = self._json(
            "https://pypi.org/stats/",
            action="get_stats",
            arguments={},
            accept="application/json",
            cache_ttl=3600,
        )
        top = payload.get("top_packages") if isinstance(payload.get("top_packages"), dict) else {}
        records = [
            {"project": name, "size": value.get("size") if isinstance(value, dict) else None}
            for name, value in top.items()
        ]
        records.sort(key=lambda item: int(item["size"] or 0), reverse=True)
        page, pagination = self._page(records, cursor=cursor, limit=limit)
        return self._result(
            "get_stats",
            {
                "total_packages_size": payload.get("total_packages_size"),
                "top_packages": page,
            },
            pagination=pagination,
        )

    def read_site_resource(self, url: str, max_chars: int = 30000) -> dict[str, Any]:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "pypi.org" or host.endswith(".pypi.org")):
            raise AgentWebError(
                "URL must be an HTTPS pypi.org page",
                code="invalid_resource_url",
                field="url",
                retryable=False,
            )
        if parsed.path.rstrip("/") in {"/search"} or parsed.path.startswith("/search"):
            raise AgentWebError(
                "PyPI's web search is protected by a browser challenge and cannot be read over HTTP; use list_all_projects for programmatic discovery",
                code="search_requires_browser",
                field="url",
                retryable=False,
                next_action="pypi.list_all_projects",
            )
        response = self._response(
            "GET",
            url,
            action="read_site_resource",
            arguments={"url": url, "max_chars": max_chars},
            accept="text/html,application/xhtml+xml",
            cache_ttl=300,
        )
        parser = _PyPIHTML()
        parser.feed(response.body.decode("utf-8", "replace"))
        text = "\n".join(parser.text)
        links: list[dict[str, str]] = []
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
