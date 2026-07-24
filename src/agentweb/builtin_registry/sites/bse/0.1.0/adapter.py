from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'bse'
    base_url = 'https://www.bseindia.com'
    allowed_domains = ('api.bseindia.com', 'bseindia.com', 'www.bseindia.com')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
