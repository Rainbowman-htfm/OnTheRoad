"""Find venues on OpenStreetMap through Nominatim. Data under ODbL: "© OpenStreetMap contributors";
read only, one search per click (Nominatim's rules forbid search-as-you-type)."""

from contextlib import nullcontext
from typing import Any, TypedDict

import httpx

from .musicbrainz import RETRY_STATUSES, get_json, user_agent

# Not venues: towns, roads, rivers, borders, land use…
EXCLUDED = {"natural", "place", "highway", "waterway", "boundary", "landuse"}


class OpenStreetMapError(Exception):
    pass


class VenueFields(TypedDict):
    """A Nominatim result translated to our Venue fields, plus its type for display."""

    osm_id: str
    name: str
    type: str
    address: str
    postal_code: str
    city: str
    latitude: str
    longitude: str
    website: str


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """transport: lets tests replace the network with httpx.MockTransport."""
    return httpx.Client(
        base_url="https://nominatim.openstreetmap.org/",
        headers={"User-Agent": user_agent()},
        timeout=10,
        transport=transport,
    )


def _venue_fields(result: dict[str, Any]) -> VenueFields:
    address = result.get("address", {})
    tags = result.get("extratags") or {}
    return {
        "osm_id": f"{result['osm_type']}/{result['osm_id']}",
        "name": result["name"],
        "type": result.get("type", "").replace("_", " "),
        "address": f"{address.get('house_number', '')} {address.get('road', '')}".strip(),
        "postal_code": address.get("postcode", ""),
        # The commune; "municipality" can be a wider area (Grenoble for Fontanil-Cornillon), so it comes last
        "city": address.get("city") or address.get("town") or address.get("village") or address.get("municipality", ""),
        # Rounded to the 6 decimals our coordinates keep (≈ 10 cm)
        "latitude": f"{float(result['lat']):.6f}",
        "longitude": f"{float(result['lon']):.6f}",
        "website": tags.get("website") or tags.get("contact:website", ""),
    }


def search_places(name: str, city: str, client: httpx.Client | None = None) -> list[VenueFields]:
    params = {
        "q": ", ".join(part for part in (name, city) if part),
        "format": "jsonv2",
        "addressdetails": 1,  # street, postcode and town as separate fields
        "extratags": 1,  # the website
        "limit": 10,
    }
    try:
        # A client created here is closed after the request; one passed in belongs to the caller
        with nullcontext(client) if client else make_client() as http:
            results = get_json(http, "search", params)
    except httpx.HTTPStatusError as error:
        if error.response.status_code in RETRY_STATUSES:
            raise OpenStreetMapError("OpenStreetMap is busy, try again in a few seconds.") from error
        raise OpenStreetMapError(f"OpenStreetMap answered with error {error.response.status_code}.") from error
    except httpx.TransportError as error:
        raise OpenStreetMapError("OpenStreetMap could not be reached.") from error
    return [
        _venue_fields(result) for result in results if result.get("category") not in EXCLUDED and result.get("name")
    ]
