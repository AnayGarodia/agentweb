from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'mdn'
    base_url = 'https://developer.mozilla.org'
    allowed_domains = ('developer.mozilla.org',)
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
