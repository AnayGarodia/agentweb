from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'indiapost'
    base_url = 'https://www.indiapost.gov.in'
    allowed_domains = ('api.postalpincode.in', 'postalpincode.in', 'www.indiapost.gov.in')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
