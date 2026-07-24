from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from agentweb.sdk import (
    AdapterContext,
    AgentWebError,
    AuthenticationRequired,
    Response,
    SiteAdapter,
    bounded_data,
    enforce_data_budget,
    parse_query_pairs,
)
from agentweb.storage import read_json, write_json


LINKEDIN_API = "https://api.linkedin.com"
LINKEDIN_WEB = "https://www.linkedin.com"
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
JOB_ID = re.compile(r"^[0-9]{5,30}$")
TOKEN = re.compile(r"^[^\s]{20,4096}$")
API_PATH = re.compile(r"^/(?:v2|rest)/[A-Za-z0-9_./:()=-]+$")
URN = re.compile(r"^urn:li:[A-Za-z0-9_]+:[A-Za-z0-9._%:()=,+-]{1,200}$")
MEMBER_ID = re.compile(r"^ACoAA[A-Za-z0-9_-]{5,80}$")
REACTIONS = ("LIKE", "PRAISE", "EMPATHY", "INTEREST", "APPRECIATION", "ENTERTAINMENT")


def _text(node: Any) -> str | None:
    if node is None:
        return None
    value = " ".join(node.get_text(" ", strip=True).split())
    return value or None


class Adapter(SiteAdapter):
    site_name = "linkedin"
    base_url = LINKEDIN_WEB
    allowed_domains = ("linkedin.com", "licdn.com")

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)
        self.token_path = (
            context.paths.profile_dir("linkedin", context.profile) / "api-token.json"
        )
        self._resolved_token: str | None = None
        self._token_checked = False

    @staticmethod
    def _meta(response: Response) -> dict[str, Any]:
        return {
            "elapsed_ms": round(response.elapsed_ms, 1),
            "from_cache": response.from_cache,
            "url": response.url,
            "transport": response.transport,
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

    @staticmethod
    def _public_headers() -> dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.linkedin.com/jobs/search/",
        }

    def _token(self) -> str | None:
        if self._token_checked:
            return self._resolved_token
        self._token_checked = True
        self._resolved_token = (
            os.environ.get("AGENTWEB_LINKEDIN_TOKEN")
            or os.environ.get("LINKEDIN_ACCESS_TOKEN")
            or (read_json(self.token_path, {}) or {}).get("token")
        )
        return self._resolved_token

    def _website_cookie(self, name: str) -> str | None:
        return next(
            (
                cookie.value
                for cookie in self.session().cookies
                if cookie.name == name
                and (
                    cookie.domain.lstrip(".") == "linkedin.com"
                    or cookie.domain.lstrip(".").endswith(".linkedin.com")
                )
            ),
            None,
        )

    def _website_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.linkedin.normalized+json+2.1",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Li-Lang": "en_US",
            "X-RestLi-Protocol-Version": "2.0.0",
            "X-Li-Track": '{"clientVersion":"1.13.30000","osName":"web","timezoneOffset":0,"deviceFormFactor":"DESKTOP","mpName":"voyager-web"}',
            "Referer": "https://www.linkedin.com/feed/",
        }
        jsession = self._website_cookie("JSESSIONID")
        if jsession:
            headers["Csrf-Token"] = jsession.strip('"')
        return headers

    @staticmethod
    def _member_identity(payload: Any) -> dict[str, Any]:
        """Find LinkedIn's compact member identity without binding to one decoration."""
        candidates: list[dict[str, Any]] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if value.get("publicIdentifier") or (
                    value.get("firstName") and value.get("lastName")
                ):
                    candidates.append(value)
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(payload)
        row = candidates[0] if candidates else {}
        first = str(row.get("firstName") or "").strip()
        last = str(row.get("lastName") or "").strip()
        name = " ".join(value for value in (first, last) if value) or None
        public_identifier = row.get("publicIdentifier")
        return {
            "name": name,
            "public_identifier": public_identifier,
            "profile_url": (
                f"https://www.linkedin.com/in/{public_identifier}/"
                if public_identifier
                else None
            ),
        }

    def _public_response(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        cache_action: str,
        cache_arguments: dict[str, Any],
        cache_ttl: int,
    ) -> Response:
        response = self.session().request(
            "GET",
            url,
            params=params,
            headers=self._public_headers(),
            cache_action=cache_action,
            cache_arguments=cache_arguments,
            cache_ttl=cache_ttl,
            impersonate="chrome146",
        )
        if response.status in {403, 429, 999}:
            raise AgentWebError(
                "LinkedIn refused this public request or asked the client to slow down",
                code="linkedin_public_access_limited",
                retryable=True,
                next_action="wait before retrying; do not loop or attempt to bypass LinkedIn's access controls",
                details={"status": response.status, "url": response.url},
            )
        if response.status >= 400:
            raise AgentWebError(
                f"LinkedIn returned HTTP {response.status}",
                code="linkedin_http_error",
                retryable=response.status >= 500,
                details={"status": response.status, "url": response.url},
            )
        return response

    def jobs_search(
        self,
        query: str,
        location: str | None = None,
        limit: int = 10,
        start: int = 0,
    ) -> dict[str, Any]:
        query = " ".join(query.split())
        location = " ".join(location.split()) if location else None
        if not query or len(query) > 200:
            raise AgentWebError("query must contain 1-200 characters")
        if location is not None and len(location) > 200:
            raise AgentWebError("location must contain at most 200 characters")
        if limit < 1 or limit > 25:
            raise AgentWebError("limit must be between 1 and 25")
        if start < 0 or start > 975:
            raise AgentWebError("start must be between 0 and 975")
        params = {"keywords": query, "start": start}
        if location:
            params["location"] = location
        response = self._public_response(
            LINKEDIN_WEB + "/jobs-guest/jobs/api/seeMoreJobPostings/search",
            params=params,
            cache_action="jobs_search",
            cache_arguments={
                "query": query,
                "location": location,
                "limit": limit,
                "start": start,
            },
            cache_ttl=120,
        )
        soup = BeautifulSoup(response.text, "html.parser")
        jobs: list[dict[str, Any]] = []
        for card in soup.select(".base-search-card"):
            urn = str(card.get("data-entity-urn") or "")
            link = card.select_one("a.base-card__full-link")
            href = str(link.get("href") or "").strip() if link else ""
            match = re.search(r"jobPosting:([0-9]+)", urn) or re.search(
                r"/jobs/view/(?:[^/?]+-)?([0-9]+)", href
            )
            if not match:
                continue
            company_link = card.select_one(".base-search-card__subtitle a")
            time_node = card.select_one("time")
            jobs.append(
                {
                    "id": match.group(1),
                    "title": _text(card.select_one(".base-search-card__title")),
                    "company": _text(card.select_one(".base-search-card__subtitle")),
                    "company_url": (
                        str(company_link.get("href") or "").strip()
                        if company_link
                        else None
                    ),
                    "location": _text(
                        card.select_one(".job-search-card__location")
                    ),
                    "listed_at": (
                        str(time_node.get("datetime") or "").strip()
                        if time_node
                        else None
                    ),
                    "url": href or None,
                }
            )
            if len(jobs) >= limit:
                break
        return {
            "operation": "linkedin.jobs_search",
            "query": query,
            "location": location,
            "start": start,
            "count": len(jobs),
            "jobs": jobs,
            "next_start": start + len(jobs) if len(jobs) == limit else None,
            "source": "linkedin_public_jobs",
            "meta": self._meta(response),
        }

    def job_get(self, job_id: str | int) -> dict[str, Any]:
        job_id = str(job_id).strip()
        if not JOB_ID.fullmatch(job_id):
            raise AgentWebError("job_id must contain only 5-30 digits")
        response = self._public_response(
            LINKEDIN_WEB + f"/jobs-guest/jobs/api/jobPosting/{job_id}",
            cache_action="job_get",
            cache_arguments={"job_id": job_id},
            cache_ttl=300,
        )
        soup = BeautifulSoup(response.text, "html.parser")
        title = _text(soup.select_one(".top-card-layout__title"))
        if not title:
            raise AgentWebError(
                "LinkedIn did not return a public job record for that ID",
                code="linkedin_job_not_found",
                retryable=False,
            )
        company_node = soup.select_one(
            ".topcard__org-name-link, .topcard__flavor-row a"
        )
        time_node = soup.select_one("time")
        posted_text = _text(soup.select_one(".posted-time-ago__text"))
        apply_link = soup.select_one(
            "a.apply-button, a[data-tracking-control-name*='apply']"
        )
        apply_url = (
            str(apply_link.get("href") or "").strip() if apply_link else None
        )
        if apply_url and "/signup/" in apply_url:
            apply_url = None
        description = _text(
            soup.select_one(".show-more-less-html__markup, .description__text")
        )
        if description and len(description) > 20_000:
            description = description[:19_999].rstrip() + "…"
        return {
            "operation": "linkedin.job_get",
            "job": {
                "id": job_id,
                "title": title,
                "company": _text(company_node),
                "company_url": (
                    str(company_node.get("href") or "").strip()
                    if company_node
                    else None
                ),
                "location": _text(
                    soup.select_one(".topcard__flavor-row .topcard__flavor--bullet")
                ),
                "listed_at": (
                    str(time_node.get("datetime") or "").strip()
                    if time_node
                    else posted_text
                ),
                "description": description,
                "apply_url": apply_url,
                "url": f"https://www.linkedin.com/jobs/view/{job_id}",
            },
            "source": "linkedin_public_jobs",
            "meta": self._meta(response),
        }

    def company_get(self, slug: str) -> dict[str, Any]:
        slug = slug.strip()
        if not SLUG.fullmatch(slug):
            raise AgentWebError(
                "slug must be a valid LinkedIn company path component"
            )
        response = self._public_response(
            LINKEDIN_WEB + f"/company/{slug}/",
            cache_action="company_get",
            cache_arguments={"slug": slug},
            cache_ttl=600,
        )
        soup = BeautifulSoup(response.text, "html.parser")

        def meta(property_name: str) -> str | None:
            node = soup.select_one(f'meta[property="{property_name}"]')
            return (
                str(node.get("content") or "").strip()
                if node and node.get("content")
                else None
            )

        title = meta("og:title") or (_text(soup.title) if soup.title else None)
        description = meta("og:description")
        if not title:
            raise AgentWebError(
                "LinkedIn did not expose a public company page for that slug",
                code="linkedin_company_not_found",
                retryable=False,
            )
        canonical = soup.select_one('link[rel="canonical"]')
        return {
            "operation": "linkedin.company_get",
            "company": {
                "slug": slug,
                "name": re.sub(r"\s*\|\s*LinkedIn\s*$", "", title).strip(),
                "headline": description,
                "image_url": meta("og:image"),
                "url": (
                    str(canonical.get("href") or "").strip()
                    if canonical
                    else response.url
                ),
            },
            "source": "linkedin_public_company_page",
            "meta": self._meta(response),
        }

    def configure_token(self, token: str) -> dict[str, Any]:
        token = token.strip()
        if not TOKEN.fullmatch(token):
            raise AgentWebError(
                "token does not look like a LinkedIn OAuth access token"
            )
        response = self.session().request(
            "GET",
            LINKEDIN_API + "/v2/userinfo",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            cache_ttl=0,
        )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise AgentWebError(
                "LinkedIn returned malformed JSON while validating the token"
            ) from exc
        if response.status != 200:
            raise AuthenticationRequired(
                f"LinkedIn rejected the OAuth token with HTTP {response.status}"
            )
        write_json(
            self.token_path,
            {
                "token": token,
                "sub": payload.get("sub"),
                "name": payload.get("name"),
                "email": payload.get("email"),
            },
        )
        self._resolved_token = token
        self._token_checked = True
        return {
            "operation": "linkedin.configure_token",
            "authenticated": True,
            "account": {
                "id": payload.get("sub"),
                "name": payload.get("name"),
                "email": payload.get("email"),
            },
            "credential_source": "agentweb_profile",
            "token_exposed": False,
            "stored_mode": oct(self.token_path.stat().st_mode & 0o777),
        }

    def account_status(self) -> dict[str, Any]:
        """Verify the retained normal LinkedIn website session through Voyager."""
        li_at = self._website_cookie("li_at")
        if not li_at:
            return {
                "operation": "linkedin.account_status",
                "signed_in": False,
                "website_session_available": False,
                "account": None,
                "cookie_count": self.session().cookie_summary()["count"],
                "session": self.session_freshness(False),
                "verification": "no_li_at_cookie",
            }
        response = self.session().request(
            "GET",
            LINKEDIN_WEB + "/voyager/api/me",
            headers=self._website_headers(),
            cache_ttl=0,
            impersonate="chrome146",
        )
        payload: Any = None
        if response.body:
            try:
                payload = json.loads(response.text)
            except json.JSONDecodeError:
                payload = None
        if response.status in {403, 429, 999}:
            return {
                "operation": "linkedin.account_status",
                "signed_in": False,
                "website_session_available": False,
                "account": None,
                "cookie_count": self.session().cookie_summary()["count"],
                "cookie_candidate": True,
                "session": self.session_freshness(
                    False, state="challenge_required"
                ),
                "verification": "voyager_challenge_required",
                "challenge": {
                    "status": response.status,
                    "retryable": True,
                    "next_action": "agentweb connect linkedin --mode session",
                },
                "meta": self._meta(response),
                "warning": (
                    "LinkedIn retained a login cookie but challenged direct "
                    "Voyager verification. Do not report this profile as connected "
                    "until a refreshed session passes /voyager/api/me."
                ),
            }
        signed_in = response.status == 200 and isinstance(payload, dict)
        return {
            "operation": "linkedin.account_status",
            "signed_in": signed_in,
            "website_session_available": signed_in,
            "account": self._member_identity(payload) if signed_in else None,
            "cookie_count": self.session().cookie_summary()["count"],
            "session": self.session_freshness(signed_in),
            "verification": "voyager_me" if signed_in else "voyager_rejected",
            "meta": self._meta(response),
            "warning": (
                None
                if signed_in
                else "LinkedIn retained a login cookie but rejected direct account verification. Run `agentweb connect linkedin --mode session` to refresh it."
            ),
        }

    def auth_status(self) -> dict[str, Any]:
        source = None
        if os.environ.get("AGENTWEB_LINKEDIN_TOKEN"):
            source = "AGENTWEB_LINKEDIN_TOKEN"
        elif os.environ.get("LINKEDIN_ACCESS_TOKEN"):
            source = "LINKEDIN_ACCESS_TOKEN"
        elif self.token_path.exists():
            source = "agentweb_profile"
        saved = read_json(self.token_path, {}) or {}
        website = self.account_status()
        return {
            "operation": "linkedin.auth_status",
            "authenticated": bool(
                website.get("signed_in") or self._token()
            ),
            "website_authenticated": bool(website.get("signed_in")),
            "website": website,
            "official_api_token_available": bool(self._token()),
            "credential_source": source,
            "account": {
                "id": saved.get("sub"),
                "name": saved.get("name"),
                "email": saved.get("email"),
            }
            if saved
            else None,
            "public_jobs_and_company_reads_require_login": False,
            "website_session_login_supported": True,
            "typed_member_operations_complete": False,
            "note": "A normal LinkedIn login supplies a retained website session. The optional OAuth token only adds operations approved for the developer app that issued it.",
        }

    def disconnect(self, confirm: bool = False) -> dict[str, Any]:
        self.require_confirm(confirm, "linkedin.disconnect")
        token_existed = self.token_path.exists()
        cookie_path = self.context.paths.cookie_file(
            "linkedin", self.context.profile
        )
        cookies_existed = cookie_path.exists()
        self.token_path.unlink(missing_ok=True)
        cookie_path.unlink(missing_ok=True)
        self._session = None
        self._resolved_token = None
        self._token_checked = True
        return {
            "operation": "linkedin.disconnect",
            "disconnected": token_existed or cookies_existed,
            "website_session_deleted": cookies_existed,
            "saved_oauth_token_deleted": token_existed,
            "environment_tokens_unchanged": True,
        }

    def api_request(
        self,
        path: str,
        method: str = "GET",
        query: list[str] | None = None,
        body: dict[str, Any] | None = None,
        linkedin_version: str | None = None,
        confirm: bool = False,
        max_items: int = 100,
        max_string: int = 10_000,
        max_total_chars: int = 50_000,
    ) -> dict[str, Any]:
        if not API_PATH.fullmatch(path) or ".." in path.split("/"):
            raise AgentWebError(
                "path must be a safe LinkedIn API path beginning with /v2/ or /rest/"
            )
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise AgentWebError("method must be GET, POST, PUT, PATCH, or DELETE")
        if method != "GET":
            self.require_confirm(confirm, "linkedin.api_request")
        if linkedin_version is not None and not re.fullmatch(
            r"20[0-9]{4}", linkedin_version
        ):
            raise AgentWebError("linkedin_version must use LinkedIn's YYYYMM format")
        if not (1 <= max_items <= 200 and 100 <= max_string <= 20_000):
            raise AgentWebError("max_items or max_string is out of range")
        if not 1_000 <= max_total_chars <= 100_000:
            raise AgentWebError(
                "max_total_chars must be between 1000 and 100000"
            )
        token = self._token()
        if not token:
            raise AuthenticationRequired(
                "This LinkedIn API operation needs an approved OAuth token; run linkedin.configure_token"
            )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        }
        if linkedin_version:
            headers["LinkedIn-Version"] = linkedin_version
        params = parse_query_pairs(query)
        response = self.session().request(
            method,
            LINKEDIN_API + path,
            params=params,
            json_body=body,
            headers=headers,
            cache_ttl=0,
        )
        try:
            payload: Any = json.loads(response.text) if response.body else None
        except json.JSONDecodeError:
            payload = response.text
        if response.status >= 400:
            message = (
                payload.get("message")
                if isinstance(payload, dict)
                else str(payload)[:500]
            )
            raise AgentWebError(
                f"LinkedIn API returned HTTP {response.status}: {message or 'request failed'}",
                code=(
                    "linkedin_api_rate_limited"
                    if response.status == 429
                    else "linkedin_api_error"
                ),
                retryable=response.status in {429, 500, 502, 503, 504},
                details={"status": response.status, "path": path},
            )
        data, nested_truncated = bounded_data(
            payload, max_items=max_items, max_string=max_string
        )
        data, budget_truncated, original_chars = enforce_data_budget(
            data, max_total_chars=max_total_chars
        )
        return {
            "operation": "linkedin.api_request",
            "method": method,
            "path": path,
            "query": params,
            "data": data,
            "truncated": nested_truncated or budget_truncated,
            "original_chars": original_chars,
            "token_exposed": False,
            "meta": {
                **self._meta(response),
                "rate_limit_remaining": self._header(
                    response, "X-RestLi-RateLimit-Remaining"
                ),
            },
        }

    # -- Member-website writes (Voyager) ---------------------------------
    #
    # These ride the retained normal LinkedIn website session (li_at cookie +
    # JSESSIONID CSRF token), because LinkedIn exposes no approved public API
    # for personal connection, messaging, posting, or reactions. The Voyager
    # request contracts below are reverse-engineered from the LinkedIn web
    # client and are therefore NOT semantically verified: they are typed,
    # input-validated, confirmation-gated, and session-gated, but a passing
    # call proves only that LinkedIn accepted the request, and LinkedIn may
    # change these internal endpoints without notice. Automating them can also
    # trip LinkedIn anti-automation controls, so every op is confirm-gated.

    def _require_session(self, operation: str) -> str:
        """Require a retained member website session; return the CSRF token."""
        li_at = self._website_cookie("li_at")
        jsession = self._website_cookie("JSESSIONID")
        csrf = jsession.strip('"') if jsession else None
        if not li_at or not csrf:
            raise AuthenticationRequired(
                f"{operation} needs a retained LinkedIn member website session. "
                "Run `agentweb connect linkedin` to sign in once, then retry.",
                next_action="linkedin.account_status",
            )
        return csrf

    def _raise_voyager(self, response: Response, payload: Any) -> None:
        status = response.status
        message = payload.get("message") if isinstance(payload, dict) else None
        if status in {401, 403, 999}:
            raise AuthenticationRequired(
                f"LinkedIn refused this member action with HTTP {status}. The "
                "session may be stale or challenged; run "
                "`agentweb connect linkedin` to refresh it.",
                next_action="linkedin.account_status",
            )
        if status == 429:
            raise AgentWebError(
                "LinkedIn rate-limited this member action; slow down and retry later",
                code="linkedin_rate_limited",
                retryable=True,
                next_action="wait before retrying; do not loop member writes",
            )
        if status in {400, 422}:
            raise AgentWebError(
                f"LinkedIn rejected this member action: {message or 'validation failed'}",
                code="invalid_input",
                details={"status": status},
            )
        if status >= 500:
            raise AgentWebError(
                f"LinkedIn is temporarily unavailable (HTTP {status})",
                code="linkedin_unavailable",
                retryable=True,
            )
        raise AgentWebError(
            f"LinkedIn returned HTTP {status} for this member action",
            code="linkedin_http_error",
            details={"status": status},
        )

    def _voyager_write(
        self,
        path: str,
        *,
        operation: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, Response]:
        """POST a Voyager member-website mutation with the session CSRF token."""
        self._require_session(operation)
        headers = {
            **self._website_headers(),
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
        }
        response = self.session().request(
            "POST",
            LINKEDIN_WEB + path,
            params=params,
            json_body=body,
            headers=headers,
            cache_ttl=0,
            impersonate="chrome146",
        )
        try:
            payload: Any = json.loads(response.text) if response.body else None
        except json.JSONDecodeError:
            payload = None
        if response.status >= 400:
            self._raise_voyager(response, payload)
        return payload, response

    @staticmethod
    def _require_urn(value: str, field: str, kind: str | None = None) -> str:
        value = value.strip()
        if not URN.fullmatch(value):
            raise AgentWebError(
                f"{field} must be a LinkedIn URN like urn:li:{kind or 'fsd_profile'}:...",
                code="invalid_input",
            )
        if kind and not value.startswith(f"urn:li:{kind}:"):
            raise AgentWebError(
                f"{field} must be a urn:li:{kind}: URN", code="invalid_input"
            )
        return value

    @staticmethod
    def _require_text(value: str, field: str, maximum: int) -> str:
        value = value.strip()
        if not value:
            raise AgentWebError(f"{field} cannot be empty", code="invalid_input")
        if len(value) > maximum:
            raise AgentWebError(
                f"{field} must be at most {maximum} characters", code="invalid_input"
            )
        return value

    def send_invitation(
        self,
        profile_urn: str,
        message: str = "",
        confirm: bool = False,
    ) -> dict[str, Any]:
        profile_urn = self._require_urn(profile_urn, "profile_urn", "fsd_profile")
        message = message.strip()
        if len(message) > 300:
            raise AgentWebError(
                "custom invitation message must be at most 300 characters",
                code="invalid_input",
            )
        self.require_confirm(confirm, "linkedin.send_invitation")
        body: dict[str, Any] = {
            "invitee": {"inviteeUnion": {"memberProfile": profile_urn}}
        }
        if message:
            body["customMessage"] = message
        _payload, response = self._voyager_write(
            "/voyager/api/voyagerRelationshipsDashMemberRelationships",
            params={"action": "verifyQuotaAndCreateV2"},
            body=body,
            operation="linkedin.send_invitation",
        )
        return {
            "operation": "linkedin.send_invitation",
            "sent": True,
            "state_changed": True,
            "invitee": profile_urn,
            "custom_message": bool(message),
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def send_message(
        self,
        recipient: str,
        text: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        recipient = recipient.strip()
        member_id = recipient
        if recipient.startswith("urn:li:"):
            self._require_urn(recipient, "recipient", "fsd_profile")
            member_id = recipient.rsplit(":", 1)[-1]
        if not MEMBER_ID.fullmatch(member_id):
            raise AgentWebError(
                "recipient must be a LinkedIn member id (ACoAA...) or "
                "urn:li:fsd_profile: URN",
                code="invalid_input",
            )
        text = self._require_text(text, "text", 8000)
        self.require_confirm(confirm, "linkedin.send_message")
        body = {
            "keyVersion": "LEGACY_INBOX",
            "conversationCreate": {
                "eventCreate": {
                    "value": {
                        "com.linkedin.voyager.messaging.create.MessageCreate": {
                            "body": text,
                            "attachments": [],
                            "attributedBody": {"text": text, "attributes": []},
                            "mediaAttachments": [],
                        }
                    }
                },
                "recipients": [member_id],
                "subtype": "MEMBER_TO_MEMBER",
            },
        }
        _payload, response = self._voyager_write(
            "/voyager/api/messaging/conversations",
            params={"action": "create"},
            body=body,
            operation="linkedin.send_message",
        )
        return {
            "operation": "linkedin.send_message",
            "sent": True,
            "state_changed": True,
            "recipient": member_id,
            "characters": len(text),
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def create_post(
        self,
        text: str,
        visibility: str = "ANYONE",
        confirm: bool = False,
    ) -> dict[str, Any]:
        text = self._require_text(text, "text", 3000)
        visibility = visibility.strip().upper()
        if visibility not in {"ANYONE", "CONNECTIONS"}:
            raise AgentWebError(
                "visibility must be ANYONE or CONNECTIONS", code="invalid_input"
            )
        self.require_confirm(confirm, "linkedin.create_post")
        body = {
            "visibleToConnectionsOnly": visibility == "CONNECTIONS",
            "externalAudienceProviders": [],
            "commentaryV2": {"text": text, "attributes": []},
            "origin": "FEED",
            "allowedCommentersScope": "ALL",
            "postState": "PUBLISHED",
            "media": [],
        }
        payload, response = self._voyager_write(
            "/voyager/api/contentcreation/normShares",
            body=body,
            operation="linkedin.create_post",
        )
        data = payload if isinstance(payload, dict) else {}
        return {
            "operation": "linkedin.create_post",
            "created": True,
            "state_changed": True,
            "visibility": visibility,
            "characters": len(text),
            "urn": data.get("urn") or data.get("activityUrn"),
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def comment(
        self,
        activity_urn: str,
        text: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        activity_urn = self._require_urn(activity_urn, "activity_urn")
        text = self._require_text(text, "text", 3000)
        self.require_confirm(confirm, "linkedin.comment")
        body = {
            "comment": {
                "values": [{"value": {"attributes": [], "text": text}}]
            },
            "commentSourceUnion": {"member": None},
        }
        payload, response = self._voyager_write(
            f"/voyager/api/feed/comments?threadUrn={activity_urn}",
            body=body,
            operation="linkedin.comment",
        )
        data = payload if isinstance(payload, dict) else {}
        return {
            "operation": "linkedin.comment",
            "created": True,
            "state_changed": True,
            "activity_urn": activity_urn,
            "characters": len(text),
            "urn": data.get("urn"),
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def react(
        self,
        activity_urn: str,
        reaction: str = "LIKE",
        confirm: bool = False,
    ) -> dict[str, Any]:
        activity_urn = self._require_urn(activity_urn, "activity_urn")
        reaction = reaction.strip().upper()
        if reaction not in REACTIONS:
            raise AgentWebError(
                f"reaction must be one of {', '.join(REACTIONS)}",
                code="invalid_input",
            )
        self.require_confirm(confirm, "linkedin.react")
        body = {"reactionType": reaction}
        _payload, response = self._voyager_write(
            "/voyager/api/voyagerSocialDashReactions",
            params={"threadUrn": activity_urn},
            body=body,
            operation="linkedin.react",
        )
        return {
            "operation": "linkedin.react",
            "reacted": True,
            "state_changed": True,
            "activity_urn": activity_urn,
            "reaction": reaction,
            "token_exposed": False,
            "meta": self._meta(response),
        }

    def follow(
        self,
        entity_urn: str,
        follow: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        entity_urn = self._require_urn(entity_urn, "entity_urn")
        if not isinstance(follow, bool):
            raise AgentWebError("follow must be a boolean", code="invalid_input")
        self.require_confirm(confirm, "linkedin.follow")
        body = {
            "patch": {
                "$set": {"following": follow}
            }
        }
        _payload, response = self._voyager_write(
            "/voyager/api/feed/dash/followingStates",
            params={"ids": f"List({entity_urn})"},
            body=body,
            operation="linkedin.follow",
        )
        return {
            "operation": "linkedin.follow",
            "following": follow,
            "state_changed": True,
            "entity_urn": entity_urn,
            "token_exposed": False,
            "meta": self._meta(response),
        }
