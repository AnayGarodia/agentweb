from __future__ import annotations

import json
import os
import re
import time
import uuid
from base64 import b64decode
from typing import Any
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from agentweb.sdk import (
    AdapterContext,
    AuthenticationRequired,
    Response,
    SiteAdapter,
    AgentWebError,
    bounded_data,
    enforce_data_budget,
    parse_query_pairs,
)
from agentweb.storage import read_json, write_json


API_URL = "https://api.github.com"
REPOSITORY_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
WEBSITE_COOKIE_NAMES = {"user_session", "__Host-user_session_same_site", "logged_in"}
PERSISTED_WEBSITE_OPERATIONS = {
    "CreateIssueQuery": ("062f0c9d93233c51041743eb2482a247", "query"),
    "createIssueMutation": ("59355b9ba02eb93a5090ead97e4236e9", "mutation"),
}
WEBSITE_GRAPHQL_DISCOVERY_MAX_ASSETS = 12
WEBSITE_GRAPHQL_DISCOVERY_MAX_SECONDS = 12.0


def bounded(value: str | None, limit: int) -> tuple[str | None, bool]:
    if not value:
        return None, False
    if len(value) <= limit:
        return value, False
    return value[: max(limit - 1, 0)].rstrip() + "…", True


class Adapter(SiteAdapter):
    site_name = "github"
    base_url = "https://github.com"
    allowed_domains = ("github.com", "githubassets.com")

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)
        self._resolved_token: str | None = None
        self._token_checked = False
        self.token_path = (
            context.paths.profile_dir("github", context.profile) / "api-token.json"
        )
        self.website_operations_path = (
            context.paths.profile_dir("github", context.profile)
            / "website-graphql-operations.json"
        )
        self.rate_state_path = (
            context.paths.profile_dir("github", context.profile) / "rate-limit.json"
        )

    @staticmethod
    def _repo_path(owner: str, repo: str) -> str:
        if not REPOSITORY_PART.fullmatch(owner) or not REPOSITORY_PART.fullmatch(repo):
            raise AgentWebError("owner and repo must be valid GitHub path components")
        return f"{owner}/{repo}"

    def _json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        cache_action: str,
        cache_arguments: dict[str, Any],
        cache_ttl: int,
        allow_text: bool = False,
    ) -> tuple[Any, Response]:
        rate_state = read_json(self.rate_state_path, {}) or {}
        if (
            str(rate_state.get("remaining")) == "0"
            and float(rate_state.get("reset_unix") or 0) > time.time()
        ):
            retry_after = max(1, int(float(rate_state["reset_unix"]) - time.time()))
            raise AgentWebError(
                f"GitHub API budget is exhausted; retry after {retry_after} seconds",
                code="github_rate_limited",
                retryable=True,
                next_action="wait until reset or configure a GitHub token with github.configure_token",
                details={**rate_state, "retry_after_seconds": retry_after},
            )
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = self.session().request(
            "GET",
            API_URL + path,
            params=params,
            headers=headers,
            cache_action=cache_action,
            cache_arguments=cache_arguments,
            # Authenticated responses may contain private data. Never place them
            # in the shared public-response cache.
            cache_ttl=0 if token else cache_ttl,
        )
        remaining_header = self._header(response, "X-RateLimit-Remaining")
        reset_header = self._header(response, "X-RateLimit-Reset")
        if remaining_header is not None or reset_header is not None:
            write_json(
                self.rate_state_path,
                {
                    "remaining": remaining_header,
                    "reset_unix": reset_header,
                    "checked_at_unix": int(time.time()),
                },
            )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            if allow_text and response.status < 400:
                payload = response.text
            else:
                raise AgentWebError("GitHub returned malformed JSON") from exc
        if response.status >= 400:
            message = payload.get("message") if isinstance(payload, dict) else None
            reset = self._header(response, "X-RateLimit-Reset")
            remaining = self._header(response, "X-RateLimit-Remaining")
            rate_limited = response.status in {403, 429} and (
                remaining == "0" or "rate limit" in (message or "").lower()
            )
            suffix = (
                f"; rate limit resets at Unix time {reset}"
                if rate_limited and reset
                else ""
            )
            raise AgentWebError(
                f"GitHub returned HTTP {response.status}: {message or 'request failed'}{suffix}",
                code="github_rate_limited" if rate_limited else "github_http_error",
                retryable=rate_limited,
                next_action=(
                    "wait until reset or configure a GitHub token with github.configure_token"
                    if rate_limited
                    else None
                ),
                details={
                    "status": response.status,
                    "rate_limit_remaining": remaining,
                    "rate_limit_reset_unix": reset,
                },
            )
        return payload, response

    def _token(self) -> str | None:
        if self._token_checked:
            return self._resolved_token
        self._token_checked = True
        self._resolved_token = (
            os.environ.get("AGENTWEB_GITHUB_TOKEN")
            or os.environ.get("SITEPACK_GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or (read_json(self.token_path, {}) or {}).get("token")
        )
        return self._resolved_token

    def direct_headers(self, url: str) -> dict[str, str]:
        host = (urlparse(url).hostname or "").lower()
        if host == "api.github.com":
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if self._token():
                headers["Authorization"] = f"Bearer {self._token()}"
            return headers
        return {
            "Accept": "text/html,application/xhtml+xml",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        }

    def _website_graphql_context(
        self, page_path: str, operation_name: str
    ) -> tuple[str, str, str, Response, int, float]:
        discovery_started = time.monotonic()
        discovery_deadline = discovery_started + WEBSITE_GRAPHQL_DISCOVERY_MAX_SECONDS
        page_url = self._direct_url(page_path)
        page = self.session().request(
            "GET",
            page_url,
            headers=self.direct_headers(page_url),
            cache_ttl=0,
            timeout_seconds=8,
        )
        soup = BeautifulSoup(page.text, "html.parser")
        login = soup.select_one('meta[name="user-login"]')
        if page.status >= 400 or login is None or not login.get("content"):
            raise AuthenticationRequired(
                "GitHub's retained website session is not signed in; run agentweb connect github"
            )
        nonce = soup.select_one('meta[name="fetch-nonce"]')
        if nonce is None or not nonce.get("content"):
            raise AgentWebError(
                "GitHub did not expose its current verified-fetch nonce"
            )
        compiled = PERSISTED_WEBSITE_OPERATIONS.get(operation_name)
        cached = (read_json(self.website_operations_path, {}) or {}).get(operation_name)
        candidate = compiled or (
            (cached.get("query_id"), cached.get("kind"))
            if isinstance(cached, dict)
            else None
        )
        if (
            candidate
            and re.fullmatch(r"[a-f0-9]{16,64}", str(candidate[0] or ""))
            and candidate[1] in {"query", "mutation"}
        ):
            return (
                str(candidate[0]),
                str(candidate[1]),
                str(nonce.get("content")),
                page,
                1,
                page.elapsed_ms,
            )
        scripts = list(
            dict.fromkeys(
                re.findall(
                    r'(?:src|href)="(https://github\.githubassets\.com/assets/[^"]+\.js)"',
                    page.text,
                )
            )
        )
        pattern = re.compile(
            r'params:\{id:"([a-f0-9]+)",metadata:\{\},name:"'
            + re.escape(operation_name)
            + r'",operationKind:"(query|mutation)"'
        )
        # Route-specific dependency chunks are at the end of GitHub's script
        # list. Stop as soon as the persisted operation is found instead of
        # downloading the whole application bundle.
        prioritized = [
            url for url in scripts if "chunk-" in url or "issues-react" in url
        ] + [
            url for url in scripts if "chunk-" not in url and "issues-react" not in url
        ]
        request_count = 1
        elapsed_ms = page.elapsed_ms
        assets_scanned = 0
        for script_url in prioritized[:WEBSITE_GRAPHQL_DISCOVERY_MAX_ASSETS]:
            remaining_seconds = discovery_deadline - time.monotonic()
            if remaining_seconds < 0.25:
                break
            asset = self.session().request(
                "GET",
                script_url,
                headers={"Accept": "application/javascript"},
                cache_action="website_graphql_asset",
                cache_arguments={"url": script_url},
                cache_ttl=3600,
                timeout_seconds=min(3.0, remaining_seconds),
            )
            assets_scanned += 1
            request_count += 1
            elapsed_ms += asset.elapsed_ms
            match = pattern.search(asset.text)
            if match:
                mappings = read_json(self.website_operations_path, {}) or {}
                mappings[operation_name] = {
                    "query_id": match.group(1),
                    "kind": match.group(2),
                    "asset_url": script_url,
                }
                write_json(self.website_operations_path, mappings)
                return (
                    match.group(1),
                    match.group(2),
                    str(nonce.get("content")),
                    page,
                    request_count,
                    elapsed_ms,
                )
        raise AgentWebError(
            f"GitHub's current assets did not contain persisted operation "
            f"{operation_name!r}; stopped after {assets_scanned} assets within "
            f"the {WEBSITE_GRAPHQL_DISCOVERY_MAX_SECONDS:g}-second discovery budget",
            code="website_replay_changed",
            retryable=True,
            details={
                "assets_available": len(prioritized),
                "assets_scanned": assets_scanned,
                "max_assets": WEBSITE_GRAPHQL_DISCOVERY_MAX_ASSETS,
                "max_seconds": WEBSITE_GRAPHQL_DISCOVERY_MAX_SECONDS,
                "elapsed_seconds": round(time.monotonic() - discovery_started, 3),
            },
        )

    def website_graphql(
        self,
        page_path: str,
        operation_name: str,
        variables: dict[str, Any],
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not page_path.startswith("/") or ".." in page_path.split("/"):
            raise AgentWebError("page_path must be a safe GitHub path")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,100}", operation_name):
            raise AgentWebError("operation_name is invalid")
        query_id, kind, nonce, page, context_requests, context_elapsed = (
            self._website_graphql_context(page_path, operation_name)
        )
        if kind == "mutation" and not confirm:
            raise AgentWebError(
                "github.website_graphql would change remote state; inspect variables and repeat with confirm=true"
            )
        body = {
            "persistedQueryName": operation_name,
            "query": query_id,
            "variables": variables,
        }
        response = self.session().request(
            "POST",
            "https://github.com/_graphql",
            json_body=body,
            headers={
                "Accept": "application/json",
                "GitHub-Verified-Fetch": "true",
                "X-Requested-With": "XMLHttpRequest",
                "X-Fetch-Nonce": nonce,
            },
            referer=page.url,
            cache_ttl=0,
        )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise AgentWebError(
                "GitHub website GraphQL returned malformed JSON"
            ) from exc
        if response.status >= 400 or payload.get("errors"):
            messages = "; ".join(
                str(item.get("message") or item) for item in payload.get("errors") or []
            )
            raise AgentWebError(
                f"GitHub website GraphQL {operation_name} failed with HTTP "
                f"{response.status}: {messages or 'request failed'}"
            )
        data, truncated = bounded_data(
            payload.get("data"), max_items=100, max_string=10000
        )
        return {
            "operation": "github.website_graphql",
            "operation_name": operation_name,
            "operation_kind": kind,
            "data": data,
            "truncated": truncated,
            "token_exposed": False,
            "meta": {
                "request_count": context_requests + 1,
                "elapsed_ms": round(context_elapsed + response.elapsed_ms, 1),
                "url": response.url,
            },
        }

    def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str = "",
        confirm: bool = False,
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        if not title.strip():
            raise AgentWebError("title cannot be empty", code="invalid_input")
        self.require_confirm(confirm, "github.create_issue")
        # Prefer the token/REST path every other GitHub write already uses.
        # Requiring a retained website session here (while comment / PR / merge
        # accept the token) was the QA "create_issue is uncallable via any auth
        # path" bug; the GraphQL website path stays as a token-less fallback.
        if self._token():
            payload, response = self._write(
                "POST",
                f"/repos/{repository}/issues",
                body={"title": title.strip(), "body": body},
            )
            data = payload if isinstance(payload, dict) else {}
            return {
                "operation": "github.create_issue",
                "created": True,
                "state_changed": True,
                "verified": bool(data.get("html_url")),
                "issue": {
                    "id": data.get("node_id"),
                    "number": data.get("number"),
                    "title": data.get("title"),
                    "url": data.get("html_url"),
                },
                "token_exposed": False,
                "meta": self._meta(response),
            }
        page_path = f"/{repository}/issues/new"
        query_id, kind, nonce, page, context_requests, context_elapsed = (
            self._website_graphql_context(page_path, "createIssueMutation")
        )
        match = re.search(
            r'"repository":\{"id":"([^"]+)"[^{}]*"databaseId"[^{}]*'
            r'"name":"'
            + re.escape(repo)
            + r'"[^{}]*"nameWithOwner":"'
            + re.escape(repository)
            + r'"',
            page.text,
        )
        if match is None:
            raise AgentWebError("GitHub did not expose the repository node ID")
        variables = {
            "input": {
                "repositoryId": match.group(1),
                "title": title.strip(),
                "body": body,
                "clientMutationId": str(uuid.uuid4()),
            },
            "fetchParent": False,
        }
        response = self.session().request(
            "POST",
            "https://github.com/_graphql",
            json_body={
                "persistedQueryName": "createIssueMutation",
                "query": query_id,
                "variables": variables,
            },
            headers={
                "Accept": "application/json",
                "GitHub-Verified-Fetch": "true",
                "X-Requested-With": "XMLHttpRequest",
                "X-Fetch-Nonce": nonce,
            },
            referer=page.url,
            cache_ttl=0,
        )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise AgentWebError("GitHub create issue returned malformed JSON") from exc
        result = (payload.get("data") or {}).get("createIssue") or {}
        errors = payload.get("errors") or result.get("errors") or []
        issue = result.get("issue") or {}
        if response.status >= 400 or errors or not issue.get("url"):
            messages = "; ".join(str(item.get("message") or item) for item in errors)
            raise AgentWebError(
                f"GitHub create issue failed with HTTP {response.status}: "
                f"{messages or 'no issue was returned'}"
            )
        return {
            "operation": "github.create_issue",
            "created": True,
            "verified": True,
            "issue": {
                "id": issue.get("id"),
                "number": issue.get("number"),
                "title": issue.get("title"),
                "url": issue.get("url"),
            },
            "control_path": "retained_github_website_session",
            "token_exposed": False,
            "meta": {
                "request_count": context_requests + 1,
                "elapsed_ms": round(context_elapsed + response.elapsed_ms, 1),
            },
        }

    def configure_token(self, token: str) -> dict[str, Any]:
        token = token.strip()
        if not re.fullmatch(
            r"(?:gh[pousr]_[A-Za-z0-9_]{20,255}|github_pat_[A-Za-z0-9_]{20,255})", token
        ):
            raise AgentWebError(
                "token does not look like a GitHub personal or fine-grained access token"
            )
        response = self.session().request(
            "GET",
            API_URL + "/user",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise AgentWebError(
                "GitHub returned malformed JSON while validating the token"
            ) from exc
        if response.status != 200:
            raise AuthenticationRequired(
                f"GitHub rejected the token with HTTP {response.status}: {payload.get('message') or 'invalid token'}"
            )
        write_json(self.token_path, {"token": token, "login": payload.get("login")})
        self._resolved_token = token
        self._token_checked = True
        return {
            "operation": "github.configure_token",
            "authenticated": True,
            "login": payload.get("login"),
            "credential_source": "agentweb_profile",
            "token_exposed": False,
            "stored_mode": oct(self.token_path.stat().st_mode & 0o777),
        }

    def disconnect(self, confirm: bool = False) -> dict[str, Any]:
        self.require_confirm(confirm, "github.disconnect")
        existed = self.token_path.exists()
        self.token_path.unlink(missing_ok=True)
        self._resolved_token = None
        self._token_checked = True
        return {
            "operation": "github.disconnect",
            "disconnected": existed,
            "environment_tokens_unchanged": True,
        }

    def auth_status(self) -> dict[str, Any]:
        source = None
        if os.environ.get("AGENTWEB_GITHUB_TOKEN"):
            source = "AGENTWEB_GITHUB_TOKEN"
        elif os.environ.get("SITEPACK_GITHUB_TOKEN"):
            source = "SITEPACK_GITHUB_TOKEN"
        elif os.environ.get("GH_TOKEN"):
            source = "GH_TOKEN"
        elif os.environ.get("GITHUB_TOKEN"):
            source = "GITHUB_TOKEN"
        elif self.token_path.exists():
            source = "agentweb_profile"
        token_available = bool(self._token())
        login = (
            (read_json(self.token_path, {}) or {}).get("login")
            if self.token_path.exists()
            else None
        )
        cookies = list(self.session().cookies)
        website_cookie_names = {cookie.name for cookie in cookies}
        logged_in_cookie = any(
            cookie.name == "logged_in" and cookie.value.lower() in {"yes", "true", "1"}
            for cookie in cookies
        )
        website_cookie_candidate = (
            bool(
                website_cookie_names & {"user_session", "__Host-user_session_same_site"}
            )
            or logged_in_cookie
        )
        website_authenticated = False
        website_login = None
        website_verification_error = None
        if website_cookie_candidate:
            try:
                response = self.session().request(
                    "GET",
                    "https://github.com/settings/profile",
                    headers={"Accept": "text/html"},
                )
                final_path = urlparse(response.url).path
                matches = re.findall(
                    r'<meta[^>]+name=["\']user-login["\'][^>]+content=["\']([^"\']*)["\']|'
                    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']user-login["\']',
                    response.text,
                    re.I,
                )
                if matches:
                    website_login = next(
                        (left or right for left, right in matches if left or right),
                        None,
                    )
                website_authenticated = (
                    response.status == 200
                    and final_path.startswith("/settings/")
                    and bool(website_login)
                )
            except AgentWebError as exc:
                website_verification_error = str(exc)
        rate_budget = read_json(self.rate_state_path, {}) or None
        if rate_budget:
            checked_at = int(rate_budget.get("checked_at_unix") or 0)
            age_seconds = max(0, int(time.time()) - checked_at) if checked_at else None
            rate_budget = {
                **rate_budget,
                "age_seconds": age_seconds,
                "fresh": age_seconds is not None and age_seconds <= 300,
                "freshness_threshold_seconds": 300,
                "meaning": "Last API response headers observed by AgentWeb; not a live quota check.",
            }
        return {
            "operation": "github.auth_status",
            "authenticated": token_available or website_authenticated,
            "api_authenticated": token_available,
            "website_authenticated": website_authenticated,
            "website_cookie_candidate": website_cookie_candidate,
            "website_session_available": website_authenticated,
            "login": login or website_login,
            "credential_source": source,
            "token_exposed": False,
            "website_verification_error": website_verification_error,
            "session": self.session_freshness(token_available or website_authenticated),
            "last_known_api_budget": rate_budget,
            "api_rate_limit_tier": (
                "authenticated_token"
                if token_available
                else "anonymous_60_requests_per_hour"
            ),
            "website_login_increases_api_quota": False,
            "api_setup_required_for_higher_limits": not token_available,
            "api_setup_operation": (
                None if token_available else "github.configure_token"
            ),
            "capabilities": {
                "github_website_session": {
                    "available": website_authenticated,
                    "covers": "github.com website-session operations",
                },
                "rest_and_graphql_api": {
                    "authenticated": token_available,
                    "covers": "api.github.com REST and GraphQL operations",
                    "public_reads_available_without_token": True,
                },
            },
            "warning": (
                "The GitHub website session is signed in, but REST and GraphQL account operations still require github.configure_token."
                if website_authenticated and not token_available
                else None
            ),
        }

    @staticmethod
    def _header(response: Response, name: str) -> str | None:
        return next(
            (
                value
                for key, value in response.headers.items()
                if key.lower() == name.lower()
            ),
            None,
        )

    def _meta(self, response: Response) -> dict[str, Any]:
        remaining = self._header(response, "X-RateLimit-Remaining")
        reset = self._header(response, "X-RateLimit-Reset")
        try:
            low_quota = remaining is not None and int(remaining) <= 10
        except ValueError:
            low_quota = False
        return {
            "elapsed_ms": round(response.elapsed_ms, 1),
            "from_cache": response.from_cache,
            "url": response.url,
            "rate_limit_remaining": remaining,
            "rate_limit_reset_unix": reset,
            "rate_limit_low": low_quota,
            "rate_limit_warning": (
                "GitHub's anonymous API quota is low. Configure a token with "
                "github.configure_token before a multi-call task."
                if low_quota
                else None
            ),
        }

    @staticmethod
    def _repository(row: dict[str, Any]) -> dict[str, Any]:
        description, truncated = bounded(row.get("description"), 500)
        license_value = row.get("license") or {}
        return {
            "full_name": row.get("full_name"),
            "description": description,
            "description_truncated": truncated,
            "url": row.get("html_url"),
            "homepage": row.get("homepage"),
            "default_branch": row.get("default_branch"),
            "language": row.get("language"),
            "stars": row.get("stargazers_count"),
            "forks": row.get("forks_count"),
            "watchers": row.get("subscribers_count"),
            "open_issues": row.get("open_issues_count"),
            "open_issues_and_pull_requests": row.get("open_issues_count"),
            "open_issues_includes_pull_requests": True,
            "archived": row.get("archived"),
            "is_fork": row.get("fork"),
            "topics": (row.get("topics") or [])[:30],
            "license": license_value.get("spdx_id"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "pushed_at": row.get("pushed_at"),
        }

    def search_repositories(
        self, query: str, limit: int = 10, sort: str = "best_match"
    ) -> dict[str, Any]:
        if not query.strip():
            raise AgentWebError("query cannot be empty")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        if sort not in {
            "best_match",
            "stars",
            "forks",
            "help_wanted_issues",
            "updated",
        }:
            raise AgentWebError(
                "sort must be best_match, stars, forks, help_wanted_issues, or updated"
            )
        params: dict[str, Any] = {"q": query, "per_page": limit}
        if sort != "best_match":
            params["sort"] = sort
            params["order"] = "desc"
        payload, response = self._json(
            "/search/repositories",
            params=params,
            cache_action="search_repositories",
            cache_arguments={"query": query, "limit": limit, "sort": sort},
            cache_ttl=120,
        )
        results = [self._repository(row) for row in payload.get("items", [])]
        return {
            "operation": "github.search_repositories",
            "query": query,
            "sort": sort,
            "count": len(results),
            "total_count": payload.get("total_count"),
            "incomplete_results": payload.get("incomplete_results"),
            "results": results,
            "meta": self._meta(response),
        }

    def repository(self, owner: str, repo: str) -> dict[str, Any]:
        path = self._repo_path(owner, repo)
        payload, response = self._json(
            f"/repos/{path}",
            cache_action="repository",
            cache_arguments={"owner": owner, "repo": repo},
            cache_ttl=300,
        )
        summary = self._repository(payload)
        exact_counts = False
        request_count = 1
        elapsed = response.elapsed_ms
        try:
            pull_payload, pull_response = self._json(
                "/search/issues",
                params={"q": f"repo:{path} is:pr is:open", "per_page": 1},
                cache_action="repository_open_pull_request_count",
                cache_arguments={"owner": owner, "repo": repo},
                cache_ttl=60,
            )
            open_pull_requests = int(pull_payload.get("total_count") or 0)
            combined = int(payload.get("open_issues_count") or 0)
            summary.update(
                {
                    "open_issues": max(0, combined - open_pull_requests),
                    "open_pull_requests": open_pull_requests,
                    "open_issues_includes_pull_requests": False,
                }
            )
            exact_counts = True
            request_count = 2
            elapsed += pull_response.elapsed_ms
        except AgentWebError:
            # The search API has a tighter anonymous rate limit than repository
            # reads. Preserve GitHub's combined REST count and label it honestly.
            summary["open_pull_requests"] = None
        meta = self._meta(response)
        meta.update(
            {
                "elapsed_ms": round(elapsed, 1),
                "request_count": request_count,
                "website_navigation_counts_exact": exact_counts,
            }
        )
        return {
            "operation": "github.repository",
            "repository": summary,
            "meta": meta,
        }

    def issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        limit: int = 10,
        labels: list[str] | None = None,
        sort: str = "created",
    ) -> dict[str, Any]:
        path = self._repo_path(owner, repo)
        if state not in {"open", "closed", "all"}:
            raise AgentWebError("state must be open, closed, or all")
        if sort not in {"created", "updated", "comments"}:
            raise AgentWebError("sort must be created, updated, or comments")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        results = []
        pull_requests_omitted = 0
        request_count = 0
        elapsed_ms = 0.0
        response: Response | None = None
        for page in range(1, 11):
            payload, response = self._json(
                f"/repos/{path}/issues",
                params={
                    "state": state,
                    "per_page": 100,
                    "page": page,
                    "labels": ",".join(labels or []),
                    "sort": sort,
                    "direction": "desc",
                },
                cache_action="issues",
                cache_arguments={
                    "owner": owner,
                    "repo": repo,
                    "state": state,
                    "limit": limit,
                    "labels": labels or [],
                    "sort": sort,
                    "page": page,
                },
                cache_ttl=120,
            )
            request_count += 1
            elapsed_ms += response.elapsed_ms
            for row in payload:
                if row.get("pull_request"):
                    pull_requests_omitted += 1
                    continue
                body, truncated = bounded(row.get("body"), 1000)
                results.append(
                    {
                        "number": row.get("number"),
                        "title": row.get("title"),
                        "state": row.get("state"),
                        "url": row.get("html_url"),
                        "author": (row.get("user") or {}).get("login"),
                        "labels": [
                            item.get("name") for item in row.get("labels") or []
                        ],
                        "comments": row.get("comments"),
                        "created_at": row.get("created_at"),
                        "updated_at": row.get("updated_at"),
                        "closed_at": row.get("closed_at"),
                        "body": body,
                        "body_truncated": truncated,
                    }
                )
                if len(results) >= limit:
                    break
            if len(results) >= limit or len(payload) < 100:
                break
        assert response is not None
        meta = self._meta(response)
        meta.update(
            {"request_count": request_count, "elapsed_ms": round(elapsed_ms, 1)}
        )
        return {
            "operation": "github.issues",
            "repository": path,
            "state": state,
            "count": len(results),
            "pull_requests_omitted": pull_requests_omitted,
            "issues": results,
            "meta": meta,
        }

    def releases(self, owner: str, repo: str, limit: int = 10) -> dict[str, Any]:
        path = self._repo_path(owner, repo)
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        payload, response = self._json(
            f"/repos/{path}/releases",
            params={"per_page": limit},
            cache_action="releases",
            cache_arguments={"owner": owner, "repo": repo, "limit": limit},
            cache_ttl=300,
        )
        results = []
        for row in payload:
            body, truncated = bounded(row.get("body"), 1500)
            results.append(
                {
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "tag_name": row.get("tag_name"),
                    "url": row.get("html_url"),
                    "draft": row.get("draft"),
                    "prerelease": row.get("prerelease"),
                    "author": (row.get("author") or {}).get("login"),
                    "published_at": row.get("published_at"),
                    "body": body,
                    "body_truncated": truncated,
                    "assets": [
                        {
                            "name": asset.get("name"),
                            "size": asset.get("size"),
                            "download_url": asset.get("browser_download_url"),
                        }
                        for asset in (row.get("assets") or [])[:20]
                    ],
                }
            )
        return {
            "operation": "github.releases",
            "repository": path,
            "count": len(results),
            "releases": results,
            "meta": self._meta(response),
        }

    def issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        path = self._repo_path(owner, repo)
        if number < 1:
            raise AgentWebError("number must be positive")
        row, response = self._json(
            f"/repos/{path}/issues/{number}",
            cache_action="issue",
            cache_arguments={"owner": owner, "repo": repo, "number": number},
            cache_ttl=120,
        )
        body, truncated = bounded(row.get("body"), 5000)
        return {
            "operation": "github.issue",
            "repository": path,
            "issue": {
                "number": row.get("number"),
                "title": row.get("title"),
                "state": row.get("state"),
                "state_reason": row.get("state_reason"),
                "is_pull_request": bool(row.get("pull_request")),
                "url": row.get("html_url"),
                "author": (row.get("user") or {}).get("login"),
                "assignees": [item.get("login") for item in row.get("assignees") or []],
                "labels": [item.get("name") for item in row.get("labels") or []],
                "comments": row.get("comments"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "closed_at": row.get("closed_at"),
                "body": body,
                "body_truncated": truncated,
            },
            "meta": self._meta(response),
        }

    def pull_requests(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        limit: int = 10,
        sort: str = "created",
    ) -> dict[str, Any]:
        path = self._repo_path(owner, repo)
        if state not in {"open", "closed", "all"}:
            raise AgentWebError("state must be open, closed, or all")
        if sort not in {"created", "updated", "popularity", "long-running"}:
            raise AgentWebError(
                "sort must be created, updated, popularity, or long-running"
            )
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        payload, response = self._json(
            f"/repos/{path}/pulls",
            params={
                "state": state,
                "per_page": limit,
                "sort": sort,
                "direction": "desc",
            },
            cache_action="pull_requests",
            cache_arguments={
                "owner": owner,
                "repo": repo,
                "state": state,
                "limit": limit,
                "sort": sort,
            },
            cache_ttl=120,
        )
        rows = []
        for item in payload:
            body, truncated = bounded(item.get("body"), 1000)
            rows.append(
                {
                    "number": item.get("number"),
                    "title": item.get("title"),
                    "state": item.get("state"),
                    "draft": item.get("draft"),
                    "url": item.get("html_url"),
                    "author": (item.get("user") or {}).get("login"),
                    "head": (item.get("head") or {}).get("label"),
                    "base": (item.get("base") or {}).get("label"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "body": body,
                    "body_truncated": truncated,
                }
            )
        return {
            "operation": "github.pull_requests",
            "repository": path,
            "state": state,
            "count": len(rows),
            "pull_requests": rows,
            "meta": self._meta(response),
        }

    def pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        path = self._repo_path(owner, repo)
        if number < 1:
            raise AgentWebError("number must be positive")
        row, response = self._json(
            f"/repos/{path}/pulls/{number}",
            cache_action="pull_request",
            cache_arguments={"owner": owner, "repo": repo, "number": number},
            cache_ttl=120,
        )
        body, truncated = bounded(row.get("body"), 5000)
        return {
            "operation": "github.pull_request",
            "repository": path,
            "pull_request": {
                "number": row.get("number"),
                "title": row.get("title"),
                "state": row.get("state"),
                "draft": row.get("draft"),
                "merged": row.get("merged"),
                "mergeable": row.get("mergeable"),
                "mergeable_state": row.get("mergeable_state"),
                "url": row.get("html_url"),
                "author": (row.get("user") or {}).get("login"),
                "head": (row.get("head") or {}).get("label"),
                "head_sha": (row.get("head") or {}).get("sha"),
                "base": (row.get("base") or {}).get("label"),
                "base_sha": (row.get("base") or {}).get("sha"),
                "commits": row.get("commits"),
                "additions": row.get("additions"),
                "deletions": row.get("deletions"),
                "changed_files": row.get("changed_files"),
                "comments": row.get("comments"),
                "review_comments": row.get("review_comments"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "merged_at": row.get("merged_at"),
                "body": body,
                "body_truncated": truncated,
            },
            "meta": self._meta(response),
        }

    def commits(
        self,
        owner: str,
        repo: str,
        limit: int = 10,
        sha: str | None = None,
        path_filter: str | None = None,
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        params: dict[str, Any] = {"per_page": limit}
        if sha:
            params["sha"] = sha
        if path_filter:
            params["path"] = path_filter
        payload, response = self._json(
            f"/repos/{repository}/commits",
            params=params,
            cache_action="commits",
            cache_arguments={
                "owner": owner,
                "repo": repo,
                "limit": limit,
                "sha": sha,
                "path_filter": path_filter,
            },
            cache_ttl=120,
        )
        rows = []
        for item in payload:
            commit = item.get("commit") or {}
            author = commit.get("author") or {}
            message, truncated = bounded(commit.get("message"), 1000)
            rows.append(
                {
                    "sha": item.get("sha"),
                    "url": item.get("html_url"),
                    "author": (item.get("author") or {}).get("login")
                    or author.get("name"),
                    "authored_at": author.get("date"),
                    "message": message,
                    "message_truncated": truncated,
                    "verified": ((commit.get("verification") or {}).get("verified")),
                }
            )
        return {
            "operation": "github.commits",
            "repository": repository,
            "count": len(rows),
            "commits": rows,
            "meta": self._meta(response),
        }

    def contents(
        self,
        owner: str,
        repo: str,
        path: str = "",
        ref: str | None = None,
        max_chars: int = 12000,
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        if max_chars < 200 or max_chars > 100000:
            raise AgentWebError("max_chars must be between 200 and 100000")
        if ".." in path.split("/"):
            raise AgentWebError("path cannot contain ..")
        params = {"ref": ref} if ref else None
        encoded = quote(path.strip("/"), safe="/")
        suffix = f"/{encoded}" if encoded else ""
        payload, response = self._json(
            f"/repos/{repository}/contents{suffix}",
            params=params,
            cache_action="contents",
            cache_arguments={"owner": owner, "repo": repo, "path": path, "ref": ref},
            cache_ttl=300,
        )
        if isinstance(payload, list):
            entries = [
                {
                    "name": item.get("name"),
                    "path": item.get("path"),
                    "type": item.get("type"),
                    "size": item.get("size"),
                    "sha": item.get("sha"),
                    "url": item.get("html_url"),
                    "download_url": item.get("download_url"),
                }
                for item in payload[:500]
            ]
            return {
                "operation": "github.contents",
                "repository": repository,
                "path": path,
                "type": "directory",
                "count": len(entries),
                "entries": entries,
                "meta": self._meta(response),
            }
        content = None
        decode_error = None
        if payload.get("type") == "file" and payload.get("content"):
            try:
                content = b64decode(payload["content"].replace("\n", "")).decode(
                    "utf-8"
                )
            except (ValueError, UnicodeDecodeError) as exc:
                decode_error = str(exc)
        content, truncated = bounded(content, max_chars)
        return {
            "operation": "github.contents",
            "repository": repository,
            "path": payload.get("path"),
            "type": payload.get("type"),
            "size": payload.get("size"),
            "sha": payload.get("sha"),
            "url": payload.get("html_url"),
            "download_url": payload.get("download_url"),
            "content": content,
            "content_truncated": truncated,
            "content_unavailable_reason": decode_error
            or ("too_large_or_binary" if content is None else None),
            "meta": self._meta(response),
        }

    def contributors(self, owner: str, repo: str, limit: int = 20) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        payload, response = self._json(
            f"/repos/{repository}/contributors",
            params={"per_page": limit},
            cache_action="contributors",
            cache_arguments={"owner": owner, "repo": repo, "limit": limit},
            cache_ttl=300,
        )
        rows = [
            {
                "username": item.get("login"),
                "contributions": item.get("contributions"),
                "type": item.get("type"),
                "url": item.get("html_url"),
            }
            for item in payload
        ]
        return {
            "operation": "github.contributors",
            "repository": repository,
            "count": len(rows),
            "contributors": rows,
            "meta": self._meta(response),
        }

    def user(self, username: str) -> dict[str, Any]:
        if not REPOSITORY_PART.fullmatch(username):
            raise AgentWebError("username is invalid")
        row, response = self._json(
            f"/users/{username}",
            cache_action="user",
            cache_arguments={"username": username},
            cache_ttl=300,
        )
        bio, truncated = bounded(row.get("bio"), 1000)
        return {
            "operation": "github.user",
            "user": {
                "username": row.get("login"),
                "name": row.get("name"),
                "type": row.get("type"),
                "url": row.get("html_url"),
                "avatar_url": row.get("avatar_url"),
                "company": row.get("company"),
                "blog": row.get("blog"),
                "location": row.get("location"),
                "bio": bio,
                "bio_truncated": truncated,
                "public_repositories": row.get("public_repos"),
                "public_gists": row.get("public_gists"),
                "followers": row.get("followers"),
                "following": row.get("following"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            },
            "meta": self._meta(response),
        }

    def branches(self, owner: str, repo: str, limit: int = 20) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        payload, response = self._json(
            f"/repos/{repository}/branches",
            params={"per_page": limit},
            cache_action="branches",
            cache_arguments={"owner": owner, "repo": repo, "limit": limit},
            cache_ttl=120,
        )
        rows = [
            {
                "name": row.get("name"),
                "sha": (row.get("commit") or {}).get("sha"),
                "protected": row.get("protected"),
            }
            for row in payload
        ]
        return {
            "operation": "github.branches",
            "repository": repository,
            "count": len(rows),
            "branches": rows,
            "ranking": {
                "method": "github_api_order",
                "exact_website_order": False,
            },
            "meta": self._meta(response),
        }

    def tags(self, owner: str, repo: str, limit: int = 20) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        payload, response = self._json(
            f"/repos/{repository}/tags",
            params={"per_page": limit},
            cache_action="tags",
            cache_arguments={"owner": owner, "repo": repo, "limit": limit},
            cache_ttl=120,
        )
        rows = [
            {
                "name": row.get("name"),
                "sha": (row.get("commit") or {}).get("sha"),
                "url": f"https://github.com/{repository}/releases/tag/{quote(str(row.get('name') or ''), safe='')}",
                "archive_zip_url": f"https://github.com/{repository}/archive/refs/tags/{quote(str(row.get('name') or ''), safe='')}.zip",
                "archive_tar_url": f"https://github.com/{repository}/archive/refs/tags/{quote(str(row.get('name') or ''), safe='')}.tar.gz",
            }
            for row in payload
        ]
        return {
            "operation": "github.tags",
            "repository": repository,
            "count": len(rows),
            "tags": rows,
            "ranking": {
                "method": "github_api_recent_tag_order",
                "exact_website_order": False,
            },
            "meta": self._meta(response),
        }

    def issue_comments(
        self, owner: str, repo: str, number: int, limit: int = 20
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        if number < 1:
            raise AgentWebError("number must be positive")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        payload, response = self._json(
            f"/repos/{repository}/issues/{number}/comments",
            params={"per_page": limit},
            cache_action="issue_comments",
            cache_arguments={
                "owner": owner,
                "repo": repo,
                "number": number,
                "limit": limit,
            },
            cache_ttl=120,
        )
        rows = []
        for item in payload:
            body, truncated = bounded(item.get("body"), 3000)
            rows.append(
                {
                    "id": item.get("id"),
                    "author": (item.get("user") or {}).get("login"),
                    "url": item.get("html_url"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "author_association": item.get("author_association"),
                    "body": body,
                    "body_truncated": truncated,
                }
            )
        return {
            "operation": "github.issue_comments",
            "repository": repository,
            "number": number,
            "count": len(rows),
            "comments": rows,
            "meta": self._meta(response),
        }

    def pull_request_files(
        self, owner: str, repo: str, number: int, limit: int = 50
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        if number < 1:
            raise AgentWebError("number must be positive")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        payload, response = self._json(
            f"/repos/{repository}/pulls/{number}/files",
            params={"per_page": limit},
            cache_action="pull_request_files",
            cache_arguments={
                "owner": owner,
                "repo": repo,
                "number": number,
                "limit": limit,
            },
            cache_ttl=120,
        )
        rows = []
        for item in payload:
            patch, truncated = bounded(item.get("patch"), 4000)
            rows.append(
                {
                    "filename": item.get("filename"),
                    "status": item.get("status"),
                    "additions": item.get("additions"),
                    "deletions": item.get("deletions"),
                    "changes": item.get("changes"),
                    "previous_filename": item.get("previous_filename"),
                    "url": item.get("blob_url"),
                    "raw_url": item.get("raw_url"),
                    "patch": patch,
                    "patch_truncated": truncated,
                }
            )
        return {
            "operation": "github.pull_request_files",
            "repository": repository,
            "number": number,
            "count": len(rows),
            "files": rows,
            "meta": self._meta(response),
        }

    def api_get(
        self,
        path: str,
        query: list[str] | None = None,
        max_items: int = 50,
        max_string: int = 4000,
        max_total_chars: int = 20000,
    ) -> dict[str, Any]:
        if not path.startswith("/") or "://" in path or ".." in path.split("/"):
            raise AgentWebError("path must be a safe GitHub API path beginning with /")
        if max_items < 1 or max_items > 200 or max_string < 100 or max_string > 20000:
            raise AgentWebError("max_items or max_string is out of range")
        if max_total_chars < 1000 or max_total_chars > 100000:
            raise AgentWebError("max_total_chars must be between 1000 and 100000")
        params = parse_query_pairs(query)
        payload, response = self._json(
            path,
            params=params,
            cache_action="api_get",
            cache_arguments={"path": path, "query": query or []},
            cache_ttl=60,
            allow_text=True,
        )
        data, truncated = bounded_data(
            payload, max_items=max_items, max_string=max_string
        )
        data, total_truncated, original_chars = enforce_data_budget(
            data, max_total_chars=max_total_chars
        )
        return {
            "operation": "github.api_get",
            "path": path,
            "query": params,
            "data": data,
            "data_format": "json" if not isinstance(payload, str) else "text",
            "truncated": truncated or total_truncated,
            "truncation": {
                "nested_limit": truncated,
                "character_budget": total_truncated,
            },
            "original_chars": original_chars,
            "max_total_chars": max_total_chars,
            "meta": self._meta(response),
        }

    def _require_token(self) -> str:
        token = self._token()
        if not token:
            raise AuthenticationRequired(
                "This GitHub write requires an API token. Call github.configure_token "
                "once with a fine-grained personal access token, then retry.",
                next_action="github.configure_token",
            )
        return token

    @staticmethod
    def _positive_int(value: int, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise AgentWebError(
                f"{field} must be a positive integer", code="invalid_input"
            )
        return value

    def _raise_status(self, response: Response, payload: Any) -> None:
        message = payload.get("message") if isinstance(payload, dict) else None
        status = response.status
        if status in {401, 403}:
            remaining = self._header(response, "X-RateLimit-Remaining")
            reset = self._header(response, "X-RateLimit-Reset")
            if status == 403 and (
                remaining == "0" or "rate limit" in (message or "").lower()
            ):
                raise AgentWebError(
                    f"GitHub applied an API rate limit; resets at Unix time {reset}",
                    code="github_rate_limited",
                    retryable=True,
                    next_action="github.configure_token",
                    details={"rate_limit_reset_unix": reset},
                )
            raise AuthenticationRequired(
                f"GitHub rejected the token with HTTP {status}: "
                f"{message or 'insufficient permissions'}. Ensure the fine-grained "
                "token grants this repository and the required read/write scope.",
                next_action="github.configure_token",
            )
        if status == 404:
            raise AgentWebError(
                f"GitHub has no resource at that owner, repo, or number: "
                f"{message or 'not found'}",
                code="not_found",
            )
        if status in {409, 422}:
            errors = payload.get("errors") if isinstance(payload, dict) else None
            raise AgentWebError(
                f"GitHub rejected the request: {message or 'validation failed'}",
                code="invalid_input",
                details={"errors": errors} if errors else None,
            )
        if status >= 500:
            raise AgentWebError(
                f"GitHub is temporarily unavailable (HTTP {status})",
                code="github_unavailable",
                retryable=True,
            )
        raise AgentWebError(
            f"GitHub returned HTTP {status}: {message or 'request failed'}",
            code="github_http_error",
        )

    def _write(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> tuple[Any, Response]:
        """Authenticated REST mutation: token required, rate tracked, errors mapped."""
        token = self._require_token()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
        }
        response = self.session().request(
            method,
            API_URL + path,
            json_body=body,
            headers=headers,
            cache_ttl=0,
        )
        remaining_header = self._header(response, "X-RateLimit-Remaining")
        reset_header = self._header(response, "X-RateLimit-Reset")
        if remaining_header is not None or reset_header is not None:
            write_json(
                self.rate_state_path,
                {
                    "remaining": remaining_header,
                    "reset_unix": reset_header,
                    "checked_at_unix": int(time.time()),
                },
            )
        if response.status == 204 or not response.text.strip():
            payload: Any = None
        else:
            try:
                payload = json.loads(response.text)
            except json.JSONDecodeError as exc:
                raise AgentWebError("GitHub returned malformed JSON") from exc
        if response.status >= 400:
            self._raise_status(response, payload)
        return payload, response

    def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
        draft: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        if not title.strip():
            raise AgentWebError("title cannot be empty", code="invalid_input")
        if not head.strip() or not base.strip():
            raise AgentWebError(
                "head and base branches are required", code="invalid_input"
            )
        self.require_confirm(confirm, "github.create_pull_request")
        payload, response = self._write(
            "POST",
            f"/repos/{repository}/pulls",
            body={
                "title": title.strip(),
                "head": head.strip(),
                "base": base.strip(),
                "body": body,
                "draft": bool(draft),
            },
        )
        data = payload if isinstance(payload, dict) else {}
        return {
            "operation": "github.create_pull_request",
            "created": True,
            "state_changed": True,
            "pull_request": {
                "number": data.get("number"),
                "title": data.get("title"),
                "state": data.get("state"),
                "draft": data.get("draft"),
                "html_url": data.get("html_url"),
                "head": (data.get("head") or {}).get("ref"),
                "base": (data.get("base") or {}).get("ref"),
            },
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def merge_pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
        merge_method: str = "merge",
        commit_title: str = "",
        commit_message: str = "",
        confirm: bool = False,
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        number = self._positive_int(number, "number")
        if merge_method not in {"merge", "squash", "rebase"}:
            raise AgentWebError(
                "merge_method must be merge, squash, or rebase", code="invalid_input"
            )
        self.require_confirm(confirm, "github.merge_pull_request")
        request: dict[str, Any] = {"merge_method": merge_method}
        if commit_title.strip():
            request["commit_title"] = commit_title.strip()
        if commit_message.strip():
            request["commit_message"] = commit_message.strip()
        payload, response = self._write(
            "PUT",
            f"/repos/{repository}/pulls/{number}/merge",
            body=request,
        )
        data = payload if isinstance(payload, dict) else {}
        return {
            "operation": "github.merge_pull_request",
            "merged": bool(data.get("merged")),
            "state_changed": bool(data.get("merged")),
            "number": number,
            "sha": data.get("sha"),
            "message": data.get("message"),
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def review_pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
        event: str,
        body: str = "",
        confirm: bool = False,
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        number = self._positive_int(number, "number")
        event = event.upper()
        if event not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}:
            raise AgentWebError(
                "event must be APPROVE, REQUEST_CHANGES, or COMMENT",
                code="invalid_input",
            )
        if event != "APPROVE" and not body.strip():
            raise AgentWebError(
                "REQUEST_CHANGES and COMMENT reviews require a body",
                code="invalid_input",
            )
        self.require_confirm(confirm, "github.review_pull_request")
        request: dict[str, Any] = {"event": event}
        if body.strip():
            request["body"] = body.strip()
        payload, response = self._write(
            "POST",
            f"/repos/{repository}/pulls/{number}/reviews",
            body=request,
        )
        data = payload if isinstance(payload, dict) else {}
        return {
            "operation": "github.review_pull_request",
            "submitted": True,
            "state_changed": True,
            "number": number,
            "review": {
                "id": data.get("id"),
                "state": data.get("state"),
                "html_url": data.get("html_url"),
            },
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def comment(
        self,
        owner: str,
        repo: str,
        number: int,
        body: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        number = self._positive_int(number, "number")
        if not body.strip():
            raise AgentWebError("comment body cannot be empty", code="invalid_input")
        self.require_confirm(confirm, "github.comment")
        payload, response = self._write(
            "POST",
            f"/repos/{repository}/issues/{number}/comments",
            body={"body": body},
        )
        data = payload if isinstance(payload, dict) else {}
        return {
            "operation": "github.comment",
            "created": True,
            "state_changed": True,
            "number": number,
            "comment": {"id": data.get("id"), "html_url": data.get("html_url")},
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def update_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        title: str = "",
        body: str = "",
        state: str = "",
        state_reason: str = "",
        confirm: bool = False,
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        number = self._positive_int(number, "number")
        request: dict[str, Any] = {}
        if title.strip():
            request["title"] = title.strip()
        if body:
            request["body"] = body
        if state:
            if state not in {"open", "closed"}:
                raise AgentWebError(
                    "state must be open or closed", code="invalid_input"
                )
            request["state"] = state
        if state_reason:
            if state_reason not in {"completed", "not_planned", "reopened"}:
                raise AgentWebError(
                    "state_reason must be completed, not_planned, or reopened",
                    code="invalid_input",
                )
            request["state_reason"] = state_reason
        if not request:
            raise AgentWebError(
                "provide at least one of title, body, state, or state_reason",
                code="invalid_input",
            )
        self.require_confirm(confirm, "github.update_issue")
        payload, response = self._write(
            "PATCH",
            f"/repos/{repository}/issues/{number}",
            body=request,
        )
        data = payload if isinstance(payload, dict) else {}
        return {
            "operation": "github.update_issue",
            "updated": True,
            "state_changed": True,
            "issue": {
                "number": data.get("number"),
                "state": data.get("state"),
                "title": data.get("title"),
                "html_url": data.get("html_url"),
            },
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def add_reaction(
        self,
        owner: str,
        repo: str,
        number: int,
        content: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        number = self._positive_int(number, "number")
        allowed = {"+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"}
        if content not in allowed:
            raise AgentWebError(
                f"content must be one of {sorted(allowed)}", code="invalid_input"
            )
        self.require_confirm(confirm, "github.add_reaction")
        payload, response = self._write(
            "POST",
            f"/repos/{repository}/issues/{number}/reactions",
            body={"content": content},
        )
        data = payload if isinstance(payload, dict) else {}
        return {
            "operation": "github.add_reaction",
            "created": True,
            "state_changed": True,
            "number": number,
            "reaction": {"id": data.get("id"), "content": data.get("content")},
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def set_star(
        self,
        owner: str,
        repo: str,
        starred: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        self.require_confirm(confirm, "github.set_star")
        method = "PUT" if starred else "DELETE"
        _, response = self._write(method, f"/user/starred/{repository}")
        return {
            "operation": "github.set_star",
            "starred": bool(starred),
            "state_changed": True,
            "repository": repository,
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def fork_repository(
        self,
        owner: str,
        repo: str,
        default_branch_only: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        self.require_confirm(confirm, "github.fork_repository")
        payload, response = self._write(
            "POST",
            f"/repos/{repository}/forks",
            body={"default_branch_only": bool(default_branch_only)},
        )
        data = payload if isinstance(payload, dict) else {}
        return {
            "operation": "github.fork_repository",
            "created": True,
            "state_changed": True,
            "fork": {
                "full_name": data.get("full_name"),
                "html_url": data.get("html_url"),
            },
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def create_release(
        self,
        owner: str,
        repo: str,
        tag_name: str,
        name: str = "",
        body: str = "",
        draft: bool = False,
        prerelease: bool = False,
        target_commitish: str = "",
        confirm: bool = False,
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        if not tag_name.strip():
            raise AgentWebError("tag_name cannot be empty", code="invalid_input")
        self.require_confirm(confirm, "github.create_release")
        request: dict[str, Any] = {
            "tag_name": tag_name.strip(),
            "body": body,
            "draft": bool(draft),
            "prerelease": bool(prerelease),
        }
        if name.strip():
            request["name"] = name.strip()
        if target_commitish.strip():
            request["target_commitish"] = target_commitish.strip()
        payload, response = self._write(
            "POST",
            f"/repos/{repository}/releases",
            body=request,
        )
        data = payload if isinstance(payload, dict) else {}
        return {
            "operation": "github.create_release",
            "created": True,
            "state_changed": True,
            "release": {
                "id": data.get("id"),
                "tag_name": data.get("tag_name"),
                "name": data.get("name"),
                "draft": data.get("draft"),
                "prerelease": data.get("prerelease"),
                "html_url": data.get("html_url"),
            },
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def dispatch_workflow(
        self,
        owner: str,
        repo: str,
        workflow: str,
        ref: str,
        inputs: dict[str, Any] | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        repository = self._repo_path(owner, repo)
        if not str(workflow).strip():
            raise AgentWebError(
                "workflow (file name or numeric id) is required", code="invalid_input"
            )
        if not ref.strip():
            raise AgentWebError("ref cannot be empty", code="invalid_input")
        self.require_confirm(confirm, "github.dispatch_workflow")
        request: dict[str, Any] = {"ref": ref.strip()}
        if inputs:
            request["inputs"] = inputs
        _, response = self._write(
            "POST",
            f"/repos/{repository}/actions/workflows/{quote(str(workflow), safe='')}/dispatches",
            body=request,
        )
        return {
            "operation": "github.dispatch_workflow",
            "dispatched": True,
            "state_changed": True,
            "workflow": str(workflow),
            "ref": ref.strip(),
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def create_gist(
        self,
        files: dict[str, str],
        description: str = "",
        public: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(files, dict) or not files:
            raise AgentWebError(
                "files must be a non-empty map of filename to content",
                code="invalid_input",
            )
        gist_files: dict[str, Any] = {}
        for name, content in files.items():
            if not isinstance(content, str) or not content:
                raise AgentWebError(
                    f"file {name!r} must have non-empty string content",
                    code="invalid_input",
                )
            gist_files[name] = {"content": content}
        self.require_confirm(confirm, "github.create_gist")
        payload, response = self._write(
            "POST",
            "/gists",
            body={
                "description": description,
                "public": bool(public),
                "files": gist_files,
            },
        )
        data = payload if isinstance(payload, dict) else {}
        return {
            "operation": "github.create_gist",
            "created": True,
            "state_changed": True,
            "gist": {
                "id": data.get("id"),
                "public": data.get("public"),
                "html_url": data.get("html_url"),
                "files": sorted((data.get("files") or {}).keys()),
            },
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def api_request(
        self,
        path: str,
        method: str = "GET",
        query: list[str] | None = None,
        body: dict[str, Any] | None = None,
        confirm: bool = False,
        max_items: int = 100,
        max_string: int = 10000,
        max_total_chars: int = 50000,
    ) -> dict[str, Any]:
        """Authenticated REST/GraphQL escape hatch with an explicit write gate."""
        if not path.startswith("/") or "://" in path or ".." in path.split("/"):
            raise AgentWebError("path must be a safe GitHub API path beginning with /")
        method = method.upper()
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise AgentWebError("method must be GET, HEAD, POST, PUT, PATCH, or DELETE")
        mutating = method not in {"GET", "HEAD"}
        if mutating and not confirm:
            raise AgentWebError(
                f"github.api_request {method} may change GitHub state; repeat with confirm=true"
            )
        token = self._token()
        if mutating and not token:
            raise AuthenticationRequired(
                "This GitHub REST/GraphQL write requires an API token. Call github.configure_token once, then retry.",
                next_action="github.configure_token",
            )
        if max_items < 1 or max_items > 500 or max_string < 100 or max_string > 50000:
            raise AgentWebError("max_items or max_string is out of range")
        if max_total_chars < 1000 or max_total_chars > 200000:
            raise AgentWebError("max_total_chars must be between 1000 and 200000")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        params = parse_query_pairs(query)
        response = self.session().request(
            method,
            API_URL + path,
            params=params,
            json_body=body
            if method not in {"GET", "HEAD"} and body is not None
            else None,
            headers=headers,
        )
        if response.status == 204 or not response.text.strip():
            payload: Any = None
        else:
            try:
                payload = json.loads(response.text)
            except json.JSONDecodeError:
                payload = response.text
        if response.status >= 400:
            message = payload.get("message") if isinstance(payload, dict) else payload
            if response.status == 401 and not token:
                raise AuthenticationRequired(
                    "This GitHub REST/GraphQL resource requires an API token. A website login cookie cannot authenticate api.github.com; call github.configure_token once, then retry.",
                    next_action="github.configure_token",
                )
            raise AgentWebError(
                f"GitHub returned HTTP {response.status}: {message or 'request failed'}"
            )
        data, truncated = bounded_data(
            payload, max_items=max_items, max_string=max_string
        )
        data, total_truncated, original_chars = enforce_data_budget(
            data, max_total_chars=max_total_chars
        )
        return {
            "operation": "github.api_request",
            "method": method,
            "path": path,
            "query": params,
            "authenticated": bool(token),
            "state_changed": mutating,
            "status": response.status,
            "data": data,
            "truncated": truncated or total_truncated,
            "original_chars": original_chars,
            "meta": self._meta(response),
        }
