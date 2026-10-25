"""What a band member can do in the admin: only their own bands and concerts; venues are shared."""

import pytest
from django.contrib.auth.models import Group

from concerts.models import Account, Band, Concert, Membership, Venue

pytestmark = pytest.mark.django_db


@pytest.fixture
def band():
    return Band.objects.create(name="Damantra", slug="damantra")


@pytest.fixture
def other_band():
    return Band.objects.create(name="Splenius", slug="splenius")


@pytest.fixture
def venue():
    return Venue.objects.create(name="Le Plan", city="Ris-Orangis")


@pytest.fixture
def member(client, band):
    account = Account.objects.create_user("member@example.org", is_staff=True)
    account.groups.add(Group.objects.get(name="Band members"))  # created by a migration
    Membership.objects.create(account=account, band=band)
    client.force_login(account)
    return account


def add_concert(client, band, venue):
    return client.post(
        "/admin/concerts/concert/add/",
        {
            "band": band.pk,
            "venue": venue.pk,
            "starts_at_0": "2030-05-01",
            "starts_at_1": "20:30",
            "status": "planned",
            "entry": "tickets",
            "announced_at_0": "2030-01-01",
            "announced_at_1": "12:00",
        },
    )


def test_member_sees_only_their_bands(client, member, band, other_band):
    page = client.get("/admin/concerts/band/").content.decode()
    assert "Damantra" in page and "Splenius" not in page
    assert client.get(f"/admin/concerts/band/{band.pk}/change/").status_code == 200
    # Another band's page redirects to the admin home with "doesn't exist", as for a band that never existed
    assert client.get(f"/admin/concerts/band/{other_band.pk}/change/").url == "/admin/"


@pytest.mark.parametrize("page", ["share", "style", "style/preview"])
def test_share_and_style_only_for_their_bands(client, member, band, other_band, page):
    assert client.get(f"/admin/concerts/band/{band.pk}/{page}/").status_code == 200
    assert client.get(f"/admin/concerts/band/{other_band.pk}/{page}/").status_code == 404


def test_member_sees_only_their_concerts(client, member, band, other_band, venue):
    Concert.objects.create(band=band, venue=venue, starts_at="2030-05-01T20:00Z", support_acts="Ours")
    theirs = Concert.objects.create(band=other_band, venue=venue, starts_at="2030-05-02T20:00Z", support_acts="X")
    page = client.get("/admin/concerts/concert/").content.decode()
    assert "Damantra" in page and "Splenius" not in page
    assert client.get(f"/admin/concerts/concert/{theirs.pk}/change/").url == "/admin/"
    assert client.post(f"/admin/concerts/concert/{theirs.pk}/delete/", {"post": "yes"}).url == "/admin/"
    assert Concert.objects.filter(pk=theirs.pk).exists()


def test_member_adds_concerts_only_for_their_bands(client, member, band, other_band, venue):
    assert add_concert(client, band, venue).status_code == 302
    refused = add_concert(client, other_band, venue)
    assert refused.status_code == 200 and "Select a valid choice" in refused.content.decode()
    assert list(Concert.objects.values_list("band__slug", flat=True)) == ["damantra"]


def test_band_choices_list_only_their_bands(client, member, band, other_band):
    found = client.get(
        "/admin/autocomplete/", {"app_label": "concerts", "model_name": "concert", "field_name": "band"}
    ).json()
    assert [result["text"] for result in found["results"]] == ["Damantra"]


def test_member_cannot_create_or_delete_bands_or_change_the_address(client, member, band):
    assert client.get("/admin/concerts/band/add/").status_code == 403
    assert client.get(f"/admin/concerts/band/{band.pk}/delete/").status_code == 403
    page = client.get(f"/admin/concerts/band/{band.pk}/change/").content.decode()
    assert 'name="slug"' not in page  # shown, not editable: subscribed feeds depend on it
    assert "membership_set" not in page  # no managing members


def test_member_adds_venues_but_cannot_change_them(client, member, venue):
    assert client.get("/admin/concerts/venue/add/").status_code == 200
    assert client.get("/admin/concerts/venue/search/").status_code == 200
    assert client.get(f"/admin/concerts/venue/{venue.pk}/change/").status_code == 200  # read only
    assert client.post(f"/admin/concerts/venue/{venue.pk}/change/", {"name": "X", "city": "Y"}).status_code == 403
    assert client.get(f"/admin/concerts/venue/{venue.pk}/delete/").status_code == 403


def test_member_cannot_manage_accounts(client, member):
    assert client.get("/admin/concerts/account/").status_code == 403


def test_superuser_sees_every_band(client, band, other_band):
    client.force_login(Account.objects.create_superuser("admin@example.org"))
    page = client.get("/admin/concerts/band/").content.decode()
    assert "Damantra" in page and "Splenius" in page
    assert client.get(f"/admin/concerts/band/{other_band.pk}/share/").status_code == 200
    assert 'name="slug"' in client.get(f"/admin/concerts/band/{band.pk}/change/").content.decode()
