from __future__ import annotations

import json
import warnings
from typing import Any

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

from agentweb.sdk import AgentWebError, RequestRecipeAdapter, paginate_operation_output


class Adapter(RequestRecipeAdapter):
    site_name = "gst"
    base_url = "https://www.gst.gov.in"
    allowed_domains = ("gst.gov.in", "services.gst.gov.in", "tutorial.gst.gov.in")
    recipes = {"home": {"method": "GET", "path": "/", "cache_ttl": 60}}

    @staticmethod
    def _result(
        action: str,
        data: dict[str, Any],
        *,
        pagination: dict[str, Any] | None = None,
        notices: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "operation": f"gst.{action}",
            "data": data,
            "state_change": {"changed": False, "reversible": False, "idempotent": True},
            "pagination": pagination or {"supported": False},
            "warnings": notices or [],
            "verification": {
                "verified": True,
                "transport": "typed_adapter",
                "reviewed": True,
            },
        }

    def _json_get(
        self,
        url: str,
        *,
        action: str,
        arguments: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> Any:
        response = self.session().request(
            "GET",
            url,
            params=params,
            headers={
                "Accept": "application/json",
                "Referer": "https://www.gst.gov.in/",
            },
            cache_action=action,
            cache_arguments=arguments,
            cache_ttl=60,
        )
        if response.status >= 400:
            raise AgentWebError(
                f"GST portal returned HTTP {response.status}",
                code="gst_rate_limited"
                if response.status == 429
                else "gst_unavailable",
                retryable=response.status >= 429,
            )
        try:
            return json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise AgentWebError(
                "GST portal returned malformed JSON",
                code="invalid_website_response",
                retryable=True,
            ) from exc

    def search_hsn_sac(self, query: str) -> dict[str, Any]:
        arguments = {"query": query}
        payload = self._json_get(
            "https://services.gst.gov.in/commonservices/hsn/search/qsearch",
            action="search_hsn_sac",
            arguments=arguments,
            params={"category": "null", "inputText": query, "selectedType": "byCode"},
        )
        rows = payload.get("data") or [] if isinstance(payload, dict) else []
        matches = [
            {"code": row.get("c"), "description": row.get("n")}
            for row in rows
            if isinstance(row, dict)
        ]
        return self._result("search_hsn_sac", {"query": query, "matches": matches})

    def list_advisories(
        self, year: int, cursor: str | None = None, limit: int = 25
    ) -> dict[str, Any]:
        arguments = {"year": year, "cursor": cursor, "limit": limit}
        payload = self._json_get(
            f"https://services.gst.gov.in/master/advisories/updated/{year}",
            action="list_advisories",
            arguments=arguments,
        )
        rows = payload.get("data") or [] if isinstance(payload, dict) else []
        advisories = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
                content = BeautifulSoup(
                    str(row.get("content") or ""), "html.parser"
                ).get_text(" ", strip=True)
            advisories.append(
                {
                    "id": row.get("id"),
                    "title": row.get("title"),
                    "content": content,
                    "date": row.get("date"),
                    "module": row.get("module") or "",
                    "link_url": row.get("link_url") or row.get("linkUrl") or "",
                    "is_external": str(
                        row.get("is_external") or row.get("isExternal") or "N"
                    ).upper()
                    == "Y",
                }
            )
        data, pagination, notices = paginate_operation_output(
            {"year": year, "advisories": advisories},
            {
                "collection": "advisories",
                "pagination": {
                    "limit_input": "limit",
                    "cursor_input": "cursor",
                    "default_limit": 25,
                    "max_limit": 100,
                },
            },
            arguments,
        )
        return self._result(
            "list_advisories", data, pagination=pagination, notices=notices
        )
