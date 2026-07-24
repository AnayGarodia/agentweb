from __future__ import annotations

import json
import re
import time
from datetime import datetime
from html import unescape
from typing import Any
from urllib.parse import quote

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
from agentweb.storage import read_json, write_json


API_URL = "https://api.stackexchange.com/2.3"


def clean_html(value: str | None) -> str | None:
    if not value:
        return None
    text = (
        BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
        if "<" in value
        else unescape(value).strip()
    )
    return text or None


def bounded_html(value: str | None, limit: int) -> tuple[str | None, bool]:
    text = clean_html(value)
    if text is None or len(text) <= limit:
        return text, False
    return text[: max(limit - 1, 0)].rstrip() + "…", True


class Adapter(SiteAdapter):
    site_name = "stackoverflow"
    base_url = "https://stackoverflow.com"
    allowed_domains = ("stackoverflow.com", "stackauth.com", "stackexchange.com")

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)
        self.backoff_path = (
            context.paths.profile_dir("stackoverflow", context.profile)
            / "api-backoff.json"
        )

    def _json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        cache_action: str,
        cache_arguments: dict[str, Any],
        cache_ttl: int,
    ) -> tuple[dict[str, Any], Response]:
        state = read_json(self.backoff_path, {}) or {}
        blocked_until = float(state.get("until") or 0)
        if blocked_until > time.time():
            remaining = max(1, int(blocked_until - time.time()))
            raise AgentWebError(
                f"Stack Exchange requested backoff; retry after {remaining} seconds",
                code="stackoverflow_backoff",
                retryable=True,
                details={
                    "retry_after_seconds": remaining,
                    "retry_at_unix": blocked_until,
                },
            )
        response = self.session().request(
            "GET",
            API_URL + path,
            params={"site": "stackoverflow", **(params or {})},
            headers={"Accept": "application/json"},
            cache_action=cache_action,
            cache_arguments=cache_arguments,
            cache_ttl=cache_ttl,
        )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise AgentWebError("Stack Exchange returned malformed JSON") from exc
        if payload.get("backoff"):
            delay = max(1, int(payload["backoff"]))
            write_json(
                self.backoff_path,
                {"until": time.time() + delay, "retry_after_seconds": delay},
            )
        if response.status >= 400 or payload.get("error_id"):
            raise AgentWebError(
                f"Stack Exchange API error: {payload.get('error_message', response.status)}",
                code="stackoverflow_rate_limited"
                if response.status in {429, 502}
                else "stackoverflow_api_error",
                retryable=response.status in {408, 429, 502} or response.status >= 500,
                details={
                    "status": response.status,
                    "quota_remaining": payload.get("quota_remaining"),
                    "backoff_seconds": payload.get("backoff"),
                },
            )
        return payload, response

    @staticmethod
    def _meta(payload: dict[str, Any], response: Response) -> dict[str, Any]:
        return {
            "elapsed_ms": round(response.elapsed_ms, 1),
            "from_cache": response.from_cache,
            "url": response.url,
            "quota_remaining": payload.get("quota_remaining"),
            "quota_max": payload.get("quota_max"),
            "backoff_seconds": payload.get("backoff"),
            "has_more": payload.get("has_more"),
        }

    @staticmethod
    def _question_summary(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "question_id": row.get("question_id"),
            "title": clean_html(row.get("title")),
            "url": row.get("link"),
            "author": clean_html((row.get("owner") or {}).get("display_name")),
            "tags": row.get("tags") or [],
            "score": row.get("score"),
            "answer_count": row.get("answer_count"),
            "is_answered": row.get("is_answered"),
            "accepted_answer_id": row.get("accepted_answer_id"),
            "view_count": row.get("view_count"),
            "created_at_unix": row.get("creation_date"),
            "last_activity_at_unix": row.get("last_activity_date"),
        }

    @staticmethod
    def _website_number(value: str | None) -> int | None:
        if not value:
            return None
        normalized = value.strip().lower().replace(",", "")
        match = re.search(r"-?\d+(?:\.\d+)?", normalized)
        if not match:
            return None
        number = float(match.group(0))
        if "m" in normalized:
            number *= 1_000_000
        elif "k" in normalized:
            number *= 1_000
        return int(number)

    @staticmethod
    def _website_timestamp(value: str | None) -> int | None:
        if not value:
            return None
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None

    def _website_question_summary(self, row: Any) -> dict[str, Any] | None:
        identity = str(row.get("data-post-id") or "")
        if not identity.isdigit():
            identity = str(row.get("id") or "").removeprefix("question-summary-")
        link = row.select_one('a[itemprop="url"], h3 a.s-link')
        if not identity.isdigit() or not link or not link.get("href"):
            return None
        score = row.select_one('[itemprop="upvoteCount"]')
        answers = row.select_one('[itemprop="answerCount"]')
        stats = row.select(".s-post-summary--stats-item")
        views = next(
            (
                item.select_one(".s-post-summary--stats-item-number")
                for item in stats
                if "view" in item.get_text(" ", strip=True).lower()
            ),
            None,
        )
        author = row.select_one('[itemprop="author"] [itemprop="name"]')
        created = row.select_one('meta[itemprop="dateCreated"]')
        modified = row.select_one('meta[itemprop="dateModified"]')
        answer_count = self._website_number(
            answers.get("content")
            if answers and answers.name == "meta"
            else answers.get_text(" ", strip=True)
            if answers
            else None
        )
        return {
            "question_id": int(identity),
            "title": link.get_text(" ", strip=True),
            "url": self._direct_url(str(link.get("href"))),
            "author": author.get_text(" ", strip=True) if author else None,
            "tags": [
                item.get_text(" ", strip=True) for item in row.select('a[rel="tag"]')
            ],
            "score": self._website_number(
                score.get_text(" ", strip=True) if score else None
            ),
            "answer_count": answer_count,
            "is_answered": bool(answer_count),
            "accepted_answer_id": None,
            "view_count": self._website_number(
                views.get_text(" ", strip=True) if views else None
            ),
            "created_at_unix": self._website_timestamp(
                created.get("content") if created else None
            ),
            "last_activity_at_unix": self._website_timestamp(
                modified.get("content") if modified else None
            ),
            "excerpt": (row.select_one('[itemprop="text"]') or row).get_text(
                " ", strip=True
            )[:500],
        }

    @staticmethod
    def _api_read_unavailable(error: Exception) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "too many requests",
                "quota",
                "malformed json",
                "temporarily unavailable",
                "throttle",
                "http 429",
                "http 502",
                "http 503",
            )
        )

    def _website_questions(
        self,
        path: str,
        *,
        query: dict[str, Any] | None,
        limit: int,
        cache_action: str,
        cache_arguments: dict[str, Any],
        cache_ttl: int,
    ) -> tuple[list[dict[str, Any]], Response]:
        response = self.session().request(
            "GET",
            self._direct_url(path),
            params=query or {},
            headers={"Accept": "text/html,application/xhtml+xml"},
            cache_action=cache_action,
            cache_arguments=cache_arguments,
            cache_ttl=cache_ttl,
        )
        soup = BeautifulSoup(response.text, "html.parser")
        if response.status >= 400:
            raise AgentWebError(f"Stack Overflow returned HTTP {response.status}")
        if "/nocaptcha" in response.url or soup.select_one("#nocaptcha-form"):
            raise AgentWebError(
                "Stack Overflow requires human verification for this public page. "
                "The agent will not open a browser; run `agentweb connect stackoverflow "
                "--mode session` once, complete the checkpoint, and retry.",
                code="human_verification_required",
                retryable=True,
            )
        rows: list[dict[str, Any]] = []
        for node in soup.select('[id^="question-summary-"]'):
            summary = self._website_question_summary(node)
            if summary:
                rows.append(summary)
            if len(rows) >= limit:
                break
        if not rows:
            raise AgentWebError(
                "Stack Overflow changed its question-list page; no summaries were found"
            )
        return rows, response

    @staticmethod
    def _website_meta(response: Response, *, source: str) -> dict[str, Any]:
        return {
            "elapsed_ms": round(response.elapsed_ms, 1),
            "from_cache": response.from_cache,
            "url": response.url,
            "request_count": 1,
            "quota_bypassed": True,
            "quota_remaining": None,
            "source": source,
        }

    def _website_question_page(
        self, question_id: int, *, answers_limit: int
    ) -> tuple[dict[str, Any], Response]:
        response = self.session().request(
            "GET",
            f"https://stackoverflow.com/questions/{question_id}",
            headers={"Accept": "text/html,application/xhtml+xml"},
            cache_action="website_question",
            cache_arguments={
                "question_id": question_id,
                "answers_limit": answers_limit,
            },
            cache_ttl=300,
        )
        soup = BeautifulSoup(response.text, "html.parser")
        question = soup.select_one(f'.question[data-questionid="{question_id}"]')
        title = soup.select_one("h1 a.question-hyperlink")
        if response.status == 404 or question is None or title is None:
            raise AgentWebError(f"Stack Overflow question {question_id} was not found")

        def post_author(node: Any) -> str | None:
            owner = node.select_one(".post-signature.owner .user-details a")
            if owner is None:
                owner = node.select_one(
                    ".post-signature:not(.mod-info) .user-details a[href^='/users/']"
                )
            return clean_html(owner.get_text(" ", strip=True)) if owner else None

        def post_score(node: Any) -> int | None:
            score = node.select_one(".js-vote-count")
            return self._website_number(
                str(score.get("data-value") or score.get_text(" ", strip=True))
                if score
                else None
            )

        body_node = question.select_one(".js-post-body")
        body, body_truncated = bounded_html(str(body_node) if body_node else None, 6000)
        created = question.select_one('time[itemprop="dateCreated"], time')
        answer_rows = []
        for answer in soup.select(".answer[data-answerid]")[:answers_limit]:
            answer_body_node = answer.select_one(".js-post-body")
            answer_body, truncated = bounded_html(
                str(answer_body_node) if answer_body_node else None, 4000
            )
            created_node = answer.select_one('time[itemprop="dateCreated"], time')
            identity = str(answer.get("data-answerid") or "")
            answer_rows.append(
                {
                    "answer_id": int(identity) if identity.isdigit() else None,
                    "question_id": question_id,
                    "author": post_author(answer),
                    "score": post_score(answer),
                    "is_accepted": "accepted-answer" in (answer.get("class") or []),
                    "created_at_unix": self._website_timestamp(
                        str(created_node.get("datetime"))
                        if created_node and created_node.get("datetime")
                        else None
                    ),
                    "last_activity_at_unix": None,
                    "body": answer_body,
                    "body_truncated": truncated,
                }
            )
        accepted = next(
            (item["answer_id"] for item in answer_rows if item["is_accepted"]), None
        )
        view_meta = soup.select_one('meta[itemprop="interactionCount"]')
        result = {
            "question_id": question_id,
            "title": title.get_text(" ", strip=True),
            "url": response.url,
            "author": post_author(question),
            "tags": [
                tag.get_text(" ", strip=True) for tag in question.select('a[rel="tag"]')
            ],
            "score": post_score(question),
            "answer_count": len(soup.select(".answer[data-answerid]")),
            "is_answered": bool(soup.select_one(".accepted-answer")),
            "accepted_answer_id": accepted,
            "view_count": self._website_number(
                str(view_meta.get("content"))
                if view_meta and view_meta.get("content")
                else None
            ),
            "created_at_unix": self._website_timestamp(
                str(created.get("datetime"))
                if created and created.get("datetime")
                else None
            ),
            "last_activity_at_unix": None,
            "body": body,
            "body_truncated": body_truncated,
            "returned_answer_count": len(answer_rows),
            "answers": answer_rows,
        }
        return result, response

    def search(
        self,
        query: str,
        limit: int = 10,
        sort: str = "relevance",
        tagged: list[str] | None = None,
        accepted_only: bool = False,
    ) -> dict[str, Any]:
        if not query.strip():
            raise AgentWebError("query cannot be empty")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        if sort not in {"relevance", "votes", "creation", "activity"}:
            raise AgentWebError("sort must be relevance, votes, creation, or activity")
        params: dict[str, Any] = {
            "q": query,
            "pagesize": limit,
            "sort": sort,
            "order": "desc",
        }
        if accepted_only:
            params["accepted"] = "true"
        if tagged:
            params["tagged"] = ";".join(tagged)
        cache_arguments = {
            "query": query,
            "limit": limit,
            "sort": sort,
            "tagged": tagged or [],
            "accepted_only": accepted_only,
        }
        try:
            payload, response = self._json(
                "/search/advanced",
                params=params,
                cache_action="search",
                cache_arguments=cache_arguments,
                cache_ttl=120,
            )
        except AgentWebError as error:
            if not self._api_read_unavailable(error):
                raise
            tab = {
                "relevance": "Relevance",
                "votes": "Votes",
                "creation": "Newest",
                "activity": "Active",
            }[sort]
            website_query = {"q": query, "tab": tab, "pagesize": min(limit, 50)}
            if tagged:
                website_query["q"] = (
                    query + " " + " ".join(f"[{tag}]" for tag in tagged)
                )
            results, website_response = self._website_questions(
                "/search",
                query=website_query,
                limit=limit,
                cache_action="website_search",
                cache_arguments=cache_arguments,
                cache_ttl=120,
            )
            if accepted_only:
                results = [item for item in results if item.get("accepted_answer_id")]
            return {
                "operation": "stackoverflow.search",
                "query": query,
                "sort": sort,
                "tags": tagged or [],
                "count": len(results),
                "results": results,
                "ranking": {"method": "website_order", "exact_website_order": True},
                "meta": self._website_meta(
                    website_response, source="website_quota_fallback"
                ),
            }
        results = [self._question_summary(row) for row in payload.get("items", [])]
        return {
            "operation": "stackoverflow.search",
            "query": query,
            "sort": sort,
            "tags": tagged or [],
            "count": len(results),
            "results": results,
            "meta": self._meta(payload, response),
        }

    def questions(
        self,
        limit: int = 10,
        sort: str = "hot",
        tagged: list[str] | None = None,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        if sort not in {"hot", "votes", "creation", "activity"}:
            raise AgentWebError("sort must be hot, votes, creation, or activity")
        if sort == "hot":
            tag_path = "/tagged/" + quote("+".join(tagged), safe="+") if tagged else ""
            website_response = self.session().request(
                "GET",
                f"https://stackoverflow.com/questions{tag_path}",
                params={"tab": "Trending", "pagesize": min(50, max(limit, 15))},
                cache_action="website_trending",
                cache_arguments={"limit": limit, "tagged": tagged or []},
                cache_ttl=60,
            )
            if website_response.status >= 400:
                raise AgentWebError(
                    f"Stack Overflow trending page returned HTTP {website_response.status}"
                )
            soup = BeautifulSoup(website_response.text, "html.parser")
            results = []
            for row in soup.select('[id^="question-summary-"]'):
                summary = self._website_question_summary(row)
                if summary:
                    results.append(summary)
                if len(results) >= limit:
                    break
            if not results:
                raise AgentWebError(
                    "Stack Overflow changed its trending page; no question summaries were found"
                )
            return {
                "operation": "stackoverflow.questions",
                "sort": sort,
                "tags": tagged or [],
                "count": len(results),
                "questions": results,
                "ranking": {
                    "method": "website_trending_order",
                    "exact_website_order": True,
                    "source": website_response.url,
                },
                "meta": {
                    "elapsed_ms": round(website_response.elapsed_ms, 1),
                    "request_count": 1,
                    "website_url": website_response.url,
                    "quota_bypassed": True,
                    "quota_remaining": None,
                },
            }
        params: dict[str, Any] = {"pagesize": limit, "sort": sort, "order": "desc"}
        if tagged:
            params["tagged"] = ";".join(tagged)
        cache_arguments = {"limit": limit, "sort": sort, "tagged": tagged or []}
        try:
            payload, response = self._json(
                "/questions",
                params=params,
                cache_action="questions",
                cache_arguments=cache_arguments,
                cache_ttl=60,
            )
        except AgentWebError as error:
            if not self._api_read_unavailable(error):
                raise
            tab = {"votes": "Votes", "creation": "Newest", "activity": "Active"}[sort]
            tag_path = (
                "/questions/tagged/" + quote("+".join(tagged), safe="+")
                if tagged
                else "/questions"
            )
            results, website_response = self._website_questions(
                tag_path,
                query={"tab": tab, "pagesize": min(limit, 50)},
                limit=limit,
                cache_action="website_questions",
                cache_arguments=cache_arguments,
                cache_ttl=60,
            )
            return {
                "operation": "stackoverflow.questions",
                "sort": sort,
                "tags": tagged or [],
                "count": len(results),
                "questions": results,
                "ranking": {"method": "website_order", "exact_website_order": True},
                "meta": self._website_meta(
                    website_response, source="website_quota_fallback"
                ),
            }
        results = [self._question_summary(row) for row in payload.get("items", [])]
        return {
            "operation": "stackoverflow.questions",
            "sort": sort,
            "tags": tagged or [],
            "count": len(results),
            "questions": results,
            "ranking": None,
            "meta": self._meta(payload, response),
        }

    def question(self, question_id: int, answers_limit: int = 10) -> dict[str, Any]:
        if question_id < 1:
            raise AgentWebError("question_id must be positive")
        if answers_limit < 0 or answers_limit > 100:
            raise AgentWebError("answers_limit must be between 0 and 100")
        try:
            payload, response = self._json(
                f"/questions/{question_id}",
                params={"filter": "withbody"},
                cache_action="question",
                cache_arguments={"question_id": question_id},
                cache_ttl=300,
            )
        except AgentWebError as error:
            if not self._api_read_unavailable(error):
                raise
            website_question, website_response = self._website_question_page(
                question_id, answers_limit=answers_limit
            )
            return {
                "operation": "stackoverflow.question",
                "question": website_question,
                "meta": self._website_meta(
                    website_response, source="website_quota_fallback"
                ),
            }
        items = payload.get("items", [])
        if not items:
            raise AgentWebError(f"Stack Overflow question {question_id} was not found")
        row = items[0]
        body, body_truncated = bounded_html(row.get("body"), 6000)
        answer_rows = []
        answer_response = None
        answer_payload: dict[str, Any] = {}
        if answers_limit:
            answer_payload, answer_response = self._json(
                f"/questions/{question_id}/answers",
                params={
                    "filter": "withbody",
                    "pagesize": answers_limit,
                    "sort": "votes",
                    "order": "desc",
                },
                cache_action="answers",
                cache_arguments={"question_id": question_id, "limit": answers_limit},
                cache_ttl=300,
            )
            for answer in answer_payload.get("items", []):
                answer_body, truncated = bounded_html(answer.get("body"), 4000)
                answer_rows.append(
                    {
                        "answer_id": answer.get("answer_id"),
                        "author": clean_html(
                            (answer.get("owner") or {}).get("display_name")
                        ),
                        "score": answer.get("score"),
                        "is_accepted": answer.get("is_accepted"),
                        "created_at_unix": answer.get("creation_date"),
                        "last_activity_at_unix": answer.get("last_activity_date"),
                        "body": answer_body,
                        "body_truncated": truncated,
                    }
                )
        meta = self._meta(payload, response)
        if answer_response is not None:
            meta["elapsed_ms"] = round(
                response.elapsed_ms + answer_response.elapsed_ms, 1
            )
            meta["request_count"] = 2
            meta["quota_remaining"] = answer_payload.get("quota_remaining")
        else:
            meta["request_count"] = 1
        return {
            "operation": "stackoverflow.question",
            "question": {
                **self._question_summary(row),
                "body": body,
                "body_truncated": body_truncated,
                "returned_answer_count": len(answer_rows),
                "answers": answer_rows,
            },
            "meta": meta,
        }

    def tags(self, query: str | None = None, limit: int = 20) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        params: dict[str, Any] = {
            "pagesize": limit,
            "sort": "popular",
            "order": "desc",
        }
        if query:
            params["inname"] = query
        cache_arguments = {"query": query, "limit": limit}
        try:
            payload, response = self._json(
                "/tags",
                params=params,
                cache_action="tags",
                cache_arguments=cache_arguments,
                cache_ttl=300,
            )
        except AgentWebError as error:
            if not self._api_read_unavailable(error):
                raise
            website_response = self.session().request(
                "GET",
                "https://stackoverflow.com/tags",
                params={"tab": "popular", "filter": query or ""},
                headers={"Accept": "text/html,application/xhtml+xml"},
                cache_action="website_tags",
                cache_arguments=cache_arguments,
                cache_ttl=300,
            )
            soup = BeautifulSoup(website_response.text, "html.parser")
            results = []
            for cell in soup.select(".js-tag-cell"):
                tag = cell.select_one('a[rel="tag"]')
                if not tag:
                    continue
                label = tag.get_text(" ", strip=True)
                if query and query.lower() not in label.lower():
                    continue
                count = self._website_number(
                    next(
                        (
                            text
                            for text in cell.stripped_strings
                            if "question" in text.lower() and re.search(r"\d", text)
                        ),
                        None,
                    )
                )
                description = cell.select_one(".v-truncate4")
                results.append(
                    {
                        "name": label,
                        "question_count": count,
                        "description": description.get_text(" ", strip=True)
                        if description
                        else None,
                        "is_required": None,
                        "is_moderator_only": None,
                        "url": self._direct_url(str(tag.get("href"))),
                    }
                )
                if len(results) >= limit:
                    break
            return {
                "operation": "stackoverflow.tags",
                "query": query,
                "count": len(results),
                "tags": results,
                "ranking": {
                    "method": "website_popular_order",
                    "exact_website_order": True,
                },
                "meta": self._website_meta(
                    website_response, source="website_quota_fallback"
                ),
            }
        results = [
            {
                "name": row.get("name"),
                "question_count": row.get("count"),
                "is_required": row.get("is_required"),
                "is_moderator_only": row.get("is_moderator_only"),
                "url": f"https://stackoverflow.com/questions/tagged/{row.get('name')}",
            }
            for row in payload.get("items", [])
        ]
        return {
            "operation": "stackoverflow.tags",
            "query": query,
            "count": len(results),
            "tags": results,
            "meta": self._meta(payload, response),
        }

    def answers(
        self, question_id: int, limit: int = 20, sort: str = "votes"
    ) -> dict[str, Any]:
        if question_id < 1:
            raise AgentWebError("question_id must be positive")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        if sort not in {"votes", "creation", "activity"}:
            raise AgentWebError("sort must be votes, creation, or activity")
        try:
            payload, response = self._json(
                f"/questions/{question_id}/answers",
                params={
                    "filter": "withbody",
                    "pagesize": limit,
                    "sort": sort,
                    "order": "desc",
                },
                cache_action="answer_list",
                cache_arguments={
                    "question_id": question_id,
                    "limit": limit,
                    "sort": sort,
                },
                cache_ttl=300,
            )
        except AgentWebError as error:
            if not self._api_read_unavailable(error):
                raise
            website_question, website_response = self._website_question_page(
                question_id, answers_limit=limit
            )
            rows = list(website_question["answers"])
            if sort == "votes":
                rows.sort(key=lambda item: item.get("score") or 0, reverse=True)
            elif sort == "creation":
                rows.sort(
                    key=lambda item: item.get("created_at_unix") or 0, reverse=True
                )
            return {
                "operation": "stackoverflow.answers",
                "question_id": question_id,
                "sort": sort,
                "count": len(rows),
                "answers": rows,
                "meta": self._website_meta(
                    website_response, source="website_quota_fallback"
                ),
            }
        rows = []
        for item in payload.get("items", []):
            body, truncated = bounded_html(item.get("body"), 4000)
            rows.append(
                {
                    "answer_id": item.get("answer_id"),
                    "question_id": item.get("question_id"),
                    "author": clean_html((item.get("owner") or {}).get("display_name")),
                    "score": item.get("score"),
                    "is_accepted": item.get("is_accepted"),
                    "created_at_unix": item.get("creation_date"),
                    "last_activity_at_unix": item.get("last_activity_date"),
                    "body": body,
                    "body_truncated": truncated,
                }
            )
        return {
            "operation": "stackoverflow.answers",
            "question_id": question_id,
            "sort": sort,
            "count": len(rows),
            "answers": rows,
            "meta": self._meta(payload, response),
        }

    def users(
        self, query: str | None = None, limit: int = 20, sort: str = "reputation"
    ) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        if sort not in {"reputation", "creation", "name", "modified"}:
            raise AgentWebError("sort must be reputation, creation, name, or modified")
        params: dict[str, Any] = {"pagesize": limit, "sort": sort, "order": "desc"}
        if query:
            params["inname"] = query
        cache_arguments = {"query": query, "limit": limit, "sort": sort}
        try:
            payload, response = self._json(
                "/users",
                params=params,
                cache_action="users",
                cache_arguments=cache_arguments,
                cache_ttl=300,
            )
        except AgentWebError as error:
            if not self._api_read_unavailable(error):
                raise
            tab = {
                "reputation": "reputation",
                "creation": "newusers",
                "name": "name",
                "modified": "voters",
            }[sort]
            website_response = self.session().request(
                "GET",
                "https://stackoverflow.com/users",
                params={"tab": tab, "filter": query or ""},
                headers={"Accept": "text/html,application/xhtml+xml"},
                cache_action="website_users",
                cache_arguments=cache_arguments,
                cache_ttl=300,
            )
            soup = BeautifulSoup(website_response.text, "html.parser")
            rows = []
            for cell in soup.select(".user-info"):
                link = cell.select_one(".user-details > a[href^='/users/']")
                if not link:
                    continue
                match = re.match(r"/users/(\d+)/", str(link.get("href") or ""))
                if not match:
                    continue
                label = link.get_text(" ", strip=True)
                if query and query.lower() not in label.lower():
                    continue
                reputation = cell.select_one(".reputation-score")
                location = cell.select_one(".user-location")
                image = cell.select_one("img")
                rows.append(
                    {
                        "user_id": int(match.group(1)),
                        "display_name": label,
                        "reputation": self._website_number(
                            str(
                                reputation.get("title")
                                or reputation.get_text(" ", strip=True)
                            )
                            if reputation
                            else None
                        ),
                        "user_type": None,
                        "location": location.get_text(" ", strip=True)
                        if location
                        else None,
                        "profile_image": str(image.get("src"))
                        if image and image.get("src")
                        else None,
                        "url": self._direct_url(str(link.get("href"))),
                        "created_at_unix": None,
                        "last_access_at_unix": None,
                    }
                )
                if len(rows) >= limit:
                    break
            return {
                "operation": "stackoverflow.users",
                "query": query,
                "sort": sort,
                "count": len(rows),
                "users": rows,
                "ranking": {"method": "website_order", "exact_website_order": True},
                "meta": self._website_meta(
                    website_response, source="website_quota_fallback"
                ),
            }
        rows = [
            {
                "user_id": item.get("user_id"),
                "display_name": clean_html(item.get("display_name")),
                "reputation": item.get("reputation"),
                "user_type": item.get("user_type"),
                "location": item.get("location"),
                "profile_image": item.get("profile_image"),
                "url": item.get("link"),
                "created_at_unix": item.get("creation_date"),
                "last_access_at_unix": item.get("last_access_date"),
            }
            for item in payload.get("items", [])
        ]
        return {
            "operation": "stackoverflow.users",
            "query": query,
            "sort": sort,
            "count": len(rows),
            "users": rows,
            "meta": self._meta(payload, response),
        }

    def user(self, user_id: int) -> dict[str, Any]:
        if user_id < 1:
            raise AgentWebError("user_id must be positive")
        payload, response = self._json(
            f"/users/{user_id}",
            params={"filter": "default"},
            cache_action="user",
            cache_arguments={"user_id": user_id},
            cache_ttl=300,
        )
        items = payload.get("items", [])
        if not items:
            raise AgentWebError(f"Stack Overflow user {user_id} was not found")
        item = items[0]
        about, truncated = bounded_html(item.get("about_me"), 3000)
        return {
            "operation": "stackoverflow.user",
            "user": {
                "user_id": item.get("user_id"),
                "display_name": clean_html(item.get("display_name")),
                "reputation": item.get("reputation"),
                "user_type": item.get("user_type"),
                "location": item.get("location"),
                "website_url": item.get("website_url"),
                "profile_image": item.get("profile_image"),
                "url": item.get("link"),
                "view_count": item.get("view_count"),
                "up_vote_count": item.get("up_vote_count"),
                "down_vote_count": item.get("down_vote_count"),
                "answer_count": item.get("answer_count"),
                "question_count": item.get("question_count"),
                "created_at_unix": item.get("creation_date"),
                "last_access_at_unix": item.get("last_access_date"),
                "about": about,
                "about_truncated": truncated,
                "badge_counts": item.get("badge_counts"),
            },
            "meta": self._meta(payload, response),
        }

    def user_questions(
        self, user_id: int, limit: int = 20, sort: str = "activity"
    ) -> dict[str, Any]:
        if user_id < 1:
            raise AgentWebError("user_id must be positive")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        if sort not in {"activity", "votes", "creation"}:
            raise AgentWebError("sort must be activity, votes, or creation")
        payload, response = self._json(
            f"/users/{user_id}/questions",
            params={"pagesize": limit, "sort": sort, "order": "desc"},
            cache_action="user_questions",
            cache_arguments={"user_id": user_id, "limit": limit, "sort": sort},
            cache_ttl=180,
        )
        rows = [self._question_summary(item) for item in payload.get("items", [])]
        return {
            "operation": "stackoverflow.user_questions",
            "user_id": user_id,
            "sort": sort,
            "count": len(rows),
            "questions": rows,
            "meta": self._meta(payload, response),
        }

    def unanswered(
        self, limit: int = 20, tagged: list[str] | None = None, sort: str = "votes"
    ) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        if sort not in {"votes", "creation", "activity"}:
            raise AgentWebError("sort must be votes, creation, or activity")
        params: dict[str, Any] = {"pagesize": limit, "sort": sort, "order": "desc"}
        if tagged:
            params["tagged"] = ";".join(tagged)
        payload, response = self._json(
            "/questions/unanswered",
            params=params,
            cache_action="unanswered",
            cache_arguments={"limit": limit, "tagged": tagged or [], "sort": sort},
            cache_ttl=120,
        )
        rows = [self._question_summary(item) for item in payload.get("items", [])]
        return {
            "operation": "stackoverflow.unanswered",
            "sort": sort,
            "tags": tagged or [],
            "count": len(rows),
            "questions": rows,
            "meta": self._meta(payload, response),
        }

    def related(self, question_id: int, limit: int = 20) -> dict[str, Any]:
        if question_id < 1:
            raise AgentWebError("question_id must be positive")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        payload, response = self._json(
            f"/questions/{question_id}/related",
            params={"pagesize": limit},
            cache_action="related",
            cache_arguments={"question_id": question_id, "limit": limit},
            cache_ttl=300,
        )
        rows = [self._question_summary(item) for item in payload.get("items", [])]
        return {
            "operation": "stackoverflow.related",
            "question_id": question_id,
            "count": len(rows),
            "questions": rows,
            "meta": self._meta(payload, response),
        }

    def similar(
        self, title: str, limit: int = 20, tagged: list[str] | None = None
    ) -> dict[str, Any]:
        if not title.strip():
            raise AgentWebError("title cannot be empty")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        params: dict[str, Any] = {"title": title, "pagesize": limit}
        if tagged:
            params["tagged"] = ";".join(tagged)
        payload, response = self._json(
            "/similar",
            params=params,
            cache_action="similar",
            cache_arguments={"title": title, "limit": limit, "tagged": tagged or []},
            cache_ttl=300,
        )
        rows = [self._question_summary(item) for item in payload.get("items", [])]
        return {
            "operation": "stackoverflow.similar",
            "title": title,
            "tags": tagged or [],
            "count": len(rows),
            "questions": rows,
            "meta": self._meta(payload, response),
        }

    def comments(
        self, post_id: int, post_type: str = "question", limit: int = 20
    ) -> dict[str, Any]:
        if post_id < 1:
            raise AgentWebError("post_id must be positive")
        if post_type not in {"question", "answer"}:
            raise AgentWebError("post_type must be question or answer")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        segment = "questions" if post_type == "question" else "answers"
        payload, response = self._json(
            f"/{segment}/{post_id}/comments",
            params={
                "filter": "withbody",
                "pagesize": limit,
                "sort": "votes",
                "order": "desc",
            },
            cache_action="comments",
            cache_arguments={
                "post_id": post_id,
                "post_type": post_type,
                "limit": limit,
            },
            cache_ttl=180,
        )
        rows = []
        for item in payload.get("items", []):
            body, truncated = bounded_html(item.get("body"), 2000)
            rows.append(
                {
                    "comment_id": item.get("comment_id"),
                    "post_id": item.get("post_id"),
                    "author": clean_html((item.get("owner") or {}).get("display_name")),
                    "score": item.get("score"),
                    "edited": item.get("edited"),
                    "created_at_unix": item.get("creation_date"),
                    "body": body,
                    "body_truncated": truncated,
                }
            )
        return {
            "operation": "stackoverflow.comments",
            "post_id": post_id,
            "post_type": post_type,
            "count": len(rows),
            "comments": rows,
            "meta": self._meta(payload, response),
        }

    def tag_info(self, tag: str) -> dict[str, Any]:
        if not tag.strip() or len(tag) > 35:
            raise AgentWebError("tag must be a non-empty Stack Overflow tag")
        payload, response = self._json(
            f"/tags/{quote(tag, safe='')}/info",
            cache_action="tag_info",
            cache_arguments={"tag": tag},
            cache_ttl=600,
        )
        items = payload.get("items", [])
        if not items:
            raise AgentWebError(f"Stack Overflow tag {tag!r} was not found")
        item = items[0]
        return {
            "operation": "stackoverflow.tag_info",
            "tag": {
                "name": item.get("name"),
                "question_count": item.get("count"),
                "has_synonyms": item.get("has_synonyms"),
                "is_moderator_only": item.get("is_moderator_only"),
                "is_required": item.get("is_required"),
            },
            "meta": self._meta(payload, response),
        }

    def user_answers(
        self, user_id: int, limit: int = 20, sort: str = "votes"
    ) -> dict[str, Any]:
        if user_id < 1:
            raise AgentWebError("user_id must be positive")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        if sort not in {"votes", "creation", "activity"}:
            raise AgentWebError("sort must be votes, creation, or activity")
        payload, response = self._json(
            f"/users/{user_id}/answers",
            params={
                "filter": "withbody",
                "pagesize": limit,
                "sort": sort,
                "order": "desc",
            },
            cache_action="user_answers",
            cache_arguments={"user_id": user_id, "limit": limit, "sort": sort},
            cache_ttl=180,
        )
        rows = []
        for item in payload.get("items", []):
            body, truncated = bounded_html(item.get("body"), 4000)
            rows.append(
                {
                    "answer_id": item.get("answer_id"),
                    "question_id": item.get("question_id"),
                    "score": item.get("score"),
                    "is_accepted": item.get("is_accepted"),
                    "created_at_unix": item.get("creation_date"),
                    "last_activity_at_unix": item.get("last_activity_date"),
                    "body": body,
                    "body_truncated": truncated,
                }
            )
        return {
            "operation": "stackoverflow.user_answers",
            "user_id": user_id,
            "sort": sort,
            "count": len(rows),
            "answers": rows,
            "meta": self._meta(payload, response),
        }

    def question_timeline(self, question_id: int, limit: int = 50) -> dict[str, Any]:
        if question_id < 1:
            raise AgentWebError("question_id must be positive")
        if limit < 1 or limit > 100:
            raise AgentWebError("limit must be between 1 and 100")
        payload, response = self._json(
            f"/questions/{question_id}/timeline",
            params={"pagesize": limit},
            cache_action="question_timeline",
            cache_arguments={"question_id": question_id, "limit": limit},
            cache_ttl=180,
        )
        rows = []
        for item in payload.get("items", []):
            timeline_type = item.get("timeline_type")
            row = {
                "timeline_type": timeline_type,
                "post_id": item.get("post_id"),
                "comment_id": item.get("comment_id"),
                "revision_guid": item.get("revision_guid"),
                "created_at_unix": item.get("creation_date"),
                "user_id": item.get("user_id"),
                "detail": item.get("detail") or item.get("comment_id"),
            }
            if timeline_type == "vote_aggregate":
                row.update(
                    {
                        "aggregate_only": True,
                        "vote_direction_available": False,
                        "vote_count_available": False,
                    }
                )
            rows.append(row)
        return {
            "operation": "stackoverflow.question_timeline",
            "question_id": question_id,
            "count": len(rows),
            "events": rows,
            "limitations": {
                "vote_aggregate": {
                    "direction_available": False,
                    "count_available": False,
                    "reason": "The public Stack Exchange timeline API exposes only that aggregate voting activity occurred.",
                }
            },
            "meta": self._meta(payload, response),
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
            raise AgentWebError(
                "path must be a safe Stack Exchange API path beginning with /"
            )
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
        )
        data, truncated = bounded_data(
            payload, max_items=max_items, max_string=max_string
        )
        data, total_truncated, original_chars = enforce_data_budget(
            data, max_total_chars=max_total_chars
        )
        return {
            "operation": "stackoverflow.api_get",
            "path": path,
            "query": params,
            "data": data,
            "truncated": truncated or total_truncated,
            "truncation": {
                "nested_limit": truncated,
                "character_budget": total_truncated,
            },
            "original_chars": original_chars,
            "max_total_chars": max_total_chars,
            "meta": self._meta(payload, response),
        }

    def _web_page(self, path: str) -> tuple[BeautifulSoup, Response]:
        response = self.session().request(
            "GET",
            self._direct_url(path),
            headers={"Accept": "text/html"},
            cache_ttl=0,
        )
        if response.status >= 400:
            raise AgentWebError(f"Stack Overflow returned HTTP {response.status}")
        return BeautifulSoup(response.text, "html.parser"), response

    @staticmethod
    def _fkey(soup: BeautifulSoup) -> str:
        node = soup.select_one('input[name="fkey"]')
        if node and node.get("value"):
            return str(node.get("value"))
        match = re.search(r'"fkey"\s*:\s*"([^"]+)"', str(soup))
        if match:
            return match.group(1)
        raise AuthenticationRequired(
            "Stack Overflow did not expose an authenticated fkey; run agentweb connect stackoverflow"
        )

    def account_status(self) -> dict[str, Any]:
        soup, response = self._web_page("/")
        profile = soup.select_one(
            'a.my-profile, a[href^="/users/current"], a.s-topbar--item[href*="/users/"]'
        )
        authenticated = (
            profile is not None
            and "log in" not in profile.get_text(" ", strip=True).lower()
        )
        return {
            "operation": "stackoverflow.account_status",
            "authenticated": authenticated,
            "profile_url": self._direct_url(str(profile.get("href")))
            if authenticated and profile and profile.get("href")
            else None,
            "session": self.session_freshness(authenticated),
            "meta": {"elapsed_ms": round(response.elapsed_ms, 1), "url": response.url},
        }

    def _write(
        self,
        path: str,
        fields: dict[str, Any],
        operation: str,
        confirm: bool,
        preflight_path: str,
    ) -> dict[str, Any]:
        if not confirm:
            raise AgentWebError(
                f"{operation} changes remote state; inspect the inputs and repeat with confirm=true"
            )
        soup, preflight = self._web_page(preflight_path)
        fkey = self._fkey(soup)
        response = self.session().request(
            "POST",
            self._direct_url(path),
            form={"fkey": fkey, **fields},
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            referer=preflight.url,
            cache_ttl=0,
        )
        if response.status in {401, 403}:
            raise AuthenticationRequired(
                "Stack Overflow rejected the retained session; run agentweb connect stackoverflow"
            )
        try:
            payload: Any = json.loads(response.text)
        except json.JSONDecodeError:
            payload = clean_html(response.text)
        if response.status >= 400:
            raise AgentWebError(
                f"{operation} returned HTTP {response.status}: {payload}"
            )
        return {
            "operation": operation,
            "state_changed": True,
            "status": response.status,
            "data": payload,
            "secret_fields_exposed": False,
            "meta": {
                "request_count": 2,
                "elapsed_ms": round(preflight.elapsed_ms + response.elapsed_ms, 1),
                "url": self._redacted_url(response.url),
            },
        }

    def ask(
        self,
        title: str,
        body: str,
        tags: list[str],
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not title.strip() or not body.strip() or not tags:
            raise AgentWebError("title, body, and at least one tag are required")
        return self._write(
            "/questions/ask/submit",
            {"title": title, "post-text": body, "tagnames": " ".join(tags)},
            "stackoverflow.ask",
            confirm,
            "/questions/ask",
        )

    def answer(
        self, question_id: int, body: str, confirm: bool = False
    ) -> dict[str, Any]:
        if question_id < 1 or not body.strip():
            raise AgentWebError("question_id must be positive and body cannot be empty")
        return self._write(
            f"/questions/{question_id}/answer/submit",
            {"post-text": body},
            "stackoverflow.answer",
            confirm,
            f"/questions/{question_id}",
        )

    def add_comment(
        self, post_id: int, text: str, confirm: bool = False
    ) -> dict[str, Any]:
        if post_id < 1 or not text.strip():
            raise AgentWebError("post_id must be positive and text cannot be empty")
        return self._write(
            f"/posts/{post_id}/comments",
            {"comment": text},
            "stackoverflow.add_comment",
            confirm,
            "/",
        )

    def vote(self, post_id: int, vote: str, confirm: bool = False) -> dict[str, Any]:
        codes = {"undo": 0, "up": 2, "down": 3, "accept": 1, "unaccept": 0}
        if post_id < 1 or vote not in codes:
            raise AgentWebError("vote must be undo, up, down, accept, or unaccept")
        return self._write(
            f"/posts/{post_id}/vote/{codes[vote]}",
            {},
            "stackoverflow.vote",
            confirm,
            "/",
        )

    def submit_form(
        self,
        page_path: str,
        selector: str,
        fields: dict[str, Any],
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not confirm:
            raise AgentWebError(
                "stackoverflow.submit_form changes remote state; inspect the inputs and repeat with confirm=true"
            )
        soup, preflight = self._web_page(page_path)
        form = soup.select_one(selector)
        if form is None:
            raise AgentWebError(
                f"Stack Overflow page did not contain form selector {selector!r}"
            )
        values = {
            str(node.get("name")): str(node.get("value") or "")
            for node in form.select("input[name]")
            if node.get("type") not in {"submit", "button"}
        }
        values.update(fields)
        action = str(form.get("action") or page_path)
        response = self.session().request(
            str(form.get("method") or "POST").upper(),
            self._direct_url(action),
            form=values,
            referer=preflight.url,
            cache_ttl=0,
        )
        if response.status >= 400:
            raise AgentWebError(
                f"stackoverflow.submit_form returned HTTP {response.status}"
            )
        return {
            "operation": "stackoverflow.submit_form",
            "state_changed": True,
            "status": response.status,
            "url": self._redacted_url(response.url),
            "hidden_fields_exposed": False,
            "elapsed_ms": round(preflight.elapsed_ms + response.elapsed_ms, 1),
        }
