"""The band's admin page: its upcoming concerts, and a quick way to add one."""

from datetime import timedelta

import pytest
from django.utils import timezone

from concerts.models import Band, Concert, Venue

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def site(settings, tmp_path):
    settings.PUBLIC_DIR = tmp_path


def test_band_page_lists_upcoming_concerts_and_adds_one(admin_client):
    band = Band.objects.create(name="Damantra", slug="damantra")
    venue = Venue.objects.create(name="Le Plan", city="Ris-Orangis")
    for days, support in ((-3, "Past band"), (3, "Next band")):
        Concert.objects.create(
            band=band, venue=venue, starts_at=timezone.now() + timedelta(days=days), support_acts=support
        )
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
