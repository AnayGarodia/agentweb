from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'coingecko'
    base_url = 'https://www.coingecko.com'
    allowed_domains = ('api.coingecko.com', 'coingecko.com', 'www.coingecko.com')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
