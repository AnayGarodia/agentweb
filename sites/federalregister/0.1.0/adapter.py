from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'federalregister'
    base_url = 'https://www.federalregister.gov'
    allowed_domains = ('federalregister.gov', 'www.federalregister.gov')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
