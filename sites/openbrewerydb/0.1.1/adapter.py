from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'openbrewerydb'
    base_url = 'https://api.openbrewerydb.org'
    allowed_domains = ('api.openbrewerydb.org', 'openbrewerydb.org', 'www.openbrewerydb.org')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
