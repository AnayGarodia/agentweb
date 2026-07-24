from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from agentweb.sdk import (
    AdapterContext,
    AuthenticationRequired,
    HttpSession,
    Response,
    SiteAdapter,
    AgentWebError,
    bounded_data,
    enforce_data_budget,
    parse_query_pairs,
)


ALGOLIA_URL = "https://hn.algolia.com/api/v1"
FIREBASE_URL = "https://hacker-news.firebaseio.com/v0"
HN_URL = "https://news.ycombinator.com"


def clean_html(value: str | None) -> str | None:
    if not value:
        return None
    text = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
    return text or None


def bounded_html(value: str | None, limit: int) -> tuple[str | None, bool]:
    text = clean_html(value)
    if text is None or len(text) <= limit:
        return text, False
    return text[: max(limit - 1, 0)].rstrip() + "…", True


def response_meta(response: Response) -> dict[str, Any]:
    return {
        "elapsed_ms": round(response.elapsed_ms, 1),
        "from_cache": response.from_cache,
        "url": response.url,
    }


class Adapter(SiteAdapter):
    site_name = "hn"
    base_url = HN_URL
    allowed_domains = ("news.ycombinator.com", "ycombinator.com")

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)

    def _json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        cache_action: str,
        cache_arguments: dict[str, Any],
        cache_ttl: int,
    ) -> tuple[Any, Response]:
        response = self.session().request(
            "GET",
            url,
            params=params,
            headers={"Accept": "application/json"},
            cache_action=cache_action,
            cache_arguments=cache_arguments,
            cache_ttl=cache_ttl,
        )
        if response.status >= 400:
            raise AgentWebError(
                f"Hacker News returned HTTP {response.status} for {response.url}"
            )
        try:
            return json.loads(response.text), response
        except json.JSONDecodeError as exc:
            raise AgentWebError("Hacker News returned malformed JSON") from exc

    @staticmethod
    def _story(item: dict[str, Any]) -> dict[str, Any]:
        item_id = int(item["id"])
        text, text_truncated = bounded_html(item.get("text"), 1000)
        return {
            "id": item_id,
            "type": item.get("type"),
            "title": item.get("title"),
            "url": item.get("url") or f"{HN_URL}/item?id={item_id}",
            "hn_url": f"{HN_URL}/item?id={item_id}",
            "author": item.get("by"),
            "score": item.get("score"),
            "comment_count": item.get("descendants", 0),
            "created_at_unix": item.get("time"),
            "text": text,
            "text_truncated": text_truncated,
        }

    def search(
        self,
        query: str,
        limit: int = 10,
        sort: str = "relevance",
        result_type: str = "all",
    ) -> dict[str, Any]:
        if not query.strip():
            raise AgentWebError("Search query cannot be empty")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        if sort not in {"relevance", "date"}:
            raise AgentWebError("sort must be relevance or date")
        if result_type not in {"all", "story", "comment"}:
            raise AgentWebError("result_type must be all, story, or comment")
        endpoint = "search_by_date" if sort == "date" else "search"
        params: dict[str, Any] = {"query": query, "hitsPerPage": limit}
        if result_type != "all":
            params["tags"] = result_type
        payload, response = self._json(
            f"{ALGOLIA_URL}/{endpoint}",
            params=params,
            cache_action="search",
            cache_arguments={
                "query": query,
                "limit": limit,
                "sort": sort,
                "result_type": result_type,
            },
            cache_ttl=60,
        )
        results = []
        for hit in payload.get("hits", []):
            item_id = int(hit["objectID"])
            kind = "comment" if "comment" in (hit.get("_tags") or []) else "story"
            text, text_truncated = bounded_html(
                hit.get("comment_text") or hit.get("story_text"), 500
            )
            results.append(
                {
                    "id": item_id,
                    "type": kind,
                    "title": hit.get("title") or hit.get("story_title"),
                    "url": hit.get("url") or hit.get("story_url"),
                    "hn_url": f"{HN_URL}/item?id={item_id}",
                    "author": hit.get("author"),
                    "points": hit.get("points"),
                    "comment_count": hit.get("num_comments"),
                    "created_at": hit.get("created_at"),
                    "created_at_unix": hit.get("created_at_i"),
                    "parent_id": hit.get("parent_id"),
                    "text": text,
                    "text_truncated": text_truncated,
                }
            )
        return {
            "operation": "hn.search",
            "query": query,
            "sort": sort,
            "result_type": result_type,
            "count": len(results),
            "results": results,
            "meta": response_meta(response),
        }

    def stories(self, kind: str = "top", limit: int = 10) -> dict[str, Any]:
        if kind not in {"top", "new", "best", "ask", "show", "job"}:
            raise AgentWebError("kind must be top, new, best, ask, show, or job")
        if limit < 1 or limit > 50:
            raise AgentWebError("limit must be between 1 and 50")
        ids, ids_response = self._json(
            f"{FIREBASE_URL}/{kind}stories.json",
            cache_action="story_ids",
            cache_arguments={"kind": kind},
            cache_ttl=30,
        )
        stories = []
        responses = [ids_response]
        for item_id in ids[:limit]:
            item, response = self._json(
                f"{FIREBASE_URL}/item/{int(item_id)}.json",
                cache_action="item",
                cache_arguments={"id": int(item_id)},
                cache_ttl=60,
            )
            responses.append(response)
            if item and not item.get("deleted") and not item.get("dead"):
                stories.append(self._story(item))
        return {
            "operation": "hn.stories",
            "kind": kind,
            "count": len(stories),
            "stories": stories,
            "meta": {
                "elapsed_ms": round(sum(item.elapsed_ms for item in responses), 1),
                "request_count": len(responses),
                "all_from_cache": all(item.from_cache for item in responses),
            },
        }

    @staticmethod
    def _comment(
        node: dict[str, Any], *, depth: int, max_depth: int, budget: list[int]
    ) -> dict[str, Any] | None:
        if budget[0] <= 0 or node.get("deleted") or node.get("dead"):
            return None
        budget[0] -= 1
        text, text_truncated = bounded_html(node.get("text"), 2000)
        value = {
            "id": node.get("id"),
            "author": node.get("author"),
            "created_at": node.get("created_at"),
            "created_at_unix": node.get("created_at_i"),
            "text": text,
            "text_truncated": text_truncated,
            "children": [],
        }
        if depth < max_depth:
            for child in node.get("children") or []:
                mapped = Adapter._comment(
                    child, depth=depth + 1, max_depth=max_depth, budget=budget
                )
                if mapped is not None:
                    value["children"].append(mapped)
                if budget[0] <= 0:
                    break
        return value

    def item(
        self, item_id: int, comment_limit: int = 20, comment_depth: int = 2
    ) -> dict[str, Any]:
        if item_id < 1:
            raise AgentWebError("item_id must be positive")
        if comment_limit < 0 or comment_limit > 100:
            raise AgentWebError("comment_limit must be between 0 and 100")
        if comment_depth < 0 or comment_depth > 10:
            raise AgentWebError("comment_depth must be between 0 and 10")
        payload, indexed_response = self._json(
            f"{ALGOLIA_URL}/items/{item_id}",
            cache_action="thread",
            cache_arguments={"id": item_id},
            cache_ttl=60,
        )
        live, live_response = self._json(
            f"{FIREBASE_URL}/item/{item_id}.json",
            cache_action="live_item",
            cache_arguments={"id": item_id},
            cache_ttl=15,
        )
        if not payload or payload.get("id") is None:
            raise AgentWebError(f"Hacker News item {item_id} was not found")
        if not live or live.get("deleted") or live.get("dead"):
            raise AgentWebError(f"Hacker News item {item_id} is unavailable or deleted")
        budget = [comment_limit]
        comments = []
        if comment_limit and comment_depth:
            for child in payload.get("children") or []:
                mapped = self._comment(
                    child, depth=1, max_depth=comment_depth, budget=budget
                )
                if mapped is not None:
                    comments.append(mapped)
                if budget[0] <= 0:
                    break
        item_text, item_text_truncated = bounded_html(live.get("text"), 10000)

        def count_comments(nodes: list[dict[str, Any]]) -> int:
            return sum(
                1 + count_comments(child.get("children") or []) for child in nodes
            )

        indexed_comment_count = count_comments(payload.get("children") or [])
        live_comment_count = live.get("descendants", 0)
        return {
            "operation": "hn.item",
            "item": {
                "id": live["id"],
                "type": live.get("type"),
                "title": live.get("title"),
                "url": live.get("url") or f"{HN_URL}/item?id={item_id}",
                "hn_url": f"{HN_URL}/item?id={item_id}",
                "author": live.get("by"),
                "points": live.get("score"),
                "created_at": payload.get("created_at"),
                "created_at_unix": live.get("time"),
                "text": item_text,
                "text_truncated": item_text_truncated,
                "total_comment_count": live_comment_count,
                "top_level_comment_count": len(payload.get("children") or []),
                "returned_comment_count": comment_limit - budget[0],
                "comments": comments,
                "live_fields_source": "official_firebase",
                "thread_source": "algolia_index",
                "indexed_points": payload.get("points"),
                "indexed_comment_count": indexed_comment_count,
                "upstream_lag_detected": (
                    live.get("score") != payload.get("points")
                    or live_comment_count != indexed_comment_count
                ),
            },
            "meta": {
                "elapsed_ms": round(
                    indexed_response.elapsed_ms + live_response.elapsed_ms, 1
                ),
                "request_count": 2,
                "all_from_cache": indexed_response.from_cache
                and live_response.from_cache,
            },
        }

    def user(self, username: str, submissions_limit: int = 20) -> dict[str, Any]:
        if not username.strip():
            raise AgentWebError("username cannot be empty")
        if submissions_limit < 0 or submissions_limit > 100:
            raise AgentWebError("submissions_limit must be between 0 and 100")
        payload, response = self._json(
            f"{FIREBASE_URL}/user/{username}.json",
            cache_action="user",
            cache_arguments={"username": username},
            cache_ttl=60,
        )
        if payload is None:
            raise AgentWebError(f"Hacker News user {username!r} was not found")
        submitted = payload.get("submitted") or []
        about, about_truncated = bounded_html(payload.get("about"), 5000)
        return {
            "operation": "hn.user",
            "user": {
                "username": payload.get("id"),
                "karma": payload.get("karma"),
                "created_at_unix": payload.get("created"),
                "about": about,
                "about_truncated": about_truncated,
                "submitted_count": len(submitted),
                "recent_submission_ids": submitted[:submissions_limit],
                "profile_url": f"{HN_URL}/user?id={payload.get('id')}",
            },
            "meta": response_meta(response),
        }

    def max_item(self) -> dict[str, Any]:
        item_id, response = self._json(
            f"{FIREBASE_URL}/maxitem.json",
            cache_action="max_item",
            cache_arguments={},
            cache_ttl=15,
        )
        return {
            "operation": "hn.max_item",
            "max_item_id": item_id,
            "item_url": f"{HN_URL}/item?id={item_id}",
            "meta": response_meta(response),
        }

    def updates(self, limit: int = 50) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        payload, response = self._json(
            f"{FIREBASE_URL}/updates.json",
            cache_action="updates",
            cache_arguments={},
            cache_ttl=15,
        )
        item_ids = (payload.get("items") or [])[:limit]
        profiles = (payload.get("profiles") or [])[:limit]
        return {
            "operation": "hn.updates",
            "item_count": len(item_ids),
            "item_ids": item_ids,
            "items": [{"id": item_id, "url": f"{HN_URL}/item?id={item_id}"} for item_id in item_ids],
            "profile_count": len(profiles),
            "profiles": [{"username": username, "url": f"{HN_URL}/user?id={username}"} for username in profiles],
            "meta": response_meta(response),
        }

    def user_activity(
        self,
        username: str,
        limit: int = 20,
        item_types: list[str] | None = None,
    ) -> dict[str, Any]:
        if not username.strip():
            raise AgentWebError("username cannot be empty")
        if limit < 1 or limit > 50:
            raise AgentWebError("limit must be between 1 and 50")
        allowed = {"story", "comment", "job", "poll", "pollopt"}
        requested = set(item_types or allowed)
        if not requested or not requested.issubset(allowed):
            raise AgentWebError("item_types may contain story, comment, job, poll, or pollopt")
        user, user_response = self._json(
            f"{FIREBASE_URL}/user/{username}.json",
            cache_action="user",
            cache_arguments={"username": username},
            cache_ttl=60,
        )
        if user is None:
            raise AgentWebError(f"Hacker News user {username!r} was not found")
        payload, activity_response = self._json(
            f"{ALGOLIA_URL}/search_by_date",
            params={
                "tags": f"author_{username}",
                "hitsPerPage": min(100, max(limit * 3, limit)),
            },
            cache_action="user_activity",
            cache_arguments={
                "username": username,
                "limit": limit,
                "item_types": sorted(requested),
            },
            cache_ttl=60,
        )
        rows = []
        inspected = 0
        for item in payload.get("hits", []):
            inspected += 1
            tags = set(item.get("_tags") or [])
            kind = next(
                (value for value in ("comment", "pollopt", "poll", "job") if value in tags),
                "story",
            )
            if kind not in requested:
                continue
            text, truncated = bounded_html(
                item.get("comment_text") or item.get("story_text"), 1000
            )
            item_id = int(item.get("objectID"))
            rows.append(
                {
                    "id": item_id,
                    "type": kind,
                    "title": item.get("title") or item.get("story_title"),
                    "url": item.get("url") or item.get("story_url") or f"{HN_URL}/item?id={item_id}",
                    "hn_url": f"{HN_URL}/item?id={item_id}",
                    "parent_id": item.get("parent_id"),
                    "score": item.get("points"),
                    "comment_count": item.get("num_comments"),
                    "created_at_unix": item.get("created_at_i"),
                    "created_at": item.get("created_at"),
                    "text": text,
                    "text_truncated": truncated,
                }
            )
            if len(rows) >= limit:
                break
        return {
            "operation": "hn.user_activity",
            "username": username,
            "item_types": sorted(requested),
            "count": len(rows),
            "submitted_ids_inspected": inspected,
            "activity": rows,
            "meta": {
                "elapsed_ms": round(user_response.elapsed_ms + activity_response.elapsed_ms, 1),
                "request_count": 2,
                "all_from_cache": user_response.from_cache and activity_response.from_cache,
            },
        }

    def story_comments(self, story_id: int, limit: int = 50, sort: str = "date") -> dict[str, Any]:
        if story_id < 1:
            raise AgentWebError("story_id must be positive")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        if sort not in {"date", "relevance"}:
            raise AgentWebError("sort must be date or relevance")
        endpoint = "search_by_date" if sort == "date" else "search"
        payload, response = self._json(
            f"{ALGOLIA_URL}/{endpoint}",
            params={"tags": f"comment,story_{story_id}", "hitsPerPage": limit},
            cache_action="story_comments",
            cache_arguments={"story_id": story_id, "limit": limit, "sort": sort},
            cache_ttl=60,
        )
        rows = []
        for item in payload.get("hits", []):
            text, truncated = bounded_html(item.get("comment_text"), 2000)
            item_id = int(item.get("objectID"))
            rows.append({"id": item_id, "story_id": story_id, "parent_id": item.get("parent_id"), "author": item.get("author"), "created_at": item.get("created_at"), "created_at_unix": item.get("created_at_i"), "text": text, "text_truncated": truncated, "hn_url": f"{HN_URL}/item?id={item_id}"})
        return {"operation": "hn.story_comments", "story_id": story_id, "sort": sort, "count": len(rows), "comments": rows, "meta": response_meta(response)}

    def api_get(self, source: str, path: str, query: list[str] | None = None, max_items: int = 50, max_string: int = 4000, max_total_chars: int = 20000) -> dict[str, Any]:
        if source not in {"firebase", "algolia"}:
            raise AgentWebError("source must be firebase or algolia")
        if not path.startswith("/") or "://" in path or ".." in path.split("/"):
            raise AgentWebError("path must be a safe API path beginning with /")
        if max_items < 1 or max_items > 200 or max_string < 100 or max_string > 20000:
            raise AgentWebError("max_items or max_string is out of range")
        if max_total_chars < 1000 or max_total_chars > 100000:
            raise AgentWebError("max_total_chars must be between 1000 and 100000")
        requested_path = path
        if source == "firebase" and path.startswith("/v0/"):
            path = path[3:]
        if source == "algolia" and path.startswith("/api/v1/"):
            path = path[7:]
        if source == "firebase" and not path.endswith(".json"):
            raise AgentWebError("Firebase paths must end in .json")
        params = parse_query_pairs(query)
        base = FIREBASE_URL if source == "firebase" else ALGOLIA_URL
        payload, response = self._json(
            base + path,
            params=params,
            cache_action="api_get",
            cache_arguments={"source": source, "path": path, "query": query or []},
            cache_ttl=30,
        )
        data, truncated = bounded_data(payload, max_items=max_items, max_string=max_string)
        data, total_truncated, original_chars = enforce_data_budget(
            data, max_total_chars=max_total_chars
        )
        return {"operation": "hn.api_get", "source": source, "requested_path": requested_path, "path": path, "query": params, "data": data, "truncated": truncated or total_truncated, "truncation": {"nested_limit": truncated, "character_budget": total_truncated}, "original_chars": original_chars, "max_total_chars": max_total_chars, "meta": response_meta(response)}

    def _html(self, path_or_url: str) -> tuple[BeautifulSoup, Response]:
        url = urljoin(HN_URL + "/", path_or_url)
        if (urlparse(url).hostname or "").lower() != "news.ycombinator.com":
            raise AgentWebError("Hacker News action escaped news.ycombinator.com")
        response = self.session().request(
            "GET",
            url,
            headers={"Accept": "text/html"},
            cache_ttl=0,
        )
        if response.status >= 400:
            raise AgentWebError(f"Hacker News returned HTTP {response.status}")
        return BeautifulSoup(response.text, "html.parser"), response

    @staticmethod
    def _identity(soup: BeautifulSoup) -> str | None:
        logout = soup.select_one('a[href^="logout?"]')
        if logout:
            previous = logout.find_previous("a", href=lambda value: bool(value and value.startswith("user?id=")))
            if previous:
                return previous.get_text(" ", strip=True) or None
        return None

    def account_status(self) -> dict[str, Any]:
        soup, response = self._html("/news")
        username = self._identity(soup)
        return {
            "operation": "hn.account_status",
            "authenticated": bool(username),
            "username": username,
            "session": self.session_freshness(bool(username)),
            "meta": response_meta(response),
        }

    def _authenticated_page(self, item_id: int) -> tuple[BeautifulSoup, Response]:
        if item_id < 1:
            raise AgentWebError("item_id must be positive")
        soup, response = self._html(f"/item?id={item_id}")
        if not self._identity(soup):
            raise AuthenticationRequired(
                "Hacker News session is not authenticated; run agentweb connect hn"
            )
        return soup, response

    def _confirmed_link(
        self, href: str, operation: str, confirm: bool
    ) -> tuple[BeautifulSoup, Response]:
        if not confirm:
            raise AgentWebError(
                f"{operation} changes remote state; inspect the inputs and repeat with confirm=true"
            )
        return self._html(href)

    def vote(self, item_id: int, direction: str = "up", confirm: bool = False) -> dict[str, Any]:
        if direction not in {"up", "down", "unvote"}:
            raise AgentWebError("direction must be up, down, or unvote")
        soup, preflight = self._authenticated_page(item_id)
        if direction == "unvote":
            link = soup.find("a", href=lambda value: bool(value and value.startswith(f"vote?id={item_id}&how=un")))
        else:
            link = soup.select_one(f"a#{direction}_{item_id}")
        href = link.get("href") if link else None
        if not href:
            raise AgentWebError(
                f"Hacker News did not offer a {direction} vote for item {item_id}; it may be your item, already voted, or ineligible"
            )
        _soup, response = self._confirmed_link(str(href), "hn.vote", confirm)
        return {
            "operation": "hn.vote",
            "item_id": item_id,
            "direction": direction,
            "state_changed": True,
            "verified": response.status < 400,
            "meta": {
                "request_count": 2,
                "elapsed_ms": round(preflight.elapsed_ms + response.elapsed_ms, 1),
                "url": f"{HN_URL}/item?id={item_id}",
            },
        }

    def favorite(self, item_id: int, enabled: bool = True, confirm: bool = False) -> dict[str, Any]:
        soup, preflight = self._authenticated_page(item_id)
        expected_text = "favorite" if enabled else "un-favorite"
        # HN currently renders “un‑favorite” with a non-breaking Unicode
        # hyphen. Match the action URL, which is the actual protocol contract,
        # instead of brittle presentation text.
        def is_requested_action(value: str | None) -> bool:
            if not value:
                return False
            parsed = urlparse(value)
            if not parsed.path.endswith("fave"):
                return False
            query = parse_qs(parsed.query)
            if query.get("id") != [str(item_id)]:
                return False
            is_unfavorite = query.get("un") == ["t"]
            return is_unfavorite is (not enabled)

        def is_opposite_action(value: str | None) -> bool:
            if not value:
                return False
            parsed = urlparse(value)
            if not parsed.path.endswith("fave"):
                return False
            query = parse_qs(parsed.query)
            if query.get("id") != [str(item_id)]:
                return False
            is_unfavorite = query.get("un") == ["t"]
            return is_unfavorite is enabled

        link = soup.find("a", href=is_requested_action)
        href = link.get("href") if link else None
        if not href:
            if soup.find("a", href=is_opposite_action):
                return {
                    "operation": "hn.favorite",
                    "item_id": item_id,
                    "favorite": enabled,
                    "state_changed": False,
                    "already_in_requested_state": True,
                    "verified": True,
                    "meta": {
                        "request_count": 1,
                        "elapsed_ms": round(preflight.elapsed_ms, 1),
                        "url": f"{HN_URL}/item?id={item_id}",
                    },
                }
            raise AgentWebError(
                f"Hacker News did not expose enough state to verify {expected_text!r} for item {item_id}"
            )
        _soup, response = self._confirmed_link(str(href), "hn.favorite", confirm)
        verification_soup, verification_response = self._authenticated_page(item_id)
        verified = verification_soup.find("a", href=is_opposite_action) is not None
        if not verified:
            raise AgentWebError(
                f"Hacker News accepted the request but item {item_id} did not reach the requested favorite state"
            )
        return {
            "operation": "hn.favorite",
            "item_id": item_id,
            "favorite": enabled,
            "state_changed": True,
            "verified": True,
            "meta": {
                "request_count": 3,
                "elapsed_ms": round(
                    preflight.elapsed_ms
                    + response.elapsed_ms
                    + verification_response.elapsed_ms,
                    1,
                ),
                "url": f"{HN_URL}/item?id={item_id}",
            },
        }

    def comment(self, parent_id: int, text: str, confirm: bool = False) -> dict[str, Any]:
        if not text.strip():
            raise AgentWebError("text cannot be empty")
        # Resolve existence on the public API before testing authentication so a
        # missing item is not misreported as a login problem.
        self.item(parent_id, comment_limit=0)
        soup, preflight = self._authenticated_page(parent_id)
        form = soup.select_one('form[action="comment"]')
        if form is None:
            reply = soup.find(
                "a",
                href=lambda value: bool(value and value.startswith(f"reply?id={parent_id}&")),
            )
            if not reply or not reply.get("href"):
                raise AgentWebError(f"Hacker News did not offer a comment or reply form for {parent_id}")
            soup, reply_response = self._html(str(reply.get("href")))
            preflight.elapsed_ms += reply_response.elapsed_ms
            form = soup.select_one('form[action="comment"]')
        if form is None:
            raise AgentWebError("Hacker News reply page did not contain a comment form")
        fields = {
            str(node.get("name")): str(node.get("value") or "")
            for node in form.select('input[type="hidden"][name]')
        }
        if not fields.get("parent"):
            fields["parent"] = str(parent_id)
        fields["text"] = text
        if not confirm:
            raise AgentWebError(
                "hn.comment changes remote state; inspect the inputs and repeat with confirm=true"
            )
        response = self.session().request(
            "POST",
            HN_URL + "/comment",
            form=fields,
            headers={"Referer": preflight.url, "Accept": "text/html"},
            cache_ttl=0,
        )
        if response.status >= 400:
            raise AgentWebError(f"Hacker News comment returned HTTP {response.status}")
        comment_id = self._find_own_comment_id(parent_id, text)
        return {
            "operation": "hn.comment",
            "parent_id": parent_id,
            "comment_id": comment_id,
            "state_changed": True,
            "submitted": True,
            "text_exposed": False,
            "meta": response_meta(response),
        }

    def _find_own_comment_id(self, parent_id: int, text: str) -> int | None:
        """The id of the just-posted comment, located by author + text prefix.

        Returns ``None`` when the new comment cannot be identified (e.g. the
        page has not caught up yet); the submission itself already succeeded.
        """
        try:
            soup, _response = self._html(f"/item?id={parent_id}")
        except AgentWebError:
            return None
        me = self._identity(soup)
        if not me:
            return None
        prefix = " ".join(text.split())[:80].lower()
        best: int | None = None
        for row in soup.select("tr.athing.comtr"):
            author = row.select_one("a.hnuser")
            body = row.select_one("div.commtext")
            row_id = str(row.get("id") or "")
            if author is None or body is None or not row_id.isdigit():
                continue
            if author.get_text(strip=True) != me:
                continue
            body_text = " ".join(body.get_text(" ", strip=True).split()).lower()
            if prefix and not body_text.startswith(prefix):
                continue
            best = max(best or 0, int(row_id))
        return best

    def _submit_form(
        self,
        form: Any,
        fields: dict[str, Any],
        operation: str,
        confirm: bool,
    ) -> Response:
        if not confirm:
            raise AgentWebError(
                f"{operation} changes remote state; inspect the inputs and repeat with confirm=true"
            )
        hidden = {
            str(node.get("name")): str(node.get("value") or "")
            for node in form.select('input[name]')
            if node.get("type") not in {"submit", "button"}
        }
        hidden.update({key: value for key, value in fields.items() if value is not None})
        action = str(form.get("action") or "/r")
        url = urljoin(HN_URL + "/", action)
        if (urlparse(url).hostname or "").lower() != "news.ycombinator.com":
            raise AgentWebError("Hacker News form escaped news.ycombinator.com")
        response = self.session().request(
            "POST", url, form=hidden, headers={"Accept": "text/html"}, cache_ttl=0
        )
        if response.status >= 400:
            raise AgentWebError(f"{operation} returned HTTP {response.status}")
        return response

    def submit(
        self,
        title: str,
        url: str | None = None,
        text: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not title.strip() or bool(url) == bool(text):
            raise AgentWebError("title is required and exactly one of url or text must be provided")
        status = self.account_status()
        if not status["authenticated"]:
            raise AuthenticationRequired(
                "Hacker News session is not authenticated; run agentweb connect hn"
            )
        soup, preflight = self._html("/submit")
        form = soup.select_one("form")
        if form is None:
            raise AgentWebError("Hacker News submission page did not contain a form")
        response = self._submit_form(
            form,
            {"title": title, "url": url or "", "text": text or ""},
            "hn.submit",
            confirm,
        )
        return {
            "operation": "hn.submit",
            "kind": "link" if url else "text",
            "state_changed": True,
            "submitted": True,
            "content_exposed": False,
            "meta": {
                "request_count": 2,
                "elapsed_ms": round(preflight.elapsed_ms + response.elapsed_ms, 1),
                "url": self._redacted_url(response.url),
            },
        }

    def flag(self, item_id: int, enabled: bool = True, confirm: bool = False) -> dict[str, Any]:
        soup, preflight = self._authenticated_page(item_id)
        prefix = "flag" if enabled else "unflag"
        link = soup.find(
            "a",
            href=lambda value: bool(value and value.startswith(f"{prefix}?id={item_id}")),
        )
        if not link or not link.get("href"):
            raise AgentWebError(
                f"Hacker News did not offer {prefix!r} for item {item_id}; it may already be in the requested state or ineligible"
            )
        _soup, response = self._confirmed_link(str(link.get("href")), "hn.flag", confirm)
        return {
            "operation": "hn.flag",
            "item_id": item_id,
            "flagged": enabled,
            "state_changed": True,
            "verified": response.status < 400,
            "meta": {
                "request_count": 2,
                "elapsed_ms": round(preflight.elapsed_ms + response.elapsed_ms, 1),
                "url": f"{HN_URL}/item?id={item_id}",
            },
        }

    def edit(
        self,
        item_id: int,
        text: str | None = None,
        title: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if text is None and title is None:
            raise AgentWebError("text or title must be provided")
        soup, preflight = self._authenticated_page(item_id)
        link = soup.find(
            "a", href=lambda value: bool(value and value.startswith(f"edit?id={item_id}"))
        )
        if not link or not link.get("href"):
            raise AgentWebError(f"Hacker News did not offer editing for item {item_id}")
        edit_soup, edit_page = self._html(str(link.get("href")))
        form = edit_soup.select_one("form")
        if form is None:
            raise AgentWebError("Hacker News edit page did not contain a form")
        response = self._submit_form(
            form,
            {"text": text, "title": title},
            "hn.edit",
            confirm,
        )
        return {
            "operation": "hn.edit",
            "item_id": item_id,
            "state_changed": True,
            "updated": True,
            "content_exposed": False,
            "meta": {
                "request_count": 3,
                "elapsed_ms": round(
                    preflight.elapsed_ms + edit_page.elapsed_ms + response.elapsed_ms, 1
                ),
                "url": f"{HN_URL}/item?id={item_id}",
            },
        }

    def delete(self, item_id: int, confirm: bool = False) -> dict[str, Any]:
        # The item page's `delete?id=...` anchor only leads to the confirmation
        # page; following it never deletes anything. The real deletion is the
        # delete-confirm form POST, and success is verified by re-reading the
        # item page afterwards.
        soup, preflight = self._authenticated_page(item_id)
        offered = soup.find(
            "a", href=lambda value: bool(value and value.startswith(f"delete?id={item_id}"))
        )
        confirm_soup, confirm_page = self._html(f"/delete-confirm?id={item_id}")
        form = confirm_soup.select_one("form")
        if form is None:
            if offered is None:
                raise AgentWebError(
                    f"Hacker News did not offer deletion for item {item_id}"
                )
            raise AgentWebError(
                f"Hacker News delete-confirm page had no form for item {item_id}"
            )
        response = self._submit_form(form, {}, "hn.delete", confirm)
        preflight.elapsed_ms += confirm_page.elapsed_ms
        verify_soup, verify_response = self._html(f"/item?id={item_id}")
        still_deletable = verify_soup.find(
            "a", href=lambda value: bool(value and value.startswith(f"delete?id={item_id}"))
        )
        deleted = response.status < 400 and still_deletable is None
        if not deleted:
            raise AgentWebError(
                f"Hacker News did not delete item {item_id}; it is still live "
                "with a delete link offered. No state was changed."
            )
        return {
            "operation": "hn.delete",
            "item_id": item_id,
            "state_changed": True,
            "deleted": True,
            "verified": True,
            "meta": {
                "request_count": 4,
                "elapsed_ms": round(
                    preflight.elapsed_ms
                    + response.elapsed_ms
                    + verify_response.elapsed_ms,
                    1,
                ),
                "url": f"{HN_URL}/item?id={item_id}",
            },
        }
