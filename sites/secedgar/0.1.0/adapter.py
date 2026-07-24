from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'secedgar'
    base_url = 'https://www.sec.gov'
    allowed_domains = ('data.sec.gov', 'efts.sec.gov', 'sec.gov', 'www.sec.gov')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
