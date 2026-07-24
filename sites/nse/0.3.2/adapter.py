from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'nse'
    base_url = 'https://www.nseindia.com'
    allowed_domains = ('nseindia.com', 'www.nseindia.com')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
