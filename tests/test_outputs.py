"""Feeds, widget, MusicEvent data and the admin "Share" page."""

import json
import re
import xml.etree.ElementTree as ET
from datetime import timedelta

import pytest
from django.utils import timezone
from icalendar import Calendar

from concerts import public
from concerts.models import Band, Concert, Venue

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def site(settings, tmp_path):
    settings.PUBLIC_DIR = tmp_path
    settings.SITE_URL = "https://dates.example"
    return tmp_path


@pytest.fixture
def band():
    band = Band.objects.create(name="Damantra", slug="damantra")
    band.links.create(url="https://damantra.bandcamp.com")
    return band


@pytest.fixture
def venue():
    return Venue.objects.create(name="Le Plan", city="Ris-Orangis", postal_code="91130")


def add_concert(band, venue, days, **fields):
    return Concert.objects.create(band=band, venue=venue, starts_at=timezone.now() + timedelta(days=days), **fields)


def built(site, band, name):
    public.build_band(band)
    return (site / band.slug / name).read_text()


def test_calendar_keeps_past_and_cancelled_concerts(site, band, venue):
    past = add_concert(band, venue, days=-30)
    cancelled = add_concert(band, venue, days=10, status=Concert.Status.CANCELLED, support_acts="Novelists")
    events = {str(e["uid"]): e for e in Calendar.from_ical(built(site, band, "concerts.ics")).walk("VEVENT")}
    assert set(events) == {f"concert-{past.pk}@dates.example", f"concert-{cancelled.pk}@dates.example"}
    event = events[f"concert-{cancelled.pk}@dates.example"]
    assert event["status"] == "CANCELLED"
    assert event["summary"] == "Damantra at Le Plan"
    assert event["location"] == "Le Plan, 91130 Ris-Orangis"
    assert event["description"] == "Cancelled · with Novelists"
    assert event.decoded("dtstart") == cancelled.starts_at.replace(microsecond=0)  # iCal keeps whole seconds


def test_end_time_in_calendar_and_music_event(site, band, venue):
    concert = add_concert(band, venue, days=3)
    concert.ends_at = concert.starts_at + timedelta(hours=3)
    concert.save()
    [event] = Calendar.from_ical(built(site, band, "concerts.ics")).walk("VEVENT")
    assert event.decoded("dtend") == concert.ends_at.replace(microsecond=0)
    [music_event] = json.loads((site / "damantra" / "events.json").read_text())
    assert music_event["endDate"] == timezone.localtime(concert.ends_at).isoformat()


def test_time_not_announced(site, band, venue):
    concert = add_concert(band, venue, days=3, time_tba=True)
    day = timezone.localtime(concert.starts_at).date()
    assert "time TBA" in built(site, band, "index.html")
    widget = built(site, band, "widget/index.html")
    assert f'datetime="{day.isoformat()}"' in widget  # no invented time in the markup either
    assert re.search(r"<span class=\"time\">[A-Z][a-z]{2}, time TBA</span>", widget)
    [event] = Calendar.from_ical(built(site, band, "concerts.ics")).walk("VEVENT")
    assert event.decoded("dtstart") == day  # a date, not a date and time: an all-day event
    assert event.decoded("dtend") == day + timedelta(days=1)
    [music_event] = json.loads((site / "damantra" / "events.json").read_text())
    assert music_event["startDate"] == day.isoformat()


def test_rss_lists_upcoming_concerts_latest_announced_first(site, band, venue):
    add_concert(band, venue, days=-3)
    first = add_concert(band, venue, days=20, announced_at=timezone.now() - timedelta(days=5))
    latest = add_concert(band, venue, days=40, ticket_url="https://tickets.example/1")
    items = ET.fromstring(built(site, band, "concerts.xml")).findall("channel/item")
    assert [item.findtext("guid") for item in items] == [
        f"concert-{latest.pk}@dates.example",
        f"concert-{first.pk}@dates.example",
    ]
    guid = items[0].find("guid")
    assert guid is not None and guid.get("isPermaLink") == "false"
    assert items[0].findtext("link") == "https://tickets.example/1"
    assert items[1].findtext("link") == "https://dates.example/damantra/"


def test_events_json_matches_the_page(site, band, venue):
    add_concert(band, venue, days=3)
    add_concert(band, venue, days=8, status=Concert.Status.SOLD_OUT)
    page = built(site, band, "index.html")
    blocks = [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page)]
    assert json.loads((site / "damantra" / "events.json").read_text()) == blocks
    assert len(blocks) == 2


def test_widget_lists_dates_and_links_to_the_page(site, band, venue):
    add_concert(band, venue, days=3, ticket_url="https://tickets.example/1")
    widget = built(site, band, "widget/index.html")
    assert 'href="https://tickets.example/1" target="_blank"' in widget
    assert 'href="https://dates.example/damantra/" target="_blank"' in widget
    assert "otr-height" in widget


def test_public_files_load_nothing_from_elsewhere(site, band, venue):
    add_concert(band, venue, days=3)
    for name in ("index.html", "widget/index.html"):
        html = built(site, band, name)
        assert not re.search(r"<(script|img|iframe)[^>]+src=|<link[^>]+stylesheet", html), name


def test_share_page_gives_embed_code_and_fallback_block(admin_client, band, venue):
    add_concert(band, Venue.objects.create(name='The "<Big>" Room', city="Paris"), days=3)
    html = admin_client.get(f"/admin/concerts/band/{band.pk}/share/").content.decode()
    assert "https://dates.example/damantra/concerts.ics" in html
    assert 'id="code"' in html
    # The fallback block is shown as text, escaped, and stays valid JSON once copied
    block = re.search(r'<textarea id="block"[^>]*>&lt;script type="application/ld\+json"&gt;(.*?)&lt;/script&gt;', html)
    assert block and "&quot;" in block[1]


def test_embed_code(settings, band):
    settings.SITE_URL = "https://label.example/dates"  # an instance published under a path
    band.name = 'The "Band"'
    code = public.embed_code(band)
    assert 'src="https://label.example/dates/damantra/widget/__QUERY__"' in code
    assert 'title="The &quot;Band&quot; concerts"' in code
    assert 'var origin = "https://label.example"' in code  # the origin, without the path
    assert 'fetch("https://label.example/dates/damantra/events.json"' in code
    assert "textContent = JSON.stringify(events)" in code  # never innerHTML


def test_iframe_only_code_has_no_script(band):
    code = public.embed_code(band, "concerts/embed_iframe.html")
    assert 'src="https://dates.example/damantra/widget/__QUERY__"' in code
    assert "height:__HEIGHT__px" in code
    assert "<script" not in code


def test_share_page_requires_admin(client, band):
    response = client.get(f"/admin/concerts/band/{band.pk}/share/")
    assert response.status_code == 302 and "/admin/login/" in response.url
