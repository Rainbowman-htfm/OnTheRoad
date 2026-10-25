"""One venue search across MusicBrainz and OpenStreetMap, and the OpenStreetMap credit on public pages."""

import json
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from django.utils import timezone

from concerts import musicbrainz, openstreetmap, public, venue_search
from concerts.models import Band, Concert, Venue

from .test_musicbrainz import MAROQUINERIE, PLACE, SEARCH, mock_transport

FIXTURES = Path(__file__).parent / "fixtures"
OSM_MAROQUINERIE = json.loads((FIXTURES / "nominatim_search_maroquinerie.json").read_text())
OSM_TITI_TWISTER = json.loads((FIXTURES / "nominatim_search_titi_twister.json").read_text())

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(musicbrainz, "_sleep", lambda seconds: None)


@pytest.fixture
def network(monkeypatch):
    """The admin creates its own clients: give each source its fake network."""

    def use(musicbrainz_answers: list[object], openstreetmap_answers: list[object]) -> None:
        for module, answers in ((musicbrainz, musicbrainz_answers), (openstreetmap, openstreetmap_answers)):
            transport = mock_transport(*answers)
            make_client = module.make_client
            monkeypatch.setattr(module, "make_client", lambda make_client=make_client, t=transport: make_client(t))

    return use


def search(admin_client, name: str) -> str:
    return admin_client.get("/admin/concerts/venue/search/", {"name": name, "city": "Paris"}).content.decode()


def test_same_venue_on_both_is_one_result():
    mb = musicbrainz.search_places("La Maroquinerie", "Paris", musicbrainz.make_client(mock_transport(SEARCH)))
    osm = openstreetmap.search_places(
        "La Maroquinerie", "Paris", openstreetmap.make_client(mock_transport(OSM_MAROQUINERIE))
    )
    merged = venue_search.merge(mb, osm)
    maroquinerie = next(result for result in merged if result.get("musicbrainz_id") == MAROQUINERIE)
    assert maroquinerie["osm_id"] == "node/1402597553"  # about 2 m apart: the same venue
    assert maroquinerie["address"] == "23 rue Boyer"  # MusicBrainz's values come first
    assert maroquinerie["website"] == "https://www.lamaroquinerie.fr/"  # OpenStreetMap fills what it lacks
    assert len(merged) == len(mb)  # the OpenStreetMap result was not listed a second time


def test_merged_venue_takes_the_coordinates_from_the_map():
    town = {"name": "Big Band Café", "city": "Hérouville-Saint-Clair"}
    mb = [{**town, "musicbrainz_id": "1", "latitude": "49.194479", "longitude": "-0.34033"}]
    osm = [{**town, "osm_id": "way/1", "latitude": "49.210809", "longitude": "-0.334083"}]
    [merged] = venue_search.merge(mb, osm)
    assert (merged["musicbrainz_id"], merged["osm_id"]) == ("1", "way/1")
    assert (merged["latitude"], merged["longitude"]) == ("49.210809", "-0.334083")


def test_same_name_far_apart_in_another_city_is_another_venue():
    here = {"name": "Le Plan", "city": "Ris-Orangis", "latitude": "48.65", "longitude": "2.41"}
    assert venue_search.same_venue(here, {**here, "name": "LE PLAN"})
    assert venue_search.same_venue(here, {**here, "city": "Ris Orangis Cedex", "latitude": "48.6505"})  # 60 m
    # The Big Band Café: MusicBrainz's coordinates are 1.8 km off, the city tells it is the same venue
    assert venue_search.same_venue(here, {**here, "latitude": "48.666"})
    assert not venue_search.same_venue(here, {**here, "city": "Évry", "latitude": "48.666"})
    no_coordinates = {"name": "Le Plan", "city": "Ris-Orangis", "latitude": "", "longitude": ""}
    assert venue_search.same_venue(no_coordinates, {**no_coordinates, "city": "ris orangis"})
    assert not venue_search.same_venue(no_coordinates, {**no_coordinates, "city": "Paris"})


def test_admin_search_lists_both_sources_once(admin_client, network):
    network([SEARCH], [OSM_MAROQUINERIE])
    page = search(admin_client, "La Maroquinerie")
    assert page.count("node/1402597553") >= 1
    assert f'musicbrainz.org/place/{MAROQUINERIE}" target="_blank" rel="noopener">MusicBrainz</a> + <a' in page


def test_use_this_on_a_merged_result_needs_no_second_request(admin_client, network):
    network([SEARCH], [OSM_MAROQUINERIE])  # no answer for a second MusicBrainz request
    page = search(admin_client, "La Maroquinerie")
    row = page[page.index(f"musicbrainz.org/place/{MAROQUINERIE}") :]
    use_url = row.split('class="button" href="')[1].split('"')[0].replace("&amp;", "&")
    # OpenStreetMap gave the website: straight to the form, with both identifiers
    assert urlparse(use_url).path == "/admin/concerts/venue/add/"
    initial = parse_qs(urlparse(use_url).query)
    assert initial["musicbrainz_id"] == [MAROQUINERIE] and initial["osm_id"] == ["node/1402597553"]
    assert initial["website"] == ["https://www.lamaroquinerie.fr/"]
    assert not Venue.objects.exists()  # nothing saved until the person submits the form


def test_admin_search_marks_venues_already_added(admin_client, network):
    venue = Venue.objects.create(name="Titi Twister", city="Fontanil-Cornillon", osm_id="node/3772940557")
    network([{"places": []}], [OSM_TITI_TWISTER])
    page = search(admin_client, "Titi Twister")
    assert f"/admin/concerts/venue/{venue.pk}/change/" in page and "Use this" not in page


