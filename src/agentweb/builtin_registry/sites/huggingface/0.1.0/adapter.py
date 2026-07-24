from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from agentweb.sdk import AgentWebError, RequestRecipeAdapter


_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,199}$")
_PAPER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_REPO_PREFIX = {"model": "models", "dataset": "datasets", "space": "spaces"}


class Adapter(RequestRecipeAdapter):
    """Browserless, read-only access to Hugging Face's public Hub surfaces."""

    site_name = "huggingface"
    base_url = "https://huggingface.co"
    allowed_domains = ("huggingface.co", "hf.co", "datasets-server.huggingface.co")
    recipes = {"home": {"method": "GET", "path": "/", "cache_ttl": 60}}

    @staticmethod
    def _result(
        action: str,
        data: dict[str, Any],
        *,
        pagination: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "operation": f"huggingface.{action}",
            "data": data,
            "state_change": {"changed": False, "reversible": False, "idempotent": True},
            "pagination": pagination or {"supported": False},
            "warnings": warnings or [],
            "verification": {
                "verified": True,
                "reviewed": True,
                "transport": "typed_adapter",
            },
        }

    @staticmethod
    def _header(headers: dict[str, str], name: str) -> str | None:
        return next(
            (value for key, value in headers.items() if key.lower() == name.lower()),
            None,
        )

    @staticmethod
    def _repo_id(value: str) -> str:
        candidate = value.strip()
        if len(candidate) > 200 or not _REPO_ID.fullmatch(candidate):
            raise AgentWebError(
                "Repository must be namespace/name, for example google-bert/bert-base-uncased",
                code="invalid_repository",
                field="repo_id",
                retryable=False,
                user_action="Use an exact id returned by a Hugging Face search or list operation.",
                next_action="huggingface.hub_search",
            )
        return candidate

    @staticmethod
    def _name(value: str, field: str = "username") -> str:
        candidate = value.strip()
        if len(candidate) > 100 or not _NAME.fullmatch(candidate):
            raise AgentWebError(
                f"{field} is not a valid Hugging Face name",
                code="invalid_argument",
                field=field,
            )
        return candidate

    @staticmethod
    def _revision(value: str) -> str:
        candidate = value.strip()
        if not _REVISION.fullmatch(candidate) or ".." in candidate:
            raise AgentWebError(
                "Revision is invalid", code="invalid_revision", field="revision"
            )
        return candidate

    @staticmethod
    def _path(value: str, *, allow_empty: bool = True) -> str:
        candidate = value.strip().strip("/")
        if (not candidate and not allow_empty) or len(candidate) > 1000:
            raise AgentWebError(
                "Repository path is invalid", code="invalid_path", field="path"
            )
        if any(part in {"", ".", ".."} for part in candidate.split("/")) and candidate:
            raise AgentWebError(
                "Repository path cannot contain empty, dot, or parent components",
                code="invalid_path",
                field="path",
            )
        return candidate

    @staticmethod
    def _limit(value: int, maximum: int = 100) -> int:
        if isinstance(value, bool) or value < 1 or value > maximum:
            raise AgentWebError(
                f"limit must be between 1 and {maximum}",
                code="invalid_argument",
                field="limit",
            )
        return value

    @classmethod
    def _bounded(
        cls, value: Any, *, max_items: int = 100, max_string: int = 30_000
    ) -> Any:
        if isinstance(value, list):
            return [
                cls._bounded(item, max_items=max_items, max_string=max_string)
                for item in value[:max_items]
            ]
        if isinstance(value, dict):
            return {
                str(key): cls._bounded(item, max_items=max_items, max_string=max_string)
                for key, item in value.items()
            }
        if isinstance(value, str):
            return value[:max_string]
        return value

    @classmethod
    def _next_cursor(cls, headers: dict[str, str]) -> str | None:
        link = cls._header(headers, "link") or ""
        for part in link.split(","):
            if 'rel="next"' not in part and "rel=next" not in part:
                continue
            matched = re.search(r"<([^>]+)>", part)
            if not matched:
                continue
            values = parse_qs(urlparse(matched.group(1)).query)
            cursor = (
                values.get("cursor") or values.get("p") or values.get("page") or [None]
            )[0]
            if cursor:
                return str(cursor)
        return None

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
        cache_ttl: int = 60,
    ):
        arguments = {key: value for key, value in arguments.items() if key != "self"}
        response = self.session().request(
            method,
            url,
            params={
                key: value for key, value in (params or {}).items() if value is not None
            },
            json_body=json_body,
            headers={"Accept": accept},
            cache_action=action,
            cache_arguments=arguments,
            cache_ttl=cache_ttl if method in {"GET", "HEAD"} else 0,
            allowed_redirect_domains=self.allowed_domains,
        )
        if response.status < 400:
            return response
        message = self._header(response.headers, "x-error-message")
        if not message:
            try:
                error = json.loads(response.body)
                message = (
                    str(error.get("error") or error.get("message") or "")
                    if isinstance(error, dict)
                    else ""
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = ""
        if response.status == 429:
            retry = self._header(response.headers, "retry-after")
            raise AgentWebError(
                "Hugging Face is rate limiting this client",
                code="rate_limited",
                retryable=True,
                retry_after_seconds=int(retry) if retry and retry.isdigit() else 60,
                user_action="Wait for the reported rate-limit window before retrying.",
            )
        if response.status in {401, 403}:
            gated = "gated" in message.lower() or "access" in message.lower()
            raise AgentWebError(
                message
                or "This resource is private, gated, or requires authentication",
                code="gated_access_required" if gated else "authentication_required",
                retryable=False,
                user_action="Open the resource as the user to request access or authenticate; public AgentWeb calls cannot bypass access controls.",
            )
        if response.status == 404:
            raise AgentWebError(
                message
                or "Hugging Face could not find this public resource; private repositories may also appear missing",
                code="resource_not_found_or_private",
                retryable=False,
                user_action="Check the exact id, revision, and path with the matching list or tree operation.",
            )
        raise AgentWebError(
            message or f"Hugging Face returned HTTP {response.status}",
            code="upstream_unavailable"
            if response.status >= 500
            else "invalid_request",
            retryable=response.status >= 500,
            details={"http_status": response.status},
        )

    def _json_call(
        self,
        action: str,
        url: str,
        arguments: dict[str, Any],
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        max_items: int = 100,
        cache_ttl: int = 60,
    ) -> dict[str, Any]:
        # Callers commonly pass locals(); keep the adapter instance out of cache keys
        # and public pagination metadata.
        arguments = {key: value for key, value in arguments.items() if key != "self"}
        response = self._response(
            method,
            url,
            action=action,
            arguments=arguments,
            params=params,
            json_body=json_body,
            cache_ttl=cache_ttl,
        )
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentWebError(
                "Hugging Face returned malformed JSON",
                code="invalid_upstream_response",
                retryable=True,
            ) from exc
        bounded = self._bounded(payload, max_items=max_items)
        if isinstance(payload, list):
            data = {
                "items": bounded,
                "returned": min(len(payload), max_items),
                "upstream_returned": len(payload),
            }
            truncated = len(payload) > max_items
        else:
            data = {"resource": bounded}
            truncated = False
        next_cursor = self._next_cursor(response.headers)
        return self._result(
            action,
            data,
            pagination={
                "supported": next_cursor is not None or "cursor" in arguments,
                "cursor": arguments.get("cursor"),
                "next_cursor": next_cursor,
                "limit": arguments.get("limit"),
                "returned": data.get("returned"),
            },
            warnings=[
                "The upstream response exceeded the requested local bound and was truncated."
            ]
            if truncated
            else [],
        )

    def _repo_url(self, repo_type: str, repo_id: str, suffix: str = "") -> str:
        if repo_type not in _REPO_PREFIX:
            raise AgentWebError(
                "repo_type must be model, dataset, or space",
                code="invalid_argument",
                field="repo_type",
            )
        repo_id = self._repo_id(repo_id)
        return f"https://huggingface.co/api/{_REPO_PREFIX[repo_type]}/{quote(repo_id, safe='/')}{suffix}"

    def hub_search(self, query: str, limit: int = 20) -> dict[str, Any]:
        limit = self._limit(limit, 50)
        return self._json_call(
            "hub_search",
            "https://huggingface.co/api/quicksearch",
            locals(),
            params={"q": query[:250], "limit": limit},
            max_items=limit,
        )

    def hub_trending(self, limit: int = 20) -> dict[str, Any]:
        limit = self._limit(limit, 100)
        return self._json_call(
            "hub_trending",
            "https://huggingface.co/api/trending",
            locals(),
            params={"limit": limit},
            max_items=limit,
        )

    def _list_repos(
        self,
        action: str,
        repo_type: str,
        query: str | None,
        author: str | None,
        filter: str | None,
        sort: str,
        direction: int,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        limit = self._limit(limit)
        return self._json_call(
            action,
            f"https://huggingface.co/api/{_REPO_PREFIX[repo_type]}",
            locals(),
            params={
                "search": query,
                "author": author,
                "filter": filter,
                "sort": sort,
                "direction": direction,
                "limit": limit,
                "cursor": cursor,
                "full": True,
            },
            max_items=limit,
        )

    def models_list(
        self,
        query: str | None = None,
        author: str | None = None,
        filter: str | None = None,
        sort: str = "trendingScore",
        direction: int = -1,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._list_repos(
            "models_list",
            "model",
            query,
            author,
            filter,
            sort,
            direction,
            limit,
            cursor,
        )

    def datasets_list(
        self,
        query: str | None = None,
        author: str | None = None,
        filter: str | None = None,
        sort: str = "trendingScore",
        direction: int = -1,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._list_repos(
            "datasets_list",
            "dataset",
            query,
            author,
            filter,
            sort,
            direction,
            limit,
            cursor,
        )

    def spaces_list(
        self,
        query: str | None = None,
        author: str | None = None,
        filter: str | None = None,
        sort: str = "trendingScore",
        direction: int = -1,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._list_repos(
            "spaces_list",
            "space",
            query,
            author,
            filter,
            sort,
            direction,
            limit,
            cursor,
        )

    def spaces_semantic_search(self, query: str, limit: int = 20) -> dict[str, Any]:
        limit = self._limit(limit, 50)
        return self._json_call(
            "spaces_semantic_search",
            "https://huggingface.co/api/spaces/semantic-search",
            locals(),
            params={"q": query[:250], "sdk": "gradio", "limit": limit},
            max_items=limit,
        )

    def model_facets(self) -> dict[str, Any]:
        return self._json_call(
            "model_facets",
            "https://huggingface.co/api/models-tags-by-type",
            {},
            cache_ttl=3600,
        )

    def dataset_facets(self) -> dict[str, Any]:
        return self._json_call(
            "dataset_facets",
            "https://huggingface.co/api/datasets-tags-by-type",
            {},
            cache_ttl=3600,
        )

    def tasks_list(self) -> dict[str, Any]:
        return self._json_call(
            "tasks_list", "https://huggingface.co/api/tasks", {}, cache_ttl=3600
        )

    def agent_harnesses_list(self) -> dict[str, Any]:
        return self._json_call(
            "agent_harnesses_list",
            "https://huggingface.co/api/agent-harnesses",
            {},
            cache_ttl=3600,
        )

    def model_get(self, repo_id: str) -> dict[str, Any]:
        return self._json_call(
            "model_get", self._repo_url("model", repo_id), locals(), cache_ttl=300
        )

    def dataset_get(self, repo_id: str) -> dict[str, Any]:
        return self._json_call(
            "dataset_get", self._repo_url("dataset", repo_id), locals(), cache_ttl=300
        )

    def space_get(self, repo_id: str) -> dict[str, Any]:
        return self._json_call(
            "space_get", self._repo_url("space", repo_id), locals(), cache_ttl=300
        )

    def space_runtime_get(self, repo_id: str) -> dict[str, Any]:
        return self._json_call(
            "space_runtime_get", self._repo_url("space", repo_id, "/runtime"), locals()
        )

    def model_security_get(self, repo_id: str) -> dict[str, Any]:
        return self._json_call(
            "model_security_get",
            self._repo_url("model", repo_id, "/scan"),
            locals(),
            cache_ttl=300,
        )

    def dataset_leaderboard_get(self, repo_id: str) -> dict[str, Any]:
        return self._json_call(
            "dataset_leaderboard_get",
            self._repo_url("dataset", repo_id, "/leaderboard"),
            locals(),
            cache_ttl=300,
        )

    def repo_tree_list(
        self,
        repo_type: str,
        repo_id: str,
        revision: str = "main",
        path: str = "",
        recursive: bool = False,
        expand: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        revision, path, limit = (
            self._revision(revision),
            self._path(path),
            self._limit(limit),
        )
        suffix = f"/tree/{quote(revision, safe='')}/{quote(path, safe='/')}"
        return self._json_call(
            "repo_tree_list",
            self._repo_url(repo_type, repo_id, suffix),
            locals(),
            params={
                "recursive": recursive,
                "expand": expand,
                "limit": limit,
                "cursor": cursor,
            },
            max_items=limit,
            cache_ttl=300,
        )

    def repo_paths_get(
        self,
        repo_type: str,
        repo_id: str,
        paths: list[str],
        revision: str = "main",
        expand: bool = False,
    ) -> dict[str, Any]:
        revision = self._revision(revision)
        if not paths or len(paths) > 200:
            raise AgentWebError(
                "paths must contain 1 to 200 repository paths",
                code="invalid_argument",
                field="paths",
            )
        clean = [self._path(path, allow_empty=False) for path in paths]
        return self._json_call(
            "repo_paths_get",
            self._repo_url(
                repo_type, repo_id, f"/paths-info/{quote(revision, safe='')}"
            ),
            locals(),
            method="POST",
            json_body={"paths": clean, "expand": expand},
            max_items=200,
        )

    def repo_commits_list(
        self,
        repo_type: str,
        repo_id: str,
        revision: str = "main",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        revision, limit = self._revision(revision), self._limit(limit, 100)
        return self._json_call(
            "repo_commits_list",
            self._repo_url(repo_type, repo_id, f"/commits/{quote(revision, safe='')}"),
            locals(),
            params={"limit": limit, "cursor": cursor},
            max_items=limit,
            cache_ttl=300,
        )

    def repo_refs_list(self, repo_type: str, repo_id: str) -> dict[str, Any]:
        return self._json_call(
            "repo_refs_list",
            self._repo_url(repo_type, repo_id, "/refs"),
            locals(),
            cache_ttl=300,
        )

    def repo_compare(
        self, repo_type: str, repo_id: str, base: str, head: str
    ) -> dict[str, Any]:
        base, head = self._revision(base), self._revision(head)
        response = self._response(
            "GET",
            self._repo_url(
                repo_type, repo_id, f"/compare/{quote(base + '..' + head, safe='.')}"
            ),
            action="repo_compare",
            arguments=locals(),
            accept="text/plain",
            cache_ttl=300,
        )
        diff = response.text
        return self._result(
            "repo_compare",
            {
                "repo_type": repo_type,
                "repo_id": repo_id,
                "base": base,
                "head": head,
                "diff": diff[:200_000],
                "characters": len(diff),
                "truncated": len(diff) > 200_000,
            },
        )

    def repo_folder_size(
        self, repo_type: str, repo_id: str, revision: str = "main", path: str = ""
    ) -> dict[str, Any]:
        revision, path = self._revision(revision), self._path(path)
        return self._json_call(
            "repo_folder_size",
            self._repo_url(
                repo_type,
                repo_id,
                f"/treesize/{quote(revision, safe='')}/{quote(path, safe='/')}",
            ),
            locals(),
            cache_ttl=300,
        )

    def _resolve_url(
        self, repo_type: str, repo_id: str, revision: str, path: str
    ) -> str:
        repo_id, revision, path = (
            self._repo_id(repo_id),
            self._revision(revision),
            self._path(path, allow_empty=False),
        )
        prefix = "" if repo_type == "model" else f"/{_REPO_PREFIX.get(repo_type, '')}"
        if repo_type not in _REPO_PREFIX:
            raise AgentWebError(
                "repo_type must be model, dataset, or space",
                code="invalid_argument",
                field="repo_type",
            )
        return f"https://huggingface.co{prefix}/{quote(repo_id, safe='/')}/resolve/{quote(revision, safe='')}/{quote(path, safe='/')}"

    def repo_file_metadata(
        self, repo_type: str, repo_id: str, path: str, revision: str = "main"
    ) -> dict[str, Any]:
        response = self._response(
            "HEAD",
            self._resolve_url(repo_type, repo_id, revision, path),
            action="repo_file_metadata",
            arguments=locals(),
            cache_ttl=300,
        )
        size = self._header(response.headers, "x-linked-size") or self._header(
            response.headers, "content-length"
        )
        return self._result(
            "repo_file_metadata",
            {
                "repo_type": repo_type,
                "repo_id": repo_id,
                "revision": revision,
                "path": path,
                "url": response.url,
                "size": int(size) if size and size.isdigit() else None,
                "etag": self._header(response.headers, "etag"),
                "content_type": self._header(response.headers, "content-type"),
            },
        )

    def repo_text_file_read(
        self,
        repo_type: str,
        repo_id: str,
        path: str,
        revision: str = "main",
        max_bytes: int = 200_000,
    ) -> dict[str, Any]:
        if max_bytes < 1 or max_bytes > 1_000_000:
            raise AgentWebError(
                "max_bytes must be between 1 and 1000000",
                code="invalid_argument",
                field="max_bytes",
            )
        url = self._resolve_url(repo_type, repo_id, revision, path)
        metadata = self.repo_file_metadata(repo_type, repo_id, path, revision)["data"]
        size = metadata.get("size")
        if isinstance(size, int) and size > max_bytes:
            raise AgentWebError(
                "Repository file exceeds max_bytes",
                code="file_too_large",
                field="max_bytes",
                next_action="huggingface.repo_file_metadata",
            )
        response = self._response(
            "GET",
            url,
            action="repo_text_file_read",
            arguments=locals(),
            accept="text/plain,*/*",
            cache_ttl=300,
        )
        if len(response.body) > max_bytes:
            raise AgentWebError(
                "Repository file exceeds max_bytes",
                code="file_too_large",
                field="max_bytes",
                next_action="huggingface.repo_file_metadata",
            )
        try:
            content = response.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AgentWebError(
                "Repository file is binary; this operation reads UTF-8 text only",
                code="binary_file",
                retryable=False,
            ) from exc
        return self._result(
            "repo_text_file_read",
            {
                "repo_type": repo_type,
                "repo_id": repo_id,
                "revision": revision,
                "path": path,
                "bytes": len(response.body),
                "sha256": hashlib.sha256(response.body).hexdigest(),
                "content": content,
            },
        )

    def _viewer(
        self,
        action: str,
        route: str,
        arguments: dict[str, Any],
        *,
        limit: int = 100,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._json_call(
            action,
            f"https://datasets-server.huggingface.co/{route}",
            arguments,
            params=params or arguments,
            max_items=limit,
            cache_ttl=120,
        )

    def dataset_viewer_status(self, repo_id: str) -> dict[str, Any]:
        return self._viewer(
            "dataset_viewer_status",
            "is-valid",
            locals(),
            params={"dataset": self._repo_id(repo_id)},
        )

    def dataset_splits_list(self, repo_id: str) -> dict[str, Any]:
        return self._viewer(
            "dataset_splits_list",
            "splits",
            locals(),
            params={"dataset": self._repo_id(repo_id)},
        )

    def dataset_info_get(
        self, repo_id: str, config: str | None = None
    ) -> dict[str, Any]:
        return self._viewer(
            "dataset_info_get",
            "info",
            locals(),
            params={"dataset": self._repo_id(repo_id), "config": config},
        )

    def dataset_preview_rows(
        self, repo_id: str, config: str, split: str
    ) -> dict[str, Any]:
        return self._viewer(
            "dataset_preview_rows",
            "first-rows",
            locals(),
            params={
                "dataset": self._repo_id(repo_id),
                "config": config,
                "split": split,
            },
        )

    def dataset_rows_get(
        self, repo_id: str, config: str, split: str, offset: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        limit = self._limit(limit, 100)
        return self._viewer(
            "dataset_rows_get",
            "rows",
            locals(),
            limit=limit,
            params={
                "dataset": self._repo_id(repo_id),
                "config": config,
                "split": split,
                "offset": offset,
                "length": limit,
            },
        )

    def dataset_rows_search(
        self,
        repo_id: str,
        config: str,
        split: str,
        query: str,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        limit = self._limit(limit, 100)
        return self._viewer(
            "dataset_rows_search",
            "search",
            locals(),
            limit=limit,
            params={
                "dataset": self._repo_id(repo_id),
                "config": config,
                "split": split,
                "query": query,
                "offset": offset,
                "length": limit,
            },
        )

    def dataset_rows_filter(
        self,
        repo_id: str,
        config: str,
        split: str,
        where: str,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        limit = self._limit(limit, 100)
        return self._viewer(
            "dataset_rows_filter",
            "filter",
            locals(),
            limit=limit,
            params={
                "dataset": self._repo_id(repo_id),
                "config": config,
                "split": split,
                "where": where,
                "offset": offset,
                "length": limit,
            },
        )

    def dataset_parquet_list(self, repo_id: str) -> dict[str, Any]:
        return self._viewer(
            "dataset_parquet_list",
            "parquet",
            locals(),
            params={"dataset": self._repo_id(repo_id)},
        )

    def dataset_size_get(self, repo_id: str) -> dict[str, Any]:
        return self._viewer(
            "dataset_size_get",
            "size",
            locals(),
            params={"dataset": self._repo_id(repo_id)},
        )

    def dataset_statistics_get(
        self, repo_id: str, config: str, split: str
    ) -> dict[str, Any]:
        return self._viewer(
            "dataset_statistics_get",
            "statistics",
            locals(),
            params={
                "dataset": self._repo_id(repo_id),
                "config": config,
                "split": split,
            },
        )

    def dataset_croissant_get(self, repo_id: str) -> dict[str, Any]:
        return self._json_call(
            "dataset_croissant_get",
            self._repo_url("dataset", repo_id, "/croissant"),
            locals(),
            cache_ttl=300,
        )

    def discussions_list(
        self,
        repo_type: str,
        repo_id: str,
        status: str = "all",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        limit = self._limit(limit, 100)
        return self._json_call(
            "discussions_list",
            self._repo_url(repo_type, repo_id, "/discussions"),
            locals(),
            params={"status": status, "limit": limit, "cursor": cursor},
            max_items=limit,
        )

    def discussion_get(
        self, repo_type: str, repo_id: str, number: int
    ) -> dict[str, Any]:
        if number < 1:
            raise AgentWebError(
                "number must be positive", code="invalid_argument", field="number"
            )
        return self._json_call(
            "discussion_get",
            self._repo_url(repo_type, repo_id, f"/discussions/{number}"),
            locals(),
        )

    def user_get(self, username: str) -> dict[str, Any]:
        username = self._name(username)
        return self._json_call(
            "user_get",
            f"https://huggingface.co/api/users/{quote(username, safe='')}/overview",
            locals(),
            cache_ttl=300,
        )

    def organization_get(self, organization: str) -> dict[str, Any]:
        organization = self._name(organization, "organization")
        return self._json_call(
            "organization_get",
            f"https://huggingface.co/api/organizations/{quote(organization, safe='')}/overview",
            locals(),
            cache_ttl=300,
        )

    def profile_connections_list(
        self,
        username: str,
        relationship: str = "followers",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        username, limit = self._name(username), self._limit(limit, 100)
        if relationship not in {"followers", "following"}:
            raise AgentWebError(
                "relationship must be followers or following",
                code="invalid_argument",
                field="relationship",
            )
        return self._json_call(
            "profile_connections_list",
            f"https://huggingface.co/api/users/{quote(username, safe='')}/{relationship}",
            locals(),
            params={"limit": max(10, limit), "cursor": cursor},
            max_items=limit,
            cache_ttl=300,
        )

    def user_likes_list(
        self, username: str, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        username, limit = self._name(username), self._limit(limit, 100)
        return self._json_call(
            "user_likes_list",
            f"https://huggingface.co/api/users/{quote(username, safe='')}/likes",
            locals(),
            params={"limit": limit, "cursor": cursor},
            max_items=limit,
            cache_ttl=300,
        )

    def collections_list(
        self,
        owner: str | None = None,
        query: str | None = None,
        item: str | None = None,
        sort: str = "trending",
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        limit = self._limit(limit, 100)
        return self._json_call(
            "collections_list",
            "https://huggingface.co/api/collections",
            locals(),
            params={
                "owner": owner,
                "q": query,
                "item": item,
                "sort": sort,
                "limit": limit,
                "cursor": cursor,
            },
            max_items=limit,
            cache_ttl=300,
        )

    def collection_get(self, collection_id: str) -> dict[str, Any]:
        value = collection_id.strip().strip("/")
        if len(value) > 300 or value.count("/") != 1 or ".." in value:
            raise AgentWebError(
                "collection_id must be the namespace/slug-id returned by collections_list",
                code="invalid_argument",
                field="collection_id",
            )
        return self._json_call(
            "collection_get",
            f"https://huggingface.co/api/collections/{quote(value, safe='/')}",
            locals(),
            cache_ttl=300,
        )

    def papers_search(self, query: str, limit: int = 20) -> dict[str, Any]:
        limit = self._limit(limit, 120)
        return self._json_call(
            "papers_search",
            "https://huggingface.co/api/papers/search",
            locals(),
            params={"q": query[:250], "limit": limit},
            max_items=limit,
            cache_ttl=300,
        )

    def daily_papers_list(self, page: int = 0, limit: int = 20) -> dict[str, Any]:
        limit = self._limit(limit, 100)
        return self._json_call(
            "daily_papers_list",
            "https://huggingface.co/api/daily_papers",
            locals(),
            params={"p": page, "limit": limit},
            max_items=limit,
            cache_ttl=300,
        )

    def paper_get(self, paper_id: str) -> dict[str, Any]:
        paper_id = paper_id.strip()
        if not _PAPER.fullmatch(paper_id):
            raise AgentWebError(
                "paper_id is invalid", code="invalid_argument", field="paper_id"
            )
        return self._json_call(
            "paper_get",
            f"https://huggingface.co/api/papers/{quote(paper_id, safe='')}",
            locals(),
            cache_ttl=300,
        )

    def paper_read(self, paper_id: str, max_chars: int = 100_000) -> dict[str, Any]:
        paper_id = paper_id.strip()
        if not _PAPER.fullmatch(paper_id) or max_chars < 1 or max_chars > 200_000:
            raise AgentWebError(
                "paper_id or max_chars is invalid", code="invalid_argument"
            )
        response = self._response(
            "GET",
            f"https://huggingface.co/papers/{quote(paper_id, safe='')}.md",
            action="paper_read",
            arguments=locals(),
            accept="text/markdown,text/plain",
            cache_ttl=300,
        )
        text = response.text
        return self._result(
            "paper_read",
            {
                "paper_id": paper_id,
                "content": text[:max_chars],
                "characters": len(text),
                "truncated": len(text) > max_chars,
            },
        )

    def docs_list(self) -> dict[str, Any]:
        return self._json_call(
            "docs_list", "https://huggingface.co/api/docs", {}, cache_ttl=3600
        )

    def docs_search(
        self, query: str, product: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        limit = self._limit(limit, 25)
        return self._json_call(
            "docs_search",
            "https://huggingface.co/api/docs/search",
            locals(),
            params={"q": query[:250], "product": product, "limit": limit},
            max_items=limit,
            cache_ttl=300,
        )

    def kernels_list(
        self, query: str | None = None, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        limit = self._limit(limit, 100)
        return self._json_call(
            "kernels_list",
            "https://huggingface.co/api/kernels",
            locals(),
            params={"search": query, "limit": limit, "cursor": cursor},
            max_items=limit,
        )

    def kernel_get(self, repo_id: str) -> dict[str, Any]:
        repo_id = self._repo_id(repo_id)
        return self._json_call(
            "kernel_get",
            f"https://huggingface.co/api/kernels/{quote(repo_id, safe='/')}",
            locals(),
            cache_ttl=300,
        )

    def space_options_get(self, kind: str = "hardware") -> dict[str, Any]:
        if kind not in {"hardware", "templates"}:
            raise AgentWebError(
                "kind must be hardware or templates",
                code="invalid_argument",
                field="kind",
            )
        return self._json_call(
            "space_options_get",
            f"https://huggingface.co/api/spaces/{kind}",
            locals(),
            cache_ttl=3600,
        )

    def space_api_describe(self, repo_id: str) -> dict[str, Any]:
        metadata = self.space_get(repo_id)["data"]["resource"]
        host = str(metadata.get("host") or metadata.get("subdomain") or "")
        if not host:
            raise AgentWebError(
                "This Space does not publish a callable host",
                code="space_not_running",
                retryable=True,
                next_action="huggingface.space_runtime_get",
            )
        if not host.startswith("http"):
            host = f"https://{host}.hf.space" if "." not in host else f"https://{host}"
        parsed = urlparse(host)
        if parsed.scheme != "https" or not (
            (parsed.hostname or "").endswith(".hf.space")
        ):
            raise AgentWebError(
                "Space metadata returned an unsafe API host",
                code="invalid_upstream_response",
                retryable=False,
            )
        response = self.session().request(
            "GET",
            host.rstrip("/") + "/gradio_api/info",
            headers={"Accept": "application/json"},
            allowed_redirect_domains=("hf.space",),
        )
        if response.status >= 400:
            raise AgentWebError(
                "The Space API is unavailable or uses a different interface",
                code="space_api_unavailable",
                retryable=True,
                next_action="huggingface.space_runtime_get",
            )
        try:
            payload = json.loads(response.body)
        except json.JSONDecodeError as exc:
            raise AgentWebError(
                "The Space returned malformed API documentation",
                code="invalid_upstream_response",
                retryable=True,
            ) from exc
        return self._result(
            "space_api_describe",
            {"repo_id": repo_id, "api": self._bounded(payload, max_items=100)},
        )
