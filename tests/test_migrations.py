"""Migrations that convert existing data."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_free_entry_box_becomes_the_free_entry_choice():
    executor = MigrationExecutor(connection)
    executor.migrate([("concerts", "0012_concert_source")])
    old = executor.loader.project_state([("concerts", "0012_concert_source")]).apps
    band = old.get_model("concerts", "Band").objects.create(name="Damantra", slug="damantra")
    venue = old.get_model("concerts", "Venue").objects.create(name="Le Plan", city="Ris-Orangis")
    Concert = old.get_model("concerts", "Concert")
    for free in (True, False):
        Concert.objects.create(band=band, venue=venue, starts_at="2030-05-01T20:00Z", free_entry=free)

    executor = MigrationExecutor(connection)  # reloaded: the graph changed
    executor.migrate([("concerts", "0013_concert_entry")])
    new = executor.loader.project_state([("concerts", "0013_concert_entry")]).apps
    entries = new.get_model("concerts", "Concert").objects.order_by("pk").values_list("entry", flat=True)
    assert list(entries) == ["free", "tickets"]


@pytest.mark.django_db(transaction=True)
def test_other_bands_text_becomes_one_row_as_written():
    executor = MigrationExecutor(connection)
    executor.migrate([("concerts", "0014_festival")])
    old = executor.loader.project_state([("concerts", "0014_festival")]).apps
    band = old.get_model("concerts", "Band").objects.create(name="Damantra", slug="damantra")
    venue = old.get_model("concerts", "Venue").objects.create(name="Le Plan", city="Ris-Orangis")
    Concert = old.get_model("concerts", "Concert")
    for text in ("Sangham and Novelist", ""):
        Concert.objects.create(band=band, venue=venue, starts_at="2030-05-01T20:00Z", support_acts=text)

    executor = MigrationExecutor(connection)  # reloaded: the graph changed
    executor.migrate([("concerts", "0015_other_bands")])
    new = executor.loader.project_state([("concerts", "0015_other_bands")]).apps
    names = new.get_model("concerts", "OtherBand").objects.values_list("name", flat=True)
    assert list(names) == ["Sangham and Novelist"]


@pytest.mark.django_db(transaction=True)
def test_festivals_become_events():
    # Band members' rights after the rename: test_events.py, on a database built with every migration
    executor = MigrationExecutor(connection)
    executor.migrate([("concerts", "0015_other_bands")])
    old = executor.loader.project_state([("concerts", "0015_other_bands")]).apps
    old.get_model("concerts", "Festival").objects.create(name="Metal Fest 09")

    executor = MigrationExecutor(connection)  # reloaded: the graph changed
    executor.migrate([("concerts", "0016_festival_to_event")])
    new = executor.loader.project_state([("concerts", "0016_festival_to_event")]).apps
    assert list(new.get_model("concerts", "Event").objects.values_list("name", flat=True)) == ["Metal Fest 09"]
