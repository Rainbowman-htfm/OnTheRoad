import json
import re
from datetime import timedelta

import pytest
from django.utils import timezone

from concerts import public
from concerts.models import Band, Concert, Venue

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def public_dir(settings, tmp_path):
    settings.PUBLIC_DIR = tmp_path
    return tmp_path


@pytest.fixture
def band():
    band = Band.objects.create(name="Damantra", slug="damantra", website="https://damantra.fr")
    band.links.create(url="https://www.instagram.com/damantra")
    return band


@pytest.fixture
def venue():
    return Venue.objects.create(name="Le Plan", city="Ris-Orangis", latitude="48.651300", longitude="2.416500")


def add_concert(band, venue, days, **fields):
    return Concert.objects.create(band=band, venue=venue, starts_at=timezone.now() + timedelta(days=days), **fields)


def page(band):
    concerts = list(public.upcoming_concerts(band))
    return public.band_page(band, concerts, [link.url for link in band.links.all()])


def json_ld_blocks(html):
    return [json.loads(block) for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html)]


def test_only_upcoming_concerts(band, venue):
    add_concert(band, venue, days=-2, support_acts="Past band")
    add_concert(band, venue, days=10, support_acts="Future band")
    html = page(band)
    assert "Future band" in html
    assert "Past band" not in html


def test_statuses(band, venue):
    add_concert(band, venue, days=1, ticket_url="https://tickets.example/1")
    add_concert(band, venue, days=2, ticket_url="https://tickets.example/2", status=Concert.Status.SOLD_OUT)
    add_concert(band, venue, days=3, status=Concert.Status.CANCELLED)
    html = page(band)
    assert 'href="https://tickets.example/1"' in html
    assert 'href="https://tickets.example/2"' not in html  # sold out: no ticket button
    assert "Sold out" in html
    assert 'class="concert cancelled"' in html


def test_music_event_block(band, venue):
    add_concert(band, venue, days=1, ticket_url="https://tickets.example/1", status=Concert.Status.SOLD_OUT)
    add_concert(band, venue, days=2, status=Concert.Status.CANCELLED)
    sold_out, cancelled = json_ld_blocks(page(band))
    assert sold_out["@type"] == "MusicEvent"
    assert sold_out["location"]["address"]["addressLocality"] == "Ris-Orangis"
    assert sold_out["location"]["geo"]["latitude"] == 48.6513
    assert sold_out["performer"] == {
        "@type": "MusicGroup",
        "name": "Damantra",
        "url": "https://damantra.fr",
        "sameAs": ["https://www.instagram.com/damantra"],
    }
    assert sold_out["offers"]["availability"] == "https://schema.org/SoldOut"
    assert cancelled["eventStatus"] == "https://schema.org/EventCancelled"
    assert "offers" not in cancelled


def test_query_count_does_not_grow_with_concerts(band, venue, django_assert_num_queries):
    for days in range(1, 6):
        add_concert(band, venue, days=days)
    # The upcoming concerts with their venues, the band's links, then every concert for the calendar
    with django_assert_num_queries(3):
        public.build_band(band)


def test_json_ld_cannot_close_script_tag(band):
    venue = Venue.objects.create(name="</script><script>alert(1)</script>", city="Paris")
    add_concert(band, venue, days=1)
    html = page(band)
    assert "<script>alert(1)" not in html
    assert json_ld_blocks(html)[0]["location"]["name"] == venue.name


def test_saving_a_concert_rewrites_the_page(band, venue, public_dir, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        add_concert(band, venue, days=5, support_acts="Novelists")
    assert "with Novelists" in (public_dir / "damantra" / "index.html").read_text()
    with django_capture_on_commit_callbacks(execute=True):
        venue.name = "Le Plan (Big Room)"
        venue.save()
    assert "Le Plan (Big Room)" in (public_dir / "damantra" / "index.html").read_text()
    with django_capture_on_commit_callbacks(execute=True):
        band.delete()
    assert not (public_dir / "damantra").exists()


def test_build_all_removes_pages_of_renamed_bands(band, public_dir):
    (public_dir / "old-slug").mkdir()
    public.build_all()
    assert (public_dir / "damantra" / "index.html").exists()
    assert not (public_dir / "old-slug").exists()
