from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup

from agentweb.sdk import (
    AdapterContext,
    HttpSession,
    Response,
    SiteAdapter,
    AuthenticationRequired,
    AgentWebError,
    bounded_data,
    enforce_data_budget,
    parse_query_pairs,
)


LANGUAGE = re.compile(r"^[a-z][a-z0-9-]{1,11}$")
API_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "AgentWeb/0.12.2 (public research adapter)",
}


def clean_html(value: str | None) -> str | None:
    if not value:
        return None
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split()) or None


def bounded(value: str | None, limit: int) -> tuple[str | None, bool]:
    if value is None or len(value) <= limit:
        return value, False
    return value[: max(limit - 1, 0)].rstrip() + "…", True


class Adapter(SiteAdapter):
    site_name = "wikipedia"
    base_url = "https://www.wikipedia.org"
    allowed_domains = ("wikipedia.org", "wikimedia.org")

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)
        self._supported_languages: set[str] | None = None

    def _endpoint(self, language: str) -> str:
        if not LANGUAGE.fullmatch(language):
            raise AgentWebError("language must be a short Wikipedia language code")
        if language == "en":
            return "https://en.wikipedia.org/w/api.php"
        if self._supported_languages is None:
            response = self.session().request(
                "GET",
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "meta": "siteinfo",
                    "siprop": "languages",
                    "format": "json",
                    "formatversion": 2,
                },
                headers=API_HEADERS,
                cache_action="supported_languages",
                cache_arguments={},
                cache_ttl=86400,
            )
            self._raise_http_error(response, "language metadata")
            try:
                payload = json.loads(response.text)
            except json.JSONDecodeError as exc:
                raise AgentWebError("Wikipedia returned malformed language metadata") from exc
            self._supported_languages = {
                item.get("code")
                for item in payload.get("query", {}).get("languages", [])
                if item.get("code")
            }
        if language not in self._supported_languages:
            raise AgentWebError(f"Wikipedia language code {language!r} is not supported")
        return f"https://{language}.wikipedia.org/w/api.php"

    @staticmethod
    def _raise_http_error(response: Response, label: str) -> None:
        if response.status < 400:
            return
        retry_after = next(
            (value for key, value in response.headers.items() if key.lower() == "retry-after"),
            None,
        )
        suffix = f"; retry after {retry_after} seconds" if retry_after else ""
        kind = "rate limited" if response.status == 429 else "failed"
        raise AgentWebError(
            f"Wikipedia {label} request was {kind} with HTTP {response.status}{suffix}"
        )

    def _json(
        self,
        language: str,
        params: dict[str, Any],
        *,
        cache_action: str,
        cache_arguments: dict[str, Any],
        cache_ttl: int,
    ) -> tuple[Any, Response]:
        response = self.session().request(
            "GET",
            self._endpoint(language),
            params={"format": "json", "formatversion": 2, "origin": "*", **params},
            headers=API_HEADERS,
            cache_action=cache_action,
            cache_arguments={"language": language, **cache_arguments},
            cache_ttl=cache_ttl,
        )
        self._raise_http_error(response, "API")
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise AgentWebError("Wikipedia returned malformed JSON") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise AgentWebError(
                f"Wikipedia API error: {payload['error'].get('info', payload['error'])}"
            )
        return payload, response

    @staticmethod
    def _meta(response: Response) -> dict[str, Any]:
        return {
            "elapsed_ms": round(response.elapsed_ms, 1),
            "from_cache": response.from_cache,
            "url": response.url,
        }

    def search(self, query: str, limit: int = 10, language: str = "en") -> dict[str, Any]:
        if not query.strip():
            raise AgentWebError("query cannot be empty")
        if limit < 1 or limit > 50:
            raise AgentWebError("limit must be between 1 and 50")
        payload, response = self._json(
            language,
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": limit,
                "srprop": "snippet|wordcount|timestamp|sectiontitle",
            },
            cache_action="search",
            cache_arguments={"query": query, "limit": limit},
            cache_ttl=300,
        )
        results = []
        for row in payload.get("query", {}).get("search", []):
            results.append(
                {
                    "page_id": row.get("pageid"),
                    "title": row.get("title"),
                    "snippet": clean_html(row.get("snippet")),
                    "section_title": row.get("sectiontitle"),
                    "word_count": row.get("wordcount"),
                    "updated_at": row.get("timestamp"),
                    "url": f"https://{language}.wikipedia.org/wiki/{quote(str(row.get('title', '')).replace(' ', '_'))}",
                }
            )
        return {
            "operation": "wikipedia.search",
            "query": query,
            "language": language,
            "count": len(results),
            "total_hits": payload.get("query", {}).get("searchinfo", {}).get("totalhits"),
            "results": results,
            "meta": self._meta(response),
        }

    def page(
        self, title: str, language: str = "en", max_chars: int = 8000
    ) -> dict[str, Any]:
        if not title.strip():
            raise AgentWebError("title cannot be empty")
        if max_chars < 200 or max_chars > 50000:
            raise AgentWebError("max_chars must be between 200 and 50000")
        payload, response = self._json(
            language,
            {
                "action": "query",
                "prop": "extracts|info|pageimages|description",
                "titles": title,
                "redirects": 1,
                "explaintext": 1,
                "inprop": "url",
                "piprop": "thumbnail",
                "pithumbsize": 640,
            },
            cache_action="page",
            cache_arguments={"title": title},
            cache_ttl=900,
        )
        pages = payload.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            raise AgentWebError(f"Wikipedia page {title!r} was not found")
        row = pages[0]
        extract, truncated = bounded(row.get("extract"), max_chars)
        return {
            "operation": "wikipedia.page",
            "page": {
                "page_id": row.get("pageid"),
                "title": row.get("title"),
                "description": row.get("description"),
                "extract": extract,
                "extract_truncated": truncated,
                "url": row.get("fullurl"),
                "canonical_url": row.get("canonicalurl"),
                "thumbnail_url": (row.get("thumbnail") or {}).get("source"),
                "language": language,
            },
            "meta": self._meta(response),
        }

    def random(self, limit: int = 5, language: str = "en") -> dict[str, Any]:
        if limit < 1 or limit > 20:
            raise AgentWebError("limit must be between 1 and 20")
        payload, response = self._json(
            language,
            {
                "action": "query",
                "list": "random",
                "rnnamespace": 0,
                "rnlimit": limit,
            },
            cache_action="random",
            cache_arguments={"limit": limit},
            cache_ttl=0,
        )
        pages = [
            {
                "page_id": row.get("id"),
                "title": row.get("title"),
                "url": f"https://{language}.wikipedia.org/wiki/{quote(str(row.get('title', '')).replace(' ', '_'))}",
            }
            for row in payload.get("query", {}).get("random", [])
        ]
        return {
            "operation": "wikipedia.random",
            "language": language,
            "count": len(pages),
            "pages": pages,
            "meta": self._meta(response),
        }

    def links(self, title: str, limit: int = 50, language: str = "en") -> dict[str, Any]:
        if not title.strip():
            raise AgentWebError("title cannot be empty")
        if limit < 1 or limit > 500:
            raise AgentWebError("limit must be between 1 and 500")
        payload, response = self._json(
            language,
            {"action": "query", "prop": "links", "titles": title, "redirects": 1, "plnamespace": 0, "pllimit": limit},
            cache_action="links",
            cache_arguments={"title": title, "limit": limit},
            cache_ttl=600,
        )
        pages = payload.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            raise AgentWebError(f"Wikipedia page {title!r} was not found")
        rows = [
            {
                "page_id": item.get("pageid"),
                "title": item.get("title"),
                "url": f"https://{language}.wikipedia.org/wiki/{quote(str(item.get('title', '')).replace(' ', '_'))}",
            }
            for item in pages[0].get("links", [])
        ]
        return {"operation": "wikipedia.links", "title": pages[0].get("title"), "language": language, "count": len(rows), "links": rows, "has_more": bool(payload.get("continue")), "meta": self._meta(response)}

    def backlinks(self, title: str, limit: int = 50, language: str = "en") -> dict[str, Any]:
        if not title.strip():
            raise AgentWebError("title cannot be empty")
        if limit < 1 or limit > 500:
            raise AgentWebError("limit must be between 1 and 500")
        payload, response = self._json(
            language,
            {"action": "query", "list": "backlinks", "bltitle": title, "blnamespace": 0, "bllimit": limit, "blfilterredir": "nonredirects"},
            cache_action="backlinks",
            cache_arguments={"title": title, "limit": limit},
            cache_ttl=600,
        )
        rows = [
            {"page_id": item.get("pageid"), "title": item.get("title"), "url": f"https://{language}.wikipedia.org/wiki/{quote(str(item.get('title', '')).replace(' ', '_'))}"}
            for item in payload.get("query", {}).get("backlinks", [])
        ]
        return {"operation": "wikipedia.backlinks", "title": title, "language": language, "count": len(rows), "backlinks": rows, "has_more": bool(payload.get("continue")), "meta": self._meta(response)}

    def category_members(self, category: str, limit: int = 50, language: str = "en") -> dict[str, Any]:
        if not category.strip():
            raise AgentWebError("category cannot be empty")
        if limit < 1 or limit > 500:
            raise AgentWebError("limit must be between 1 and 500")
        category_title = category if category.lower().startswith("category:") else f"Category:{category}"
        payload, response = self._json(
            language,
            {"action": "query", "list": "categorymembers", "cmtitle": category_title, "cmlimit": limit, "cmtype": "page|subcat"},
            cache_action="category_members",
            cache_arguments={"category": category_title, "limit": limit},
            cache_ttl=600,
        )
        rows = [
            {"page_id": item.get("pageid"), "title": item.get("title"), "namespace": item.get("ns"), "url": f"https://{language}.wikipedia.org/wiki/{quote(str(item.get('title', '')).replace(' ', '_'))}"}
            for item in payload.get("query", {}).get("categorymembers", [])
        ]
        return {"operation": "wikipedia.category_members", "category": category_title, "language": language, "count": len(rows), "members": rows, "has_more": bool(payload.get("continue")), "meta": self._meta(response)}

    def revisions(self, title: str, limit: int = 20, language: str = "en") -> dict[str, Any]:
        if not title.strip():
            raise AgentWebError("title cannot be empty")
        if limit < 1 or limit > 500:
            raise AgentWebError("limit must be between 1 and 500")
        payload, response = self._json(
            language,
            {"action": "query", "prop": "revisions", "titles": title, "redirects": 1, "rvlimit": limit, "rvprop": "ids|timestamp|user|comment|size|flags"},
            cache_action="revisions",
            cache_arguments={"title": title, "limit": limit},
            cache_ttl=300,
        )
        pages = payload.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            raise AgentWebError(f"Wikipedia page {title!r} was not found")
        rows = []
        for item in pages[0].get("revisions", []):
            comment, truncated = bounded(item.get("comment"), 500)
            rows.append({"revision_id": item.get("revid"), "parent_id": item.get("parentid"), "timestamp": item.get("timestamp"), "user": item.get("user"), "size": item.get("size"), "minor": bool(item.get("minor")), "comment": comment, "comment_truncated": truncated})
        return {"operation": "wikipedia.revisions", "title": pages[0].get("title"), "language": language, "count": len(rows), "revisions": rows, "has_more": bool(payload.get("continue")), "meta": self._meta(response)}

    def languages(self, title: str, limit: int = 100, language: str = "en") -> dict[str, Any]:
        if not title.strip():
            raise AgentWebError("title cannot be empty")
        if limit < 1 or limit > 500:
            raise AgentWebError("limit must be between 1 and 500")
        payload, response = self._json(
            language,
            {"action": "query", "prop": "langlinks", "titles": title, "redirects": 1, "lllimit": limit, "llprop": "url|langname|autonym"},
            cache_action="languages",
            cache_arguments={"title": title, "limit": limit},
            cache_ttl=900,
        )
        pages = payload.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            raise AgentWebError(f"Wikipedia page {title!r} was not found")
        rows = [{"language": item.get("lang"), "language_name": item.get("langname"), "autonym": item.get("autonym"), "title": item.get("title"), "url": item.get("url")} for item in pages[0].get("langlinks", [])]
        return {"operation": "wikipedia.languages", "title": pages[0].get("title"), "source_language": language, "count": len(rows), "languages": rows, "has_more": bool(payload.get("continue")), "meta": self._meta(response)}

    def nearby(self, latitude: float, longitude: float, radius: int = 1000, limit: int = 20, language: str = "en") -> dict[str, Any]:
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise AgentWebError("latitude or longitude is out of range")
        if radius < 10 or radius > 10000:
            raise AgentWebError("radius must be between 10 and 10000 meters")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        payload, response = self._json(
            language,
            {"action": "query", "list": "geosearch", "gscoord": f"{latitude}|{longitude}", "gsradius": radius, "gslimit": limit, "gsnamespace": 0},
            cache_action="nearby",
            cache_arguments={"latitude": latitude, "longitude": longitude, "radius": radius, "limit": limit},
            cache_ttl=300,
        )
        rows = [{"page_id": item.get("pageid"), "title": item.get("title"), "latitude": item.get("lat"), "longitude": item.get("lon"), "distance_meters": item.get("dist"), "url": f"https://{language}.wikipedia.org/wiki/{quote(str(item.get('title', '')).replace(' ', '_'))}"} for item in payload.get("query", {}).get("geosearch", [])]
        return {"operation": "wikipedia.nearby", "center": {"latitude": latitude, "longitude": longitude}, "radius_meters": radius, "language": language, "count": len(rows), "pages": rows, "meta": self._meta(response)}

    def user_contributions(self, username: str, limit: int = 20, language: str = "en") -> dict[str, Any]:
        if not username.strip():
            raise AgentWebError("username cannot be empty")
        if limit < 1 or limit > 500:
            raise AgentWebError("limit must be between 1 and 500")
        payload, response = self._json(
            language,
            {"action": "query", "list": "usercontribs", "ucuser": username, "uclimit": limit, "ucprop": "ids|title|timestamp|comment|size|flags|tags"},
            cache_action="user_contributions",
            cache_arguments={"username": username, "limit": limit},
            cache_ttl=300,
        )
        rows = []
        for item in payload.get("query", {}).get("usercontribs", []):
            comment, truncated = bounded(item.get("comment"), 500)
            rows.append({"page_id": item.get("pageid"), "revision_id": item.get("revid"), "parent_id": item.get("parentid"), "title": item.get("title"), "timestamp": item.get("timestamp"), "size": item.get("size"), "new": bool(item.get("new")), "minor": bool(item.get("minor")), "top": bool(item.get("top")), "comment": comment, "comment_truncated": truncated, "tags": item.get("tags") or []})
        return {"operation": "wikipedia.user_contributions", "username": username, "language": language, "count": len(rows), "contributions": rows, "has_more": bool(payload.get("continue")), "meta": self._meta(response)}

    def sections(self, title: str, language: str = "en") -> dict[str, Any]:
        if not title.strip():
            raise AgentWebError("title cannot be empty")
        payload, response = self._json(
            language,
            {"action": "parse", "page": title, "prop": "sections", "redirects": 1},
            cache_action="sections",
            cache_arguments={"title": title},
            cache_ttl=900,
        )
        rows = [{"index": item.get("index"), "level": item.get("level"), "number": item.get("number"), "line": clean_html(item.get("line")), "anchor": item.get("anchor")} for item in payload.get("parse", {}).get("sections", [])]
        return {"operation": "wikipedia.sections", "title": payload.get("parse", {}).get("title", title), "language": language, "count": len(rows), "sections": rows, "meta": self._meta(response)}

    def categories(self, title: str, limit: int = 50, language: str = "en") -> dict[str, Any]:
        if not title.strip():
            raise AgentWebError("title cannot be empty")
        if limit < 1 or limit > 500:
            raise AgentWebError("limit must be between 1 and 500")
        payload, response = self._json(
            language,
            {"action": "query", "prop": "categories", "titles": title, "redirects": 1, "cllimit": limit, "clshow": "!hidden"},
            cache_action="categories",
            cache_arguments={"title": title, "limit": limit},
            cache_ttl=900,
        )
        pages = payload.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            raise AgentWebError(f"Wikipedia page {title!r} was not found")
        rows = [item.get("title", "").removeprefix("Category:") for item in pages[0].get("categories", [])]
        return {"operation": "wikipedia.categories", "title": pages[0].get("title"), "language": language, "count": len(rows), "categories": rows, "has_more": bool(payload.get("continue")), "meta": self._meta(response)}

    def images(self, title: str, limit: int = 50, language: str = "en") -> dict[str, Any]:
        if not title.strip():
            raise AgentWebError("title cannot be empty")
        if limit < 1 or limit > 500:
            raise AgentWebError("limit must be between 1 and 500")
        payload, response = self._json(
            language,
            {"action": "query", "prop": "images", "titles": title, "redirects": 1, "imlimit": limit},
            cache_action="images",
            cache_arguments={"title": title, "limit": limit},
            cache_ttl=900,
        )
        pages = payload.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            raise AgentWebError(f"Wikipedia page {title!r} was not found")
        rows = [{"title": item.get("title"), "description_url": f"https://{language}.wikipedia.org/wiki/{quote(str(item.get('title', '')).replace(' ', '_'))}"} for item in pages[0].get("images", [])]
        return {"operation": "wikipedia.images", "title": pages[0].get("title"), "language": language, "count": len(rows), "images": rows, "has_more": bool(payload.get("continue")), "meta": self._meta(response)}

    def pageviews(self, title: str, start: str, end: str, language: str = "en") -> dict[str, Any]:
        if not title.strip():
            raise AgentWebError("title cannot be empty")
        if not re.fullmatch(r"[0-9]{8}", start) or not re.fullmatch(r"[0-9]{8}", end) or start > end:
            raise AgentWebError(
                "start and end must be ordered YYYYMMDD dates, for example "
                "20260701 (not 2026-07-01)",
                code="invalid_input",
                field="start",
            )
        self._endpoint(language)
        article = quote(title.replace(" ", "_"), safe="")
        response = self.session().request(
            "GET",
            f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{language}.wikipedia/all-access/user/{article}/daily/{start}/{end}",
            headers=API_HEADERS,
            cache_action="pageviews",
            cache_arguments={"title": title, "start": start, "end": end, "language": language},
            cache_ttl=3600,
        )
        self._raise_http_error(response, "pageviews")
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise AgentWebError("Wikimedia returned malformed pageview data") from exc
        rows = payload.get("items", [])
        return {"operation": "wikipedia.pageviews", "title": title, "language": language, "start": start, "end": end, "total_views": sum(item.get("views") or 0 for item in rows), "count": len(rows), "days": [{"date": item.get("timestamp", "")[:8], "views": item.get("views")} for item in rows], "meta": self._meta(response)}

    def api_query(self, parameters: list[str] | dict[str, Any], language: str = "en", max_items: int = 50, max_string: int = 4000, max_total_chars: int = 20000) -> dict[str, Any]:
        params = parse_query_pairs(parameters)
        action = params.get("action", "query")
        if action not in {"query", "parse", "opensearch", "help"}:
            raise AgentWebError("api_query permits only read-only query, parse, opensearch, or help actions")
        params["action"] = action
        if max_items < 1 or max_items > 200 or max_string < 100 or max_string > 20000:
            raise AgentWebError("max_items or max_string is out of range")
        if max_total_chars < 1000 or max_total_chars > 100000:
            raise AgentWebError("max_total_chars must be between 1000 and 100000")
        payload, response = self._json(
            language,
            params,
            cache_action="api_query",
            cache_arguments={"parameters": parameters},
            cache_ttl=60,
        )
        data, truncated = bounded_data(payload, max_items=max_items, max_string=max_string)
        data, total_truncated, original_chars = enforce_data_budget(data, max_total_chars=max_total_chars)
        return {"operation": "wikipedia.api_query", "language": language, "parameters": params, "data": data, "truncated": truncated or total_truncated, "truncation": {"nested_limit": truncated, "character_budget": total_truncated}, "original_chars": original_chars, "max_total_chars": max_total_chars, "meta": self._meta(response)}

    def account_status(self, language: str = "en") -> dict[str, Any]:
        payload, response = self._json(
            language,
            {"action": "query", "meta": "userinfo", "uiprop": "rights|groups|blockinfo"},
            cache_action="account_status",
            cache_arguments={},
            cache_ttl=0,
        )
        user = payload.get("query", {}).get("userinfo", {})
        authenticated = not bool(user.get("anon")) and bool(user.get("name"))
        return {
            "operation": "wikipedia.account_status",
            "authenticated": authenticated,
            "username": user.get("name") if authenticated else None,
            "groups": user.get("groups") or [],
            "rights": user.get("rights") or [],
            "blocked": bool(user.get("blockedby")),
            "session": self.session_freshness(authenticated),
            "meta": self._meta(response),
        }

    def _csrf_token(self, language: str) -> str:
        payload, _response = self._json(
            language,
            {"action": "query", "meta": "tokens", "type": "csrf"},
            cache_action="csrf_token",
            cache_arguments={},
            cache_ttl=0,
        )
        token = payload.get("query", {}).get("tokens", {}).get("csrftoken")
        if not token or token == "+\\":
            raise AuthenticationRequired(
                "Wikipedia did not provide an authenticated CSRF token; run agentweb connect wikipedia"
            )
        return str(token)

    def api_request(
        self,
        parameters: list[str],
        language: str = "en",
        confirm: bool = False,
        max_items: int = 100,
        max_string: int = 10000,
        max_total_chars: int = 50000,
    ) -> dict[str, Any]:
        params = parse_query_pairs(parameters)
        action = str(params.get("action") or "query")
        read_only = action in {"query", "parse", "opensearch", "help", "compare", "expandtemplates"}
        if not read_only and not confirm:
            raise AgentWebError(
                "wikipedia.api_request changes remote state; inspect the inputs and repeat with confirm=true"
            )
        if max_items < 1 or max_items > 500 or max_string < 100 or max_string > 50000:
            raise AgentWebError("max_items or max_string is out of range")
        if max_total_chars < 1000 or max_total_chars > 250000:
            raise AgentWebError("max_total_chars must be between 1000 and 250000")
        params.update({"action": action, "format": "json", "formatversion": 2})
        method = "GET" if read_only else "POST"
        if not read_only:
            params.setdefault("assert", "user")
            params.setdefault("token", self._csrf_token(language))
        response = self.session().request(
            method,
            self._endpoint(language),
            params=params if read_only else None,
            form=params if not read_only else None,
            headers=API_HEADERS,
            cache_action="api_request" if read_only else None,
            cache_arguments={"language": language, "parameters": parameters},
            cache_ttl=60 if read_only else 0,
        )
        self._raise_http_error(response, "API")
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise AgentWebError("Wikipedia returned malformed JSON") from exc
        if payload.get("error"):
            code = payload["error"].get("code")
            info = payload["error"].get("info", payload["error"])
            if code in {"assertuserfailed", "notloggedin"}:
                raise AuthenticationRequired(
                    "Wikipedia session is not authenticated; run agentweb connect wikipedia"
                )
            raise AgentWebError(f"Wikipedia API error: {info}")
        data, truncated = bounded_data(payload, max_items=max_items, max_string=max_string)
        data, total_truncated, original_chars = enforce_data_budget(
            data, max_total_chars=max_total_chars
        )
        return {
            "operation": "wikipedia.api_request",
            "language": language,
            "action": action,
            "method": method,
            "state_changed": not read_only,
            "data": data,
            "truncated": truncated or total_truncated,
            "original_chars": original_chars,
            "meta": self._meta(response),
        }

    def edit(
        self,
        title: str,
        text: str,
        summary: str,
        language: str = "en",
        section: str | None = None,
        minor: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not title.strip() or not summary.strip():
            raise AgentWebError("title and summary cannot be empty")
        parameters = [
            "action=edit",
            f"title={title}",
            f"text={text}",
            f"summary={summary}",
        ]
        if section is not None:
            parameters.append(f"section={section}")
        if minor:
            parameters.append("minor=1")
        result = self.api_request(
            parameters,
            language=language,
            confirm=confirm,
            max_total_chars=50000,
        )
        result["operation"] = "wikipedia.edit"
        return result

    def upload(
        self,
        path: str,
        filename: str | None = None,
        comment: str = "Uploaded through AgentWeb",
        description: str | None = None,
        language: str = "en",
        ignore_warnings: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not confirm:
            raise AgentWebError(
                "wikipedia.upload changes remote state; inspect the inputs and repeat with confirm=true"
            )
        token = self._csrf_token(language)
        form: dict[str, Any] = {
            "action": "upload",
            "format": "json",
            "formatversion": 2,
            "assert": "user",
            "token": token,
            "comment": comment,
        }
        if description is not None:
            form["text"] = description
        if ignore_warnings:
            form["ignorewarnings"] = 1
        file_specification: dict[str, Any] = {"path": path, "field": "file"}
        if filename:
            file_specification["filename"] = filename
            form["filename"] = filename
        response = self.session().request(
            "POST",
            self._endpoint(language),
            form=form,
            files=[file_specification],
            headers=API_HEADERS,
            cache_ttl=0,
        )
        self._raise_http_error(response, "upload")
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise AgentWebError("Wikipedia returned malformed upload JSON") from exc
        if payload.get("error"):
            code = payload["error"].get("code")
            if code in {"assertuserfailed", "notloggedin"}:
                raise AuthenticationRequired(
                    "Wikipedia session is not authenticated; run agentweb connect wikipedia"
                )
            raise AgentWebError(
                f"Wikipedia upload error: {payload['error'].get('info', payload['error'])}"
            )
        upload = payload.get("upload") or {}
        return {
            "operation": "wikipedia.upload",
            "result": upload.get("result"),
            "filename": upload.get("filename") or filename,
            "image_info": upload.get("imageinfo"),
            "warnings": upload.get("warnings"),
            "state_changed": upload.get("result") == "Success",
            "local_path_exposed": False,
            "meta": self._meta(response),
        }
