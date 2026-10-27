"""The band's admin page: its upcoming concerts, and a quick way to add one."""

from datetime import timedelta

import pytest
from django.forms import modelform_factory
from django.utils import timezone

from concerts.admin import ConcertForm
from concerts.models import Band, Concert, Venue

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def site(settings, tmp_path):
    settings.PUBLIC_DIR = tmp_path


def test_band_page_lists_upcoming_concerts_and_adds_one(admin_client):
    band = Band.objects.create(name="Damantra", slug="damantra")
    venue = Venue.objects.create(name="Le Plan", city="Ris-Orangis")
    for days, other in ((-3, "Past band"), (3, "Next band")):
        concert = Concert.objects.create(band=band, venue=venue, starts_at=timezone.now() + timedelta(days=days))
        concert.other_bands.create(name=other)  # in the concert's block
    page = admin_client.get(f"/admin/concerts/band/{band.pk}/change/").content.decode()
    assert "Next band" in page and "Past band" not in page  # past ones are in the list of concerts
    add_url = f"/admin/concerts/concert/add/?band={band.pk}"
    assert f'href="{add_url}"' in page
    assert f'<option value="{band.pk}" selected>' in admin_client.get(add_url).content.decode()  # band chosen
    form = {
        "band": band.pk,
        "venue": venue.pk,
        "starts_at_0": "2030-05-01",
        "starts_at_1": "20:30",
        "status": "planned",
        "entry": "tickets",
        "announced_at_0": "2030-01-01",
        "announced_at_1": "12:00",
    }
    assert admin_client.post(add_url, form).url == f"/admin/concerts/band/{band.pk}/change/"  # back to the band
    assert admin_client.post(add_url, form | {"_addanother": "1"}).url == add_url  # the next one, same band


def test_other_bands_typed_as_one_line():
    band = Band.objects.create(name="Damantra", slug="damantra")
    sangham = Band.objects.create(name="Sangham", slug="sangham")
    concert = Concert.objects.create(band=band, venue=Venue.objects.create(name="Le Plan"), starts_at=timezone.now())
    Form = modelform_factory(Concert, form=ConcertForm, fields=["other_bands"])

    def typed(line):
        form = Form({"other_bands": line}, instance=concert)
        assert form.is_valid()
        form.save()
        return [(act.band, act.name) for act in concert.other_bands.all()]

    assert typed("Novelists,  sangham ,") == [(None, "Novelists"), (sangham, "")]  # linked to the band's page
    assert Form(instance=concert).initial["other_bands"] == "Novelists, Sangham"
    Band.objects.create(name="Novelists", slug="novelists-fr")
    Band.objects.create(name="Novelists", slug="novelists-us")
    assert typed("Novelists") == [(None, "Novelists")]  # two bands of that name: not linked
    assert typed("") == []
    concert.other_bands.create(name="Novelist, Sangham")  # one row, typed with a comma in an older version
    assert typed("Novelist, Sangham") == [(None, "Novelist"), (sangham, "")]  # the same text splits it
    unchanged = list(concert.other_bands.values_list("pk", flat=True))
    assert typed("Novelist, Sangham") and list(concert.other_bands.values_list("pk", flat=True)) == unchanged


def test_saving_marks_the_dates_checked(admin_client, settings, django_capture_on_commit_callbacks):
    band = Band.objects.create(name="Damantra", slug="damantra")
    venue = Venue.objects.create(name="Le Plan", city="Ris-Orangis")
    form = {
        "band": band.pk,
        "venue": venue.pk,
        "starts_at_0": "2030-05-01",
        "starts_at_1": "20:30",
        "status": "planned",
        "entry": "tickets",
        "announced_at_0": "2030-01-01",
        "announced_at_1": "12:00",
    }
    with django_capture_on_commit_callbacks(execute=True):  # the page rebuilt after the save
        admin_client.post("/admin/concerts/concert/add/", form)  # a concert saved: its band's dates were checked
    band.refresh_from_db()
    assert band.checked_at and timezone.now() - band.checked_at < timedelta(minutes=1)
    page = (settings.PUBLIC_DIR / "damantra" / "index.html").read_text()
    assert f"Dates checked on {timezone.localdate():%-d %b %Y} · " in page
