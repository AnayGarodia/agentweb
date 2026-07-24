from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'crossref'
    base_url = 'https://api.crossref.org'
    allowed_domains = ('api.crossref.org', 'crossref.org')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