def test_one_source_down_still_shows_the_other(admin_client, network):
    network([503, 503, 503], [OSM_TITI_TWISTER])
    page = search(admin_client, "Titi Twister")
    assert "MusicBrainz is busy" in page and "Use this" in page


def test_search_requires_admin(client):
    response = client.get("/admin/concerts/venue/search/", {"name": "Titi"})
    assert response.status_code == 302 and "/admin/login/" in response.url


def test_public_pages_credit_openstreetmap_only_when_needed(settings, tmp_path):
    settings.PUBLIC_DIR = tmp_path
    band = Band.objects.create(name="Damantra", slug="damantra")
    venue = Venue.objects.create(name="Le Plan", city="Ris-Orangis")
    Concert.objects.create(band=band, venue=venue, starts_at=timezone.now() + timedelta(days=3))
    public.build_band(band)
    assert "OpenStreetMap contributors" not in (tmp_path / "damantra" / "index.html").read_text()
    other = Venue.objects.create(name="Titi Twister", city="Fontanil-Cornillon", osm_id="node/3772940557")
    Concert.objects.create(band=band, venue=other, starts_at=timezone.now() + timedelta(days=5))
    public.build_band(band)
    for name in ("index.html", "widget/index.html"):
        assert "Some venue data © " in (tmp_path / "damantra" / name).read_text(), name
    Venue.objects.filter(pk=venue.pk).update(osm_id="node/1")
    assert ">Venue data © " in public.band_page(band, list(public.upcoming_concerts(band)), [], False)
    public.build_home()  # "Next show" shows a venue from OpenStreetMap
    assert "OpenStreetMap contributors" in (tmp_path / "index.html").read_text()


# From a concert, "+" next to the venue opens "Add venue" in a small window marked with these parameters
POPUP = {"_popup": "1", "_to_field": "id"}


def test_find_link_in_the_small_window_keeps_it_small(admin_client):
    page = admin_client.get("/admin/concerts/venue/add/", POPUP).content.decode()
    assert 'href="/admin/concerts/venue/search/?_popup=1&amp;_to_field=id"' in page


def test_use_this_in_the_small_window_saves_into_the_concert(admin_client, network):
    network([{"places": []}], [OSM_TITI_TWISTER])
    page = admin_client.get("/admin/concerts/venue/search/", {"name": "Titi Twister", **POPUP}).content.decode()
    assert '<input type="hidden" name="_popup" value="1">' in page  # a new search stays in the window
    use_url = page.split('class="button" href="')[1].split('"')[0].replace("&amp;", "&")
    assert parse_qs(urlparse(use_url).query)["_popup"] == ["1"]
    form = admin_client.get(use_url).content.decode()
    assert 'name="_popup"' in form  # Django then closes the window after "Save" and fills in the venue


def test_musicbrainz_use_this_in_the_small_window_keeps_it_small(admin_client, network):
    network([PLACE], [])
    add_url = admin_client.get(f"/admin/concerts/venue/musicbrainz/{MAROQUINERIE}/", POPUP).url
    assert parse_qs(urlparse(add_url).query)["_popup"] == ["1"]


def test_venue_already_added_is_picked_into_the_concert(admin_client, network):
    venue = Venue.objects.create(name="Titi Twister", city="Fontanil-Cornillon", osm_id="node/3772940557")
    network([{"places": []}], [OSM_TITI_TWISTER])
    page = admin_client.get("/admin/concerts/venue/search/", {"name": "Titi Twister", **POPUP}).content.decode()
    pick_url = f"/admin/concerts/venue/{venue.pk}/pick/?_popup=1&amp;_to_field=id"
    assert pick_url in page
    response = admin_client.get(pick_url.replace("&amp;", "&")).content.decode()
    assert "data-popup-response" in response and f"&quot;value&quot;: &quot;{venue.pk}&quot;" in response


RUE_BOYER_12 = json.loads((FIXTURES / "nominatim_address_12_rue_boyer.json").read_text())


def test_search_by_address_alone_fills_in_coordinates_and_the_name_searched(admin_client, network):
    network([{"places": []}], [[], RUE_BOYER_12])
    page = admin_client.get(
        "/admin/concerts/venue/search/", {"name": "Le Petit Bain", "address": "12 rue Boyer", "city": "Paris"}
    ).content.decode()
    assert "address only" in page and "mlat=48.867604" in page
    use_url = page.split('class="button" href="')[1].split('"')[0].replace("&amp;", "&")
    initial = parse_qs(urlparse(use_url).query)
    assert initial["name"] == ["Le Petit Bain"] and initial["latitude"] == ["48.867604"]
    assert "osm_id" not in initial  # the building is not the venue


def test_search_by_address_without_a_name(admin_client, network):
    network([], [RUE_BOYER_12])
    page = admin_client.get("/admin/concerts/venue/search/", {"address": "12 rue Boyer", "city": "Paris"})
    assert "address only" in page.content.decode()


def test_venue_found_by_name_and_by_address_is_listed_once(admin_client, network):
    rue_boyer_23 = json.loads((FIXTURES / "nominatim_address_rue_boyer.json").read_text())
    network([{"places": []}], [OSM_MAROQUINERIE, rue_boyer_23])
    page = admin_client.get(
        "/admin/concerts/venue/search/", {"name": "La Maroquinerie", "address": "23 rue Boyer", "city": "Paris"}
    ).content.decode()
    assert page.count("openstreetmap.org/node/1402597553") == 1
