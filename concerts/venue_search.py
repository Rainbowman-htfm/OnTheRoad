"""Venue search across MusicBrainz and OpenStreetMap: one list, where a venue found in both is one result."""

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

SAME_PLACE_METRES = 200


def normalize(text: str) -> str:
    """Name for comparisons, without accents, case or punctuation: "Glaz'art" and "GLAZART" are equal."""
    without_accents = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", without_accents.lower())


def distance_metres(a: Mapping[str, Any], b: Mapping[str, Any]) -> float | None:
    """Distance between two results' coordinates (haversine formula), or None if one has none."""
    if not (a.get("latitude") and b.get("latitude")):
        return None
    lat1, lon1, lat2, lon2 = map(
        math.radians, map(float, (a["latitude"], a["longitude"], b["latitude"], b["longitude"]))
    )
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def same_venue(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Same name, and in the same city or close together (a city written differently). Coordinates alone
    are not enough to tell two venues apart: MusicBrainz's are typed by hand, sometimes a few km off."""
    if normalize(a["name"]) != normalize(b["name"]):
        return False
    distance = distance_metres(a, b)
    return normalize(a["city"]) == normalize(b["city"]) or (distance is not None and distance <= SAME_PLACE_METRES)


def merge(musicbrainz: Sequence[Mapping[str, Any]], openstreetmap: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """MusicBrainz results first, each joined by the same venue from OpenStreetMap if there is one: MusicBrainz's
    values win and OpenStreetMap fills what they lack (often the website), except the coordinates, placed on
    the map in OpenStreetMap. Then the other OpenStreetMap results."""
    remaining = list(openstreetmap)
    results = []
    for place in musicbrainz:
        match = next((result for result in remaining if same_venue(place, result)), None)
        if match:
            remaining.remove(match)
        merged = {**(match or {}), **{key: value for key, value in place.items() if value}}
        if match and match.get("latitude"):
            merged["latitude"], merged["longitude"] = match["latitude"], match["longitude"]
        results.append(merged)
    return results + [dict(result) for result in remaining]
