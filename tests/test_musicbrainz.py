import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from concerts import musicbrainz
from concerts.models import Venue

FIXTURES = Path(__file__).parent / "fixtures"
MAROQUINERIE = "7e46c91a-2774-4607-b209-af1da7de8a0d"
SEARCH = json.loads((FIXTURES / "musicbrainz_search_maroquinerie.json").read_text())
PLACE = json.loads((FIXTURES / "musicbrainz_place_maroquinerie.json").read_text())


def mock_transport(*answers: Any, requests: list[httpx.Request] | None = None) -> httpx.MockTransport:
    """Fake network: answers are returned in order, each a JSON body or an HTTP status code."""
    pending = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        answer = pending.pop(0)
        return httpx.Response(answer) if isinstance(answer, int) else httpx.Response(200, json=answer)

    return httpx.MockTransport(handler)


def mb_client(*answers: Any, requests: list[httpx.Request] | None = None) -> httpx.Client:
    return musicbrainz.make_client(transport=mock_transport(*answers, requests=requests))


@pytest.fixture(autouse=True)
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Records the pauses instead of waiting."""
    waits: list[float] = []
    monkeypatch.setattr(musicbrainz, "_sleep", waits.append)
    return waits


def test_search_request_and_result(settings):
    settings.MUSICBRAINZ_CONTACT = "contact@example.org"
    requests: list[httpx.Request] = []
    [place] = musicbrainz.search_places("Maroquinerie", "Paris", client=mb_client(SEARCH, requests=requests))
    request = requests[0]
    assert request.headers["user-agent"] == "OTR/0.1 ( contact@example.org )"
    assert request.url.path == "/ws/2/place/"
    assert request.url.params["query"] == 'place:"Maroquinerie" AND area:"Paris"'
    assert request.url.params["fmt"] == "json"
    assert place == {
        "musicbrainz_id": MAROQUINERIE,
        "name": "La Maroquinerie",
        "type": "Venue",
        "address": "23 rue Boyer",
        "postal_code": "75020",
        "city": "Paris",  # MusicBrainz area is "20e arrondissement de Paris"
        "latitude": "48.868528",
        "longitude": "2.392083",
    }


def test_quotes_in_names_are_escaped():
    requests: list[httpx.Request] = []
    musicbrainz.search_places('The "Big" Room', "", client=mb_client({"places": []}, requests=requests))
    assert requests[0].url.params["query"] == 'place:"The \\"Big\\" Room"'


def test_place_has_official_website():
    requests: list[httpx.Request] = []
    fields = musicbrainz.place(MAROQUINERIE, client=mb_client(PLACE, requests=requests))
    assert requests[0].url.params["inc"] == "url-rels"
    assert fields["website"] == "http://www.lamaroquinerie.fr/"


def test_busy_server_is_retried(waits):
    assert musicbrainz.place(MAROQUINERIE, client=mb_client(503, 503, PLACE))["name"] == "La Maroquinerie"
    assert [w for w in waits if w > 1] == [2, 4]


def test_still_busy_after_retries():
    with pytest.raises(musicbrainz.MusicBrainzError, match="busy"):
        musicbrainz.place(MAROQUINERIE, client=mb_client(503, 503, 503))


def test_country_after_the_city_is_dropped():
    fields = musicbrainz._venue_fields(
        {"id": "x", "name": "Glazart", "address": "7-15 avenue de la porte de la Villette 75019 Paris, France"}
    )
    assert (fields["address"], fields["postal_code"], fields["city"]) == (
        "7-15 avenue de la porte de la Villette",
        "75019",
        "Paris",
    )


def test_address_without_french_postal_code_kept_whole():
    fields = musicbrainz._venue_fields(
        {"id": "x", "name": "Paradiso", "address": "Weteringschans 6-8, Amsterdam", "area": {"name": "Amsterdam"}}
    )
    assert (fields["address"], fields["postal_code"], fields["city"]) == (
        "Weteringschans 6-8, Amsterdam",
        "",
        "Amsterdam",
    )


@pytest.fixture
def admin_musicbrainz(monkeypatch):
    """The admin creates its own client: give it the fake network."""
    make_client = musicbrainz.make_client

    def use(*answers):
        transport = mock_transport(*answers)
        monkeypatch.setattr(musicbrainz, "make_client", lambda: make_client(transport=transport))

    return use


@pytest.mark.django_db
def test_use_this_opens_prefilled_add_form(admin_client, admin_musicbrainz):
    admin_musicbrainz(PLACE)
    response = admin_client.get(f"/admin/concerts/venue/musicbrainz/{MAROQUINERIE}/")
    url = urlparse(response.url)
    assert url.path == "/admin/concerts/venue/add/"
    assert parse_qs(url.query)["website"] == ["http://www.lamaroquinerie.fr/"]
    form = admin_client.get(response.url).content.decode()
    assert 'value="23 rue Boyer"' in form
    assert 'value="48.868528"' in form
    assert not Venue.objects.exists()  # nothing saved until the person submits the form


def test_real_network_is_blocked():
    with pytest.raises(RuntimeError, match="Network access in tests"):
        musicbrainz.search_places("Le Plan", "", client=musicbrainz.make_client())
