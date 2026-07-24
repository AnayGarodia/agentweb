from __future__ import annotations

from agentweb.sdk import RequestRecipeAdapter


class Adapter(RequestRecipeAdapter):
    site_name = 'openmeteo'
    base_url = 'https://open-meteo.com'
    allowed_domains = ('air-quality-api.open-meteo.com', 'api.open-meteo.com', 'archive-api.open-meteo.com', 'geocoding-api.open-meteo.com', 'open-meteo.com')
    recipes = {
        "home": {"method": "GET", "path": "/", "cache_ttl": 60}
    }
