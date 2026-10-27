"""Feeds, widget, MusicEvent data and the admin "Share" page."""

import json
import re
import xml.etree.ElementTree as ET
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from icalendar import Calendar

from concerts import public
from concerts.models import Account, Band, Concert, Membership, Venue

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
    cancelled = add_concert(band, venue, days=10, status=Concert.Status.CANCELLED)
    cancelled.other_bands.create(name="Novelists")
    events = {str(e["uid"]): e for e in Calendar.from_ical(built(site, band, "concerts.ics")).walk("VEVENT")}
    assert set(events) == {f"concert-{past.pk}@dates.example", f"concert-{cancelled.pk}@dates.example"}
    event = events[f"concert-{cancelled.pk}@dates.example"]
    assert event["status"] == "CANCELLED"
    assert event["summary"] == "Damantra at Le Plan"
    assert event["location"] == "Le Plan, 91130 Ris-Orangis"
    assert str(event["description"]).startswith("Cancelled · with Novelists\n")
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


def test_band_style_on_page_and_widget(site, venue):
    band = Band.objects.create(
        name="Splenius", slug="splenius", theme="dark", accent_color="#c4161c", font="serif", corners="square"
    )
    for name in ("index.html", "widget/index.html"):
        html = built(site, band, name)
        assert 'data-theme="dark"' in html, name
        assert "--accent: #c4161c; --on-accent: #fff" in html, name  # white text on a dark red button
        assert "--font: Georgia" in html and "--radius: 0" in html, name


def test_unstyled_band_keeps_the_default_look(site, band):
    page = built(site, band, "index.html")
    assert 'data-theme="light"' in page
    assert "--accent" not in page.split("<head>")[0]  # no colour set on the page: the theme decides


def test_invalid_colour_is_refused_and_never_reaches_the_css(site, band):
    band.accent_color = "red; } body { background: url(evil.example)"
    with pytest.raises(ValidationError):
        band.full_clean()
    Band.objects.filter(pk=band.pk).update(accent_color=band.accent_color)  # saved without validation
    band.refresh_from_db()
    assert "evil.example" not in built(site, band, "index.html")


def test_style_page_saves_and_rebuilds(admin_client, site, band, django_capture_on_commit_callbacks):
    url = f"/admin/concerts/band/{band.pk}/style/"
    assert admin_client.get(url).status_code == 200
    form = {"theme": "dark", "font": "serif", "corners": "round", "accent_color": "#c4161c", "use_accent_color": "on"}
    # Text colour sent by its picker but not ticked: it keeps following the theme
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(url, {**form, "text_color": "#00ff00", "background_color": "#ffffff"})
    assert response.status_code == 302
    band.refresh_from_db()
    assert (band.theme, band.accent_color, band.text_color) == ("dark", "#c4161c", "")
    assert "--accent: #c4161c" in (site / "damantra" / "index.html").read_text()


def test_style_preview_shows_the_unsaved_style(admin_client, band):
    response = admin_client.get(
        f"/admin/concerts/band/{band.pk}/style/preview/",
        {"theme": "dark", "font": "mono", "corners": "square", "accent_color": "#c4161c", "use_accent_color": "on"},
    )
    assert response["X-Frame-Options"] == "SAMEORIGIN"
    assert "--accent: #c4161c" in response.content.decode()
    band.refresh_from_db()
    assert band.accent_color == ""  # nothing saved


def test_style_pages_require_admin(client, band):
    for url in (f"/admin/concerts/band/{band.pk}/style/", f"/admin/concerts/band/{band.pk}/style/preview/"):
        assert client.get(url).status_code == 302  # to the login page


def test_free_entry_shown_when_there_is_no_ticket_link(site, band, venue):
    free = add_concert(band, venue, days=3, entry=Concert.Entry.FREE)
    page = built(site, band, "index.html")
    assert "Free entry" in page and "Tickets" not in page
    assert public.music_event(free, [])["isAccessibleForFree"] is True
    assert "Free entry" in built(site, band, "concerts.ics")
    # A link is the event's page, beside "Free entry": no ticket button, nothing to buy in the data
    free.ticket_url = "https://venue.example/event"
    free.save()
    page = built(site, band, "index.html")
    assert 'Free entry<a href="https://venue.example/event" rel="noopener">Details</a>' in page
    assert "Tickets" not in page
    assert public.music_event(free, [])["url"] == "https://venue.example/event"
    assert "offers" not in public.music_event(free, [])


