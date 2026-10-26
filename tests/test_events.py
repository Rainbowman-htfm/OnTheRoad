"""Events: a festival or a night with a name, shown before the venue; festivals are found on MusicBrainz."""

import json
import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest
from django.contrib.auth.models import Group
from django.utils import timezone
from icalendar import Calendar

from concerts import musicbrainz, public
from concerts.models import Account, Band, Concert, Event, Membership, Venue

from .test_musicbrainz import mock_transport

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIXTURES / "musicbrainz_search_hellfest.json").read_text())
SERIES = json.loads((FIXTURES / "musicbrainz_series_hellfest.json").read_text())
HELLFEST_FRANCE = "728d4b81-b03d-4111-baf6-4d4c08c05798"

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def site(settings, tmp_path):
    settings.PUBLIC_DIR = tmp_path
    return tmp_path


@pytest.fixture(autouse=True)
def no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(musicbrainz, "_sleep", lambda seconds: None)


@pytest.fixture
def network(monkeypatch):
    """The admin creates its own MusicBrainz client: give it a fake network answering these, in order."""

    def use(answers: list[object]) -> None:
        transport, make_client = mock_transport(*answers), musicbrainz.make_client
        monkeypatch.setattr(musicbrainz, "make_client", lambda: make_client(transport))

    return use


@pytest.fixture
def band():
    return Band.objects.create(name="Damantra", slug="damantra")


@pytest.fixture
def venue():
    return Venue.objects.create(name="Val de Moine", city="Clisson")


@pytest.fixture
def hellfest():
    return Event.objects.create(name="Hellfest", website="https://hellfest.fr")


def add_concert(band, venue, days, **fields):
    return Concert.objects.create(band=band, venue=venue, starts_at=timezone.now() + timedelta(days=days), **fields)


def test_event_name_first_then_the_venue_smaller(site, band, venue, hellfest):
    add_concert(band, venue, days=3, event=hellfest)
    public.build_band(band)
    page = (site / "damantra" / "index.html").read_text()
    shown = page.split("<body>")[1]
    assert '<a href="https://hellfest.fr" rel="noopener">Hellfest</a>' in shown
    assert '<span class="at">Val de Moine</span>' in shown  # under it, smaller
    [block] = re.findall(r'<script type="application/ld\+json">(.*?)</script>', page)
    event = json.loads(block)
    assert event["name"] == "Damantra at Hellfest"
    assert event["superEvent"] == {"@type": "Event", "name": "Hellfest", "url": "https://hellfest.fr"}
    assert event["location"]["name"] == "Val de Moine"  # where it is stays the venue
    [item] = Calendar.from_ical((site / "damantra" / "concerts.ics").read_bytes()).walk("VEVENT")
    assert item["summary"] == "Damantra at Hellfest" and str(item["location"]).startswith("Val de Moine")
    title = ET.fromstring((site / "damantra" / "concerts.xml").read_text()).findtext("channel/item/title")
    assert title and title.endswith("– Hellfest, Clisson")


def test_search_festivals_on_musicbrainz():
    requests: list = []
    found = musicbrainz.search_festivals(
        "Hellfest", client=musicbrainz.make_client(mock_transport(SEARCH, requests=requests))
    )
    assert requests[0].url.path == "/ws/2/series/"
    assert requests[0].url.params["query"] == 'series:"Hellfest" AND type:festival'
    assert found == [
        {"musicbrainz_id": HELLFEST_FRANCE, "name": "Hellfest", "disambiguation": "France"},
        {"musicbrainz_id": "2c6b5d3e-88ae-4621-b629-9ffcad4c8514", "name": "Hellfest", "disambiguation": "USA"},
    ]
    website = musicbrainz.festival_website(HELLFEST_FRANCE, client=musicbrainz.make_client(mock_transport(SERIES)))
    assert website == "https://www.hellfest.fr/"


def test_find_a_festival_and_use_it(admin_client, network):
    Event.objects.create(name="Hellfest", musicbrainz_id="2c6b5d3e-88ae-4621-b629-9ffcad4c8514")
    network([SEARCH, SERIES])
    page = admin_client.get("/admin/concerts/event/search/", {"name": "Hellfest"}).content.decode()
    assert "France" in page and "USA" in page
    assert page.count("already added") == 1  # the American one
    [use_url] = re.findall(r'class="button" href="([^"]+)"', page)
    form = admin_client.get(use_url.replace("&amp;", "&"))
    assert form.url == "/admin/concerts/event/add/?" + urlencode(
        {"name": "Hellfest", "musicbrainz_id": HELLFEST_FRANCE, "website": "https://www.hellfest.fr/"}
    )


def test_use_this_when_musicbrainz_is_busy_still_opens_the_form(admin_client, network):
    network([503, 503, 503])
    response = admin_client.get(f"/admin/concerts/event/musicbrainz/{HELLFEST_FRANCE}/", {"name": "Hellfest"})
    assert response.url == "/admin/concerts/event/add/?" + urlencode(
        {"name": "Hellfest", "musicbrainz_id": HELLFEST_FRANCE}
    )


def test_event_already_added_goes_into_the_concert(admin_client, hellfest):
    response = admin_client.get(f"/admin/concerts/event/{hellfest.pk}/pick/", {"_popup": "1"})
    assert f"&quot;value&quot;: &quot;{hellfest.pk}&quot;" in response.content.decode()


def test_renaming_an_event_rebuilds_its_bands_pages(site, band, venue, hellfest, django_capture_on_commit_callbacks):
    add_concert(band, venue, days=3, event=hellfest)
    with django_capture_on_commit_callbacks(execute=True):
        hellfest.name = "Hellfest Open Air"
        hellfest.save()
    assert "Hellfest Open Air" in (site / "damantra" / "index.html").read_text()


def test_band_member_adds_events_but_cannot_change_them(client, band, hellfest):
    account = Account.objects.create_user("member@example.org", is_staff=True)
    account.groups.add(Group.objects.get(name="Band members"))
    Membership.objects.create(account=account, band=band)
    client.force_login(account)
    assert client.get("/admin/concerts/event/add/").status_code == 200
    change = f"/admin/concerts/event/{hellfest.pk}/change/"
    assert client.post(change, {"name": "Other", "website": ""}).status_code == 403
