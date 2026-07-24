from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'readthedocs'
    base_url = 'https://readthedocs.org'
    allowed_domains = ('readthedocs.org',)
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
