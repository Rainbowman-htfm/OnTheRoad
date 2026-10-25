"""Pages of bands with no member account: dates listed by On the Road, which the page and the feeds say."""

import xml.etree.ElementTree as ET
from datetime import timedelta

import pytest
from django.utils import timezone
from icalendar import Calendar

from concerts import public
from concerts.models import Account, Band, Concert, Membership, Venue

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def site(settings, tmp_path):
    settings.PUBLIC_DIR = tmp_path
    settings.SITE_URL = "https://dates.example"
    settings.CONTACT_EMAIL = "contact@example.org"
    return tmp_path


@pytest.fixture
def venue():
    return Venue.objects.create(name="Le Plan", city="Ris-Orangis")


def band_with_a_concert(name, venue, claimed):
    band = Band.objects.create(name=name, slug=name.lower())
    band.links.create(url="https://piaille.fr/@" + band.slug)
    Concert.objects.create(band=band, venue=venue, starts_at=timezone.now() + timedelta(days=3))
    if claimed:
        Membership.objects.create(account=Account.objects.create_user(f"{band.slug}@example.org"), band=band)
    return band


def test_unclaimed_page_says_so_and_offers_to_claim_help_or_remove(site, venue):
    band = band_with_a_concert("Damantra", venue, claimed=False)
    public.build_band(band)
    page = (site / "damantra" / "index.html").read_text()
    assert "maintained by On the Road, not by the band" in page
    assert 'href="mailto:contact@example.org?subject=Claim%20Damantra&amp;body=' in page
    assert "subject=Help%20with%20Damantra" in page and "subject=Remove%20Damantra" in page
    # We entered the band's links, so the page does not claim to be theirs
    assert 'href="https://piaille.fr/@damantra" rel="noopener"' in page


def test_claimed_page_is_the_bands(site, venue):
    band = band_with_a_concert("Damantra", venue, claimed=True)
    public.build_band(band)
    page = (site / "damantra" / "index.html").read_text()
    assert "On the Road, not by the band" not in page and "mailto:" not in page
    assert 'href="https://piaille.fr/@damantra" rel="me noopener"' in page  # verified link on Mastodon


def test_feeds_say_who_listed_the_dates(site, venue):
    unclaimed = band_with_a_concert("Damantra", venue, claimed=False)
    claimed = band_with_a_concert("Splenius", venue, claimed=True)
    public.build_home()
    for band in (unclaimed, claimed):
        public.build_band(band)
        rss = ET.fromstring((site / band.slug / "concerts.xml").read_text()).findtext("channel/item/description")
        [event] = Calendar.from_ical((site / band.slug / "concerts.ics").read_bytes()).walk("VEVENT")
        assert (public.UNCLAIMED_NOTE in (rss or "")) is (band == unclaimed)
        assert (public.UNCLAIMED_NOTE in str(event.get("description", ""))) is (band == unclaimed)
    items = ET.fromstring((site / "just-announced.xml").read_text()).findall("channel/item")
    notes = {
        item.findtext("title", "").split(":")[0]: public.UNCLAIMED_NOTE in (item.findtext("description") or "")
        for item in items
    }
    assert notes == {"Damantra": True, "Splenius": False}


def test_source_link_only_when_filled_in_and_no_ticket_link(site, venue):
    band = band_with_a_concert("Damantra", venue, claimed=False)
    public.build_band(band)
    assert ">Source</a>" not in (site / "damantra" / "index.html").read_text()
    band.concerts.update(source_url="https://www.instagram.com/p/abc")
    public.build_band(band)
    assert (
        '<a href="https://www.instagram.com/p/abc" rel="noopener">Source</a>'
        in (site / "damantra" / "index.html").read_text()
    )
    band.concerts.update(ticket_url="https://tickets.example/1")  # the ticket page shows it already
    public.build_band(band)
    assert ">Source</a>" not in (site / "damantra" / "index.html").read_text()


def test_claiming_rebuilds_the_page(site, venue, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        band = band_with_a_concert("Damantra", venue, claimed=False)
    with django_capture_on_commit_callbacks(execute=True):
        Membership.objects.create(account=Account.objects.create_user("member@example.org"), band=band)
    assert "not by the band" not in (site / "damantra" / "index.html").read_text()


def test_home_page_asks_for_bands_only_with_a_contact(site, settings):
    public.build_home()
    assert 'href="mailto:contact@example.org?subject=Suggest%20a%20band' in (site / "index.html").read_text()
    settings.CONTACT_EMAIL = ""
    public.build_home()
    assert "Suggest" not in (site / "index.html").read_text()
