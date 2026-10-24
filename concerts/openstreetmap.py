"""Find venues on OpenStreetMap through Nominatim, by name or by address. Data under ODbL:
"© OpenStreetMap contributors"; read only, one search per click (Nominatim's rules forbid search-as-you-type)."""

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


def _search(params: dict[str, Any], client: httpx.Client | None) -> Any:
    try:
        # A client created here is closed after the request; one passed in belongs to the caller
        with nullcontext(client) if client else make_client() as http:
            return get_json(http, "search", {**params, "format": "jsonv2"})
    except httpx.HTTPStatusError as error:
        if error.response.status_code in RETRY_STATUSES:
            raise OpenStreetMapError("OpenStreetMap is busy, try again in a few seconds.") from error
        raise OpenStreetMapError(f"OpenStreetMap answered with error {error.response.status_code}.") from error
    except httpx.TransportError as error:
        raise OpenStreetMapError("OpenStreetMap could not be reached.") from error


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
        "addressdetails": 1,  # street, postcode and town as separate fields
        "extratags": 1,  # the website
        "limit": 10,
    }
    results = _search(params, client)
    return [_venue_fields(result) for result in results if is_venue(result)]


def is_venue(result: dict[str, Any]) -> bool:
    return bool(result.get("name")) and result.get("category") not in EXCLUDED


def search_address(address: str, postal_code: str, city: str, client: httpx.Client | None = None) -> list[VenueFields]:
    """Venues at this address, and the address itself: a venue missing from OpenStreetMap still gets its
    coordinates, since its street is usually there. A structured search (street, city) rather than free text."""
    params = {
        "street": address,
        "postalcode": postal_code,
        "city": city,
        "addressdetails": 1,
        "extratags": 1,
        "limit": 5,
    }
    results = _search({key: value for key, value in params.items() if value}, client)
    venues = [_venue_fields(result) for result in results if is_venue(result)]
    # The address alone, once: a building is not the venue, so no name, website or OpenStreetMap ID. Its postal
    # code is often missing from the map, and Nominatim then guesses it from the nearby postal areas, wrongly near
    # a border (75002 for the Olympia, 75009): the one typed in, or none
    plain = next((result for result in results if not is_venue(result)), None)
    if plain:
        venues.append(
            _venue_fields(plain)
            | {"osm_id": "", "name": "", "type": "address only", "website": "", "postal_code": postal_code}
        )
    return venues
