from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'nominatim'
    base_url = 'https://nominatim.openstreetmap.org'
    allowed_domains = ('nominatim.openstreetmap.org', 'openstreetmap.org')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
