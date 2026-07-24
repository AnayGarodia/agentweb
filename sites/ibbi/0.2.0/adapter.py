from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'ibbi'
    base_url = 'https://ibbi.gov.in'
    allowed_domains = ('gov.in', 'ibbi.gov.in')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
