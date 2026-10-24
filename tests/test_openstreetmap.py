import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from concerts import musicbrainz, openstreetmap

from .test_musicbrainz import mock_transport

FIXTURES = Path(__file__).parent / "fixtures"
TITI_TWISTER = json.loads((FIXTURES / "nominatim_search_titi_twister.json").read_text())
# Only a stream called "L'Aure": not a venue
LAURE = json.loads((FIXTURES / "nominatim_search_laure_saverdun.json").read_text())


@pytest.fixture(autouse=True)
def no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(musicbrainz, "_sleep", lambda seconds: None)


def osm_client(*answers: object, requests: list[httpx.Request] | None = None) -> httpx.Client:
    return openstreetmap.make_client(transport=mock_transport(*answers, requests=requests))


def test_search_request_and_result(settings):
    settings.CONTACT_EMAIL = "contact@example.org"
    requests: list[httpx.Request] = []
    [result] = openstreetmap.search_places(
        "Titi Twister", "Fontanil-Cornillon", osm_client(TITI_TWISTER, requests=requests)
    )
    request = requests[0]
    assert parse_qs(request.url.query.decode())["q"] == ["Titi Twister, Fontanil-Cornillon"]
    assert "contact@example.org" in request.headers["User-Agent"]
    assert result == {
        "osm_id": "node/3772940557",
        "name": "Titi Twister",
        "type": "bar",
        "address": "1 Rue du Cornillon",
        "postal_code": "38120",
        "city": "Fontanil-Cornillon",  # the village, not the wider "municipality" (Grenoble)
        "latitude": result["latitude"],
        "longitude": result["longitude"],
        "website": "https://www.titi-twister.fr/",
    }
    assert len(result["latitude"].split(".")[1]) == 6  # rounded to what our coordinates keep


def test_places_roads_and_rivers_are_not_venues():
    assert openstreetmap.search_places("La Laure", "Saverdun", osm_client(LAURE)) == []


def test_busy_server_gives_a_readable_error():
    with pytest.raises(openstreetmap.OpenStreetMapError, match="busy"):
        openstreetmap.search_places("Titi Twister", "", osm_client(503, 503, 503))


# 23 rue Boyer: La Maroquinerie, and the building's address. 12 rue Boyer: two entrances of a building, no venue
RUE_BOYER_23 = json.loads((FIXTURES / "nominatim_address_rue_boyer.json").read_text())
RUE_BOYER_12 = json.loads((FIXTURES / "nominatim_address_12_rue_boyer.json").read_text())


def test_address_of_a_mapped_venue_finds_it():
    requests: list[httpx.Request] = []
    venue, plain = openstreetmap.search_address(
        "23 rue Boyer", "", "Paris", client=osm_client(RUE_BOYER_23, requests=requests)
    )
    assert venue["name"] == "La Maroquinerie" and venue["osm_id"] == "node/1402597553"
    assert venue["website"] == "https://www.lamaroquinerie.fr/"
    assert plain["type"] == "address only" and plain["name"] == "" and plain["osm_id"] == ""
    sent = parse_qs(requests[0].url.query.decode())
    assert sent["street"] == ["23 rue Boyer"] and sent["city"] == ["Paris"]


def test_address_alone_gives_coordinates_once():
    requests: list[httpx.Request] = []
    client = osm_client(RUE_BOYER_12, RUE_BOYER_12, requests=requests)
    [plain] = openstreetmap.search_address("12 rue Boyer", "75020", "Paris", client=client)
    assert plain["type"] == "address only" and plain["osm_id"] == ""
    assert (plain["address"], plain["postal_code"], plain["city"]) == ("12 Rue Boyer", "75020", "Paris")
    assert parse_qs(requests[0].url.query.decode())["postalcode"] == ["75020"]
    # Without one typed in, none: the map's may be a guess
    [plain] = openstreetmap.search_address("12 rue Boyer", "", "Paris", client=client)
    assert plain["postal_code"] == ""
    assert (plain["latitude"], plain["longitude"]) == ("48.867604", "2.392682")
