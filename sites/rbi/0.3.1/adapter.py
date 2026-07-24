from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'rbi'
    base_url = 'https://www.rbi.org.in'
    allowed_domains = ('org.in', 'rbi.org.in', 'www.rbi.org.in')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
