"""Read venues ("places") from MusicBrainz. Data under CC0; read only, never write."""
import re
import threading
import time
from contextlib import nullcontext

import httpx
from django.conf import settings

# French address ending: "23 rue Boyer, 75020 Paris" → street, postal code, city
ADDRESS = re.compile(r"^(?P<street>.*?)[,\s]*\b(?P<postal_code>\d{5})\s+(?P<city>.+)$")
RETRY_STATUSES = {429, 500, 502, 503, 504}
MIN_INTERVAL = 1.0  # seconds between requests, MusicBrainz's rule
_sleep = time.sleep  # replaced in tests

_lock = threading.Lock()
_last_request = 0.0


class MusicBrainzError(Exception):
    pass


def make_client(transport=None):
    """transport: lets tests replace the network with httpx.MockTransport."""
    contact = settings.MUSICBRAINZ_CONTACT
    user_agent = f"OTR/0.1 ( {contact} )" if contact else "OTR/0.1"
    return httpx.Client(base_url="https://musicbrainz.org/ws/2/", headers={"User-Agent": user_agent},
                        timeout=10, transport=transport)


def get_json(client, path, params, retries=2):
    """GET path, at most one request per second, waiting 2 then 4 s between retries."""
    global _last_request
    for attempt in range(retries + 1):
        # ponytail: per-process lock; several server processes would need a shared limiter
        with _lock:
            _sleep(max(0.0, MIN_INTERVAL - (time.monotonic() - _last_request)))
            try:
                response = client.get(path, params={**params, "fmt": "json"})
            except httpx.TransportError:
                if attempt == retries:
                    raise
                response = None
            finally:
                _last_request = time.monotonic()
        if response is not None and (response.status_code not in RETRY_STATUSES or attempt == retries):
            response.raise_for_status()
            return response.json()
        _sleep(2 ** (attempt + 1))


def _fetch(client, path, params):
    try:
        # A client created here is closed after the request; one passed in belongs to the caller
        with nullcontext(client) if client else make_client() as client:
            return get_json(client, path, params)
    except httpx.HTTPStatusError as error:
        if error.response.status_code in RETRY_STATUSES:
            raise MusicBrainzError("MusicBrainz is busy, try again in a few seconds.") from error
        raise MusicBrainzError(f"MusicBrainz answered with error {error.response.status_code}.") from error
    except httpx.TransportError as error:
        raise MusicBrainzError("MusicBrainz could not be reached.") from error


def _venue_fields(place):
    """Map a MusicBrainz place to our Venue fields."""
    address = place.get("address", "")
    match = ADDRESS.match(address)
    coordinates = place.get("coordinates") or {}
    return {
        "musicbrainz_id": place["id"],
        "name": place["name"],
        "type": place.get("type", ""),
        # The area can be a district ("20e arrondissement de Paris"), so the address's city comes first
        "address": match["street"] if match else address,
        "postal_code": match["postal_code"] if match else "",
        "city": match["city"] if match else place.get("area", {}).get("name", ""),
        "latitude": coordinates.get("latitude", ""),
        "longitude": coordinates.get("longitude", ""),
    }


def _quoted(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def search_places(name, city, client=None):
    query = f"place:{_quoted(name)}" + (f" AND area:{_quoted(city)}" if city else "")
    data = _fetch(client, "place/", {"query": query, "limit": 10})
    return [_venue_fields(place) for place in data["places"]]


def place(mbid, client=None):
    """One place with its official website, if MusicBrainz knows it."""
    data = _fetch(client, f"place/{mbid}", {"inc": "url-rels"})
    fields = _venue_fields(data)
    fields["website"] = next((relation["url"]["resource"] for relation in data.get("relations", [])
                              if relation["type"] == "official homepage" and not relation["ended"]), "")
    return fields
