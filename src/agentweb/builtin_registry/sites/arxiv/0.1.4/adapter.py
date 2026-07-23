from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from agentweb.sdk import (
    AgentWebError,
    RequestRecipeAdapter,
    paginate_operation_output,
)


def _parse_xml_feed(body: bytes) -> dict[str, Any]:
    """Parse arXiv Atom/RSS without depending on the installed core version."""
    if len(body) > 10 * 1024 * 1024:
        raise AgentWebError(
            "The website returned an XML feed larger than AgentWeb's 10 MB parsing limit",
            code="feed_too_large",
            retryable=False,
        )
    declarations = body.upper()
    if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
        raise AgentWebError(
            "AgentWeb refused an XML feed containing a document type or entity declaration",
            code="unsafe_xml_feed",
            retryable=False,
        )
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise AgentWebError(
            "The website returned a malformed XML feed",
            code="invalid_xml_feed",
            retryable=True,
        ) from exc

    def local(node: ET.Element) -> str:
        return str(node.tag).rsplit("}", 1)[-1]

    def children(node: ET.Element, name: str) -> list[ET.Element]:
        return [child for child in node if local(child) == name]

    def first_text(node: ET.Element, *names: str) -> str | None:
        for name in names:
            match = next(iter(children(node, name)), None)
            if match is not None:
                value = " ".join("".join(match.itertext()).split())
                if value:
                    return value
        return None

    def feed_link(node: ET.Element) -> str | None:
        for link in children(node, "link"):
            href = str(link.get("href") or "").strip()
            text = " ".join("".join(link.itertext()).split())
            if href or text:
                return href or text
        return None

    root_name = local(root)
    channel = (
        next(iter(children(root, "channel")), root) if root_name == "rss" else root
    )
    entry_nodes = children(channel, "item" if root_name == "rss" else "entry")
    entries: list[dict[str, Any]] = []
    for node in entry_nodes:
        links = []
        for link in children(node, "link"):
            href = str(link.get("href") or "").strip()
            if not href:
                href = " ".join("".join(link.itertext()).split())
            if href:
                links.append(
                    {
                        key: value
                        for key, value in {
                            "url": href,
                            "rel": link.get("rel"),
                            "type": link.get("type"),
                            "title": link.get("title"),
                        }.items()
                        if value
                    }
                )
        authors = [
            first_text(author, "name") or " ".join("".join(author.itertext()).split())
            for author in children(node, "author")
        ]
        authors = [author for author in authors if author]
        if not authors:
            authors = [
                value for value in (first_text(node, "creator", "author"),) if value
            ]
        categories = [
            str(category.get("term") or " ".join(category.itertext())).strip()
            for category in children(node, "category")
        ]
        source_id = first_text(node, "id", "guid")
        identifier = source_id
        canonical_url = None
        if source_id:
            match = re.fullmatch(
                r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/([^?#]+?)(?:\.pdf)?/?",
                source_id,
                re.I,
            )
            if match:
                identifier = match.group(1)
                canonical_url = f"https://arxiv.org/abs/{identifier}"
            elif source_id.lower().startswith("oai:arxiv.org:"):
                identifier = source_id.split(":", 2)[-1]
                canonical_url = f"https://arxiv.org/abs/{identifier}"
        entry = {
            "id": identifier,
            "url": canonical_url,
            "title": first_text(node, "title"),
            "summary": first_text(node, "summary", "description", "content"),
            "published": first_text(node, "published", "pubDate"),
            "updated": first_text(node, "updated"),
            "authors": authors,
            "categories": [item for item in categories if item],
            "primary_category": next(
                (
                    str(item.get("term") or "").strip()
                    for item in children(node, "primary_category")
                    if item.get("term")
                ),
                None,
            ),
            "comment": first_text(node, "comment"),
            "journal_reference": first_text(node, "journal_ref"),
            "doi": first_text(node, "doi"),
            "links": links,
        }
        entries.append(
            {key: value for key, value in entry.items() if value not in (None, [], "")}
        )

    metadata = {
        "id": first_text(channel, "id"),
        "title": first_text(channel, "title"),
        "description": first_text(channel, "subtitle", "description"),
        "updated": first_text(channel, "updated", "lastBuildDate"),
        "url": feed_link(channel),
        "total_results": first_text(channel, "totalResults"),
        "start_index": first_text(channel, "startIndex"),
        "items_per_page": first_text(channel, "itemsPerPage"),
        "entries": entries,
    }
    for key in ("total_results", "start_index", "items_per_page"):
        value = metadata.get(key)
        if isinstance(value, str) and value.isdigit():
            metadata[key] = int(value)
    return {key: value for key, value in metadata.items() if value not in (None, "")}


class Adapter(RequestRecipeAdapter):
    site_name = "arxiv"
    base_url = "https://arxiv.org"
    allowed_domains = (
        "arxiv.org",
        "export.arxiv.org",
        "info.arxiv.org",
        "rss.arxiv.org",
    )
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
            "operation": f"arxiv.{action}",
            "data": data,
            "state_change": {"changed": False, "reversible": False, "idempotent": True},
            "pagination": pagination or {"supported": False},
            "warnings": warnings or [],
            "verification": {
                "verified": True,
                "transport": "typed_adapter",
                "reviewed": True,
            },
        }

    def _feed(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        action: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        response = self.session().request(
            "GET",
            url,
            params=params,
            headers={
                "Accept": "application/atom+xml, application/rss+xml, application/xml"
            },
            cache_action=action,
            cache_arguments=arguments,
            cache_ttl=60,
        )
        if response.status >= 400:
            raise AgentWebError(
                f"arXiv returned HTTP {response.status}",
                code="arxiv_rate_limited"
                if response.status == 429
                else "arxiv_unavailable",
                retryable=response.status >= 429,
            )
        return _parse_xml_feed(response.body)

    def _api_query(
        self, action: str, *, arguments: dict[str, Any], **params: Any
    ) -> dict[str, Any]:
        feed = self._feed(
            "https://export.arxiv.org/api/query",
            params=params,
            action=action,
            arguments=arguments,
        )
        papers = feed.get("entries") or []
        data = {
            "total_results": feed.get("total_results", len(papers)),
            "start_index": feed.get("start_index", int(params.get("start", 0))),
            "items_per_page": feed.get("items_per_page", len(papers)),
            "papers": papers,
        }
        return self._result(action, data)

    def search_papers(
        self,
        query: str,
        limit: int = 20,
        offset: int = 0,
        sort: str = "relevance",
        order: str = "descending",
    ) -> dict[str, Any]:
        sort_map = {
            "relevance": "relevance",
            "submitted": "submittedDate",
            "updated": "lastUpdatedDate",
        }
        arguments = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "order": order,
        }
        return self._api_query(
            "search_papers",
            arguments=arguments,
            search_query=query,
            start=offset,
            max_results=limit,
            sortBy=sort_map[sort],
            sortOrder=order,
        )

    def get_papers(self, ids: str) -> dict[str, Any]:
        count = len(ids.split(","))
        return self._api_query(
            "get_papers",
            arguments={"ids": ids},
            id_list=ids,
            start=0,
            max_results=min(count, 50),
        )

    def list_category_papers(
        self, category: str, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        arguments = {"category": category, "limit": limit, "offset": offset}
        return self._api_query(
            "list_category_papers",
            arguments=arguments,
            search_query=f"cat:{category}",
            start=offset,
            max_results=limit,
            sortBy="submittedDate",
            sortOrder="descending",
        )

    def list_author_papers(
        self, author: str, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        arguments = {"author": author, "limit": limit, "offset": offset}
        return self._api_query(
            "list_author_papers",
            arguments=arguments,
            search_query=f'au:"{author}"',
            start=offset,
            max_results=limit,
            sortBy="submittedDate",
            sortOrder="descending",
        )

    def get_category_feed(
        self, category: str, cursor: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        arguments = {"category": category, "cursor": cursor, "limit": limit}
        feed = self._feed(
            f"https://rss.arxiv.org/rss/{category}",
            action="get_category_feed",
            arguments=arguments,
        )
        data, pagination, warnings = paginate_operation_output(
            {
                "title": feed.get("title") or f"arXiv {category}",
                "papers": feed.get("entries") or [],
            },
            {
                "collection": "papers",
                "pagination": {
                    "limit_input": "limit",
                    "cursor_input": "cursor",
                    "default_limit": 10,
                    "max_limit": 50,
                },
            },
            arguments,
        )
        return self._result(
            "get_category_feed", data, pagination=pagination, warnings=warnings
        )
