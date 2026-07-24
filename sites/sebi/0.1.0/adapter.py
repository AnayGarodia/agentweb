from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'sebi'
    base_url = 'https://www.sebi.gov.in'
    allowed_domains = ('gov.in', 'www.sebi.gov.in')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
