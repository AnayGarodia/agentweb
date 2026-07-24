from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'worldbank'
    base_url = 'https://www.worldbank.org'
    allowed_domains = ('api.worldbank.org', 'worldbank.org', 'www.worldbank.org')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
