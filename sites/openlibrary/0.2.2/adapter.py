from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'openlibrary'
    base_url = 'https://openlibrary.org'
    allowed_domains = ('openlibrary.org',)
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
