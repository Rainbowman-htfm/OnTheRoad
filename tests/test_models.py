from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from concerts.models import Account, Band, BandLink, Concert, Membership, Venue

pytestmark = pytest.mark.django_db


@pytest.fixture
def concert():
    band = Band.objects.create(name="Damantra", slug="damantra")
    venue = Venue.objects.create(name="Le Plan", city="Ris-Orangis")
    starts_at = datetime(2026, 11, 14, 20, 30, tzinfo=ZoneInfo("Europe/Paris"))
    return Concert.objects.create(band=band, venue=venue, starts_at=starts_at)


def test_account_identified_by_email():
    account = Account.objects.create_user("Someone@Example.org", "secret")
    assert account.email == "Someone@example.org"  # domain normalized to lowercase
    assert account.check_password("secret")
    with pytest.raises(IntegrityError):
        Account.objects.create_user("Someone@example.org")


def test_account_without_password_cannot_log_in_with_one():
    assert not Account.objects.create_user("fan@example.org").has_usable_password()


def test_membership_unique_per_account_and_band(concert):
    account = Account.objects.create_user("a@example.org")
    Membership.objects.create(account=account, band=concert.band)
    assert list(account.bands.all()) == [concert.band]
    with pytest.raises(IntegrityError):
        Membership.objects.create(account=account, band=concert.band)


def test_band_links_unique_and_deleted_with_band(concert):
    band = concert.band
    band.links.create(url="https://damantra.bandcamp.com")
    assert [link.url for link in band.links.all()] == ["https://damantra.bandcamp.com"]
    with pytest.raises(IntegrityError), transaction.atomic():
        band.links.create(url="https://damantra.bandcamp.com")
    band.delete()
    assert not BandLink.objects.exists()


def test_sqlite_foreign_keys_enforced(concert):
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA foreign_keys")
        assert cursor.fetchone() == (1,)
    # A concert pointing to a missing venue is rejected by the database itself.
    # Django declares foreign keys as "deferred": checked when the transaction commits,
    # which tests never reach, hence the forced check.
    with pytest.raises(IntegrityError), transaction.atomic():
        Concert.objects.filter(pk=concert.pk).update(venue_id=9999)
        connection.check_constraints()


def test_end_must_be_after_start(concert):
    concert.ends_at = concert.starts_at
    with pytest.raises(IntegrityError), transaction.atomic():
        concert.save()


def test_venue_protected_and_band_cascades(concert):
    with pytest.raises(ProtectedError):
        concert.venue.delete()
    concert.band.delete()
    assert not Concert.objects.exists()


def test_starts_at_keeps_its_time_zone(concert):
    concert.refresh_from_db()
    assert concert.starts_at == datetime(2026, 11, 14, 19, 30, tzinfo=ZoneInfo("UTC"))
    assert concert.status == Concert.Status.PLANNED


def test_moving_a_concert_to_another_day_keeps_its_first_date(concert):
    first = concert.starts_at
    concert.starts_at = first.replace(hour=21)  # the time only: not a reschedule
    concert.save()
    assert concert.previous_starts_at is None
    concert.starts_at = first.replace(day=20)
    concert.save()
    concert.starts_at = first.replace(day=27)  # moved again: fans still remember the first date
    concert.save()
    assert concert.previous_starts_at == first.replace(hour=21)  # its date and time just before the first move
    concert.starts_at = first  # back to its first day: not rescheduled after all
    concert.save()
    assert concert.previous_starts_at is None


def test_previous_date_cleared_by_hand_stays_cleared(concert):
    concert.starts_at = concert.starts_at.replace(day=20)  # a typo...
    concert.save()
    concert.previous_starts_at = None  # ...so the "previous date" is emptied in the admin
    concert.save()
    concert.refresh_from_db()
    assert concert.previous_starts_at is None
