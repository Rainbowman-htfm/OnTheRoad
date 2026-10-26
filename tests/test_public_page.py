import json
import re
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from concerts import public
from concerts.models import Account, Band, Concert, Venue

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
    return Venue.objects.create(
        name="Le Plan", city="Ris-Orangis", country="FR", latitude="48.651300", longitude="2.416500"
    )


def add_concert(band, venue, days, **fields):
    return Concert.objects.create(band=band, venue=venue, starts_at=timezone.now() + timedelta(days=days), **fields)


def page(band):
    concerts = list(public.upcoming_concerts(band))
    return public.band_page(band, concerts, [link.url for link in band.links.all()], False)


def json_ld_blocks(html):
    return [json.loads(block) for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html)]


def test_only_upcoming_concerts(band, venue):
    add_concert(band, venue, days=-2).other_bands.create(name="Past band")
    add_concert(band, venue, days=10).other_bands.create(name="Future band")
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
    assert sold_out["location"]["address"]["addressCountry"] == "FR"
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


def test_venue_name_links_to_its_website(band, venue, public_dir):
    other = Venue.objects.create(name="Le Gibus", city="Paris")
    venue.website = "https://leplan.com"
    venue.save()
    add_concert(band, venue, days=1)
    add_concert(band, other, days=2)
    html = page(band)
    assert '<a href="https://leplan.com" rel="noopener">Le Plan</a>' in html
    assert "Le Gibus</a>" not in html  # no website: plain text
    assert json_ld_blocks(html)[0]["location"]["url"] == "https://leplan.com"
    public.build_home()
    assert 'href="https://leplan.com"' in (public_dir / "index.html").read_text()


def test_query_count_does_not_grow_with_concerts(band, venue, django_assert_num_queries):
    for days in range(1, 6):
        add_concert(band, venue, days=days)
    # The upcoming concerts with their venues and their other bands, the band's links, whether it has members,
    # every concert for the calendar with their other bands
    with django_assert_num_queries(6):
        public.build_band(band)


def test_json_ld_cannot_close_script_tag(band):
    venue = Venue.objects.create(name="</script><script>alert(1)</script>", city="Paris")
    add_concert(band, venue, days=1)
    html = page(band)
    assert "<script>alert(1)" not in html
    assert json_ld_blocks(html)[0]["location"]["name"] == venue.name