def test_pay_what_you_can(site, band, venue):
    concert = add_concert(band, venue, days=3, entry=Concert.Entry.PAY_WHAT_YOU_CAN)
    assert "Pay what you can" in built(site, band, "index.html")
    assert "Pay what you can" in built(site, band, "concerts.xml")
    assert "isAccessibleForFree" not in public.music_event(concert, [])


def test_rescheduled_concert_shows_its_previous_date(site, band, venue):
    concert = add_concert(band, venue, days=10)
    previous = concert.starts_at
    concert.starts_at += timedelta(days=7)
    concert.save()
    event = public.music_event(concert, [])
    assert event["eventStatus"] == "https://schema.org/EventRescheduled"
    assert event["previousStartDate"] == timezone.localdate(previous).isoformat()
    was = f"was {timezone.localtime(previous):%b} {timezone.localtime(previous).day}"
    assert f"Rescheduled · {was}" in built(site, band, "index.html")
    assert f"Rescheduled ({was})" in built(site, band, "concerts.xml")
    concert.status = Concert.Status.CANCELLED  # cancelled wins over rescheduled
    assert public.music_event(concert, [])["eventStatus"] == "https://schema.org/EventCancelled"


def test_just_announced_on_home_page_feed_and_badge(site, band, venue, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        add_concert(band, venue, days=20)
        # Entered today, but announced elsewhere a month ago: not new
        add_concert(band, venue, days=30, announced_at=timezone.now() - timedelta(days=30))
    home = (site / "index.html").read_text()
    assert home.count("Damantra</strong></a>") == 1  # only the new concert in "Just announced"
    assert (site / "damantra" / "index.html").read_text().count('class="new"') == 1
    feed = ET.fromstring((site / "just-announced.xml").read_text())
    assert [item.findtext("title", "").startswith("Damantra: ") for item in feed.iter("item")] == [True]


def test_just_announced_shows_a_tour_as_one_line(site, band, venue):
    for days, city in [(20, "Lyon"), (10, None), (30, "Lille"), (40, "Nantes"), (50, "Brest")]:
        add_concert(band, Venue.objects.create(name="Club", city=city) if city else venue, days=days)
    public.build_home()
    home = (site / "index.html").read_text()
    assert home.count("Damantra</strong></a>") == 1
    assert "5 new dates, from " in home and " · Ris-Orangis, Lyon, Lille…</li>" in home  # by date


def test_home_page_hides_just_announced_when_empty(site, band):
    public.build_home()
    assert "<h2>Just announced" not in (site / "index.html").read_text()


def test_every_json_ld_block_on_the_page_is_a_complete_music_event(site, band, venue):
    add_concert(band, venue, days=3, ticket_url="https://tickets.example/1")
    add_concert(band, venue, days=5, status=Concert.Status.CANCELLED, time_tba=True)
    page = built(site, band, "index.html")
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.DOTALL)
    assert len(blocks) == 2
    for event in map(json.loads, blocks):
        assert event["@type"] == "MusicEvent"
        assert {"name", "startDate", "eventStatus", "location", "performer"} <= event.keys()
        assert event["location"]["@type"] == "MusicVenue" and event["location"]["address"]["addressLocality"]
        assert event["performer"]["@type"] == "MusicGroup"
    assert blocks[0].count("tickets.example") == 1  # offers.url when there is a ticket link


def test_site_events_json_lists_every_band_for_tools(site, band, venue, settings):
    settings.SITE_URL = "https://dates.example"
    splenius = Band.objects.create(name="Splenius", slug="splenius")
    Membership.objects.create(account=Account.objects.create_user("member@example.org"), band=splenius)
    add_concert(band, venue, days=-2)  # past: left out
    later = add_concert(band, venue, days=9, status=Concert.Status.CANCELLED)  # cancelled: kept, so tools see it
    sooner = add_concert(splenius, venue, days=4)
    sooner.other_bands.create(band=band)
    sooner.other_bands.create(name="Novelists")
    public.build_all()
    events = json.loads((site / "events.json").read_text())
    assert [entry["event"]["identifier"] for entry in events] == [
        f"concert-{concert.pk}@dates.example" for concert in (sooner, later)
    ]  # soonest first, the calendars' UID
    assert events[0]["band"] == {"name": "Splenius", "page": "https://dates.example/splenius/", "claimed": True}
    assert events[1]["band"]["claimed"] is False
    assert events[0]["other_bands"] == [
        {"name": "Damantra", "page": "https://dates.example/damantra/"},
        {"name": "Novelists", "page": ""},
    ]
    assert events[0]["announced_at"] == timezone.localtime(sooner.announced_at).isoformat()
    # The same schema.org event as the band's own events.json
    assert events[1]["event"] in json.loads((site / "damantra" / "events.json").read_text())