def test_saving_a_concert_rewrites_the_page(band, venue, public_dir, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        add_concert(band, venue, days=5).other_bands.create(name="Novelists")
    assert "with Novelists" in (public_dir / "damantra" / "index.html").read_text()
    with django_capture_on_commit_callbacks(execute=True):
        venue.name = "Le Plan (Big Room)"
        venue.save()
    assert "Le Plan (Big Room)" in (public_dir / "damantra" / "index.html").read_text()
    with django_capture_on_commit_callbacks(execute=True):
        band.delete()
    assert not (public_dir / "damantra").exists()


def test_home_page_lists_bands(band, public_dir, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        Band.objects.create(name="DVM Spero", slug="dvm-spero")
    home = (public_dir / "index.html").read_text()
    assert '<a class="name" href="damantra/">Damantra</a>' in home
    assert '<a class="name" href="dvm-spero/">DVM Spero</a>' in home
    with django_capture_on_commit_callbacks(execute=True):
        Band.objects.get(slug="dvm-spero").delete()
    assert "DVM Spero" not in (public_dir / "index.html").read_text()


def test_public_files_are_served(client, band, public_dir):
    public.build_all()
    assert client.get("/").status_code == 200  # the home page
    assert client.get("/damantra/").status_code == 200
    assert client.get("/damantra/events.json")["Access-Control-Allow-Origin"] == "*"


def test_build_all_removes_pages_of_renamed_bands(band, public_dir):
    (public_dir / "old-slug").mkdir()
    public.build_all()
    assert (public_dir / "damantra" / "index.html").exists()
    assert not (public_dir / "old-slug").exists()


def test_home_page_bands_sorted_by_next_show(band, venue, public_dir):
    Band.objects.create(name="Faith In Agony", slug="faith-in-agony")
    later = Band.objects.create(name="Splenius", slug="splenius")
    Concert.objects.create(band=later, venue=venue, starts_at=timezone.now() + timedelta(days=30))
    Concert.objects.create(band=band, venue=venue, starts_at=timezone.now() + timedelta(days=3))
    public.build_home()
    home = (public_dir / "index.html").read_text()
    # Soonest show first; bands without one last
    assert home.index(">Damantra<") < home.index(">Splenius<") < home.index(">Faith In Agony<")
    assert "No upcoming shows" in home and "Next show: " in home
    assert public.monogram(Band(name="Faith In Agony", slug="faith-in-agony"))["initials"] == "FA"
    assert 'href="webcal://' in home


def test_home_page_contact_only_when_configured(band, public_dir, settings):
    settings.CONTACT_EMAIL = ""
    public.build_home()
    assert "mailto:" not in (public_dir / "index.html").read_text()
    settings.CONTACT_EMAIL = "contact@example.org"
    public.build_home()
    assert (public_dir / "index.html").read_text().count('href="mailto:contact@example.org"') == 2


def test_your_data_page_is_linked_and_kept_by_the_nightly_build(band, public_dir):
    public.build_all()
    page = (public_dir / "data.html").read_text()
    assert "CC0" in page and "OpenStreetMap contributors" in page
    assert 'href="data.html"' in (public_dir / "index.html").read_text()
    assert 'href="../data.html"' in (public_dir / "damantra" / "index.html").read_text()


def test_source_code_linked_from_home_and_data_pages(public_dir, settings):
    settings.SOURCE_URL = "https://code.example.org/otr"
    public.build_home()
    for page in ["index.html", "data.html"]:
        assert 'href="https://code.example.org/otr"' in (public_dir / page).read_text()


def test_guide_for_bands_is_built_and_linked(public_dir, client, settings):
    settings.CONTACT_EMAIL = "contact@example.org"
    public.build_home()
    guide = (public_dir / "guide.html").read_text()
    assert "Finding the venue" in guide and "30 minutes" in guide and "mailto:contact@example.org" in guide
    assert f'"New" for {public.NEW_DAYS} days' in guide
    assert 'href="guide.html"' in (public_dir / "index.html").read_text()
    client.force_login(Account.objects.create_superuser("admin@example.org"))
    assert f'href="{public.site_url()}/guide.html"' in client.get("/admin/").content.decode()


def test_other_bands_on_the_site_link_to_their_page(band, venue, settings):
    settings.SITE_URL = "https://dates.example"
    splenius = Band.objects.create(name="Splenius", slug="splenius")
    concert = add_concert(band, venue, days=3)
    concert.other_bands.create(band=splenius)
    concert.other_bands.create(name="Novelists")
    assert 'with <a href="https://dates.example/splenius/">Splenius</a>, Novelists' in page(band)


def test_other_band_is_a_band_or_a_name(band, venue):
    concert = add_concert(band, venue, days=3)
    with pytest.raises(IntegrityError):
        concert.other_bands.create(band=band, name="Novelists")


def test_other_band_renamed_or_deleted_updates_the_page(band, venue, public_dir, django_capture_on_commit_callbacks):
    splenius = Band.objects.create(name="Splenius", slug="splenius")
    add_concert(band, venue, days=3).other_bands.create(band=splenius)
    with django_capture_on_commit_callbacks(execute=True):
        splenius.name = "Splenius (FR)"
        splenius.save()
    assert ">Splenius (FR)</a>" in (public_dir / "damantra" / "index.html").read_text()
    with django_capture_on_commit_callbacks(execute=True):
        splenius.delete()
    # It still played that night: its name stays, without a link to a page that is gone
    assert "with Splenius (FR)</span>" in (public_dir / "damantra" / "index.html").read_text()


def test_one_save_builds_each_page_once(band, venue, monkeypatch, django_capture_on_commit_callbacks):
    built: list[str] = []
    monkeypatch.setattr(public, "build_band", lambda band: built.append(band.slug))
    monkeypatch.setattr(public, "build_home", lambda: built.append("home"))
    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():  # one save in the admin
        concert = add_concert(band, venue, days=3)
        for name in ("Novelists", "Splenius", "Landmvrks"):
            concert.other_bands.create(name=name)
    assert built == ["damantra", "home"]  # not once per signal: 4 of them
