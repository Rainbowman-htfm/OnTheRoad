"""Regenerate public pages after each change made in the admin."""

import threading
from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver

from . import public
from .models import Band, BandLink, Concert, Event, Membership, OtherBand, Venue

# The bands waiting to be rebuilt: one save in the admin sends many signals (the concert, each of its other
# bands…), each page is still built once. One set per thread, so two saves at the same time do not mix
pending = threading.local()


def rebuild(band_ids: Iterable[int]) -> None:
    # Read now: once committed, a deleted concert no longer tells its band
    pending.ids = getattr(pending, "ids", set()) | set(band_ids)

    # After the transaction commits, so a rolled-back save never reaches the public page. The first of these
    # jobs builds every band waiting, the next ones find nothing left. A rolled-back save leaves its bands
    # waiting: they are built with the next one, from the database, so never wrong. That is why a job is
    # scheduled on every call, not only when the set was empty: after a rollback it would stay full forever
    def run() -> None:
        ids, pending.ids = pending.ids, set()
        if not ids:
            return
        for band in Band.objects.filter(pk__in=ids):
            public.build_band(band)
        public.build_home()  # its "just announced" list shows concerts of every band

    transaction.on_commit(run)


def bands_playing_with(band: Band) -> list[int]:
    """The bands whose concerts list this one among the other bands of the night."""
    return list(Concert.objects.filter(other_bands__band=band).values_list("band_id", flat=True).distinct())


@receiver(post_save, sender=Band)
def band_saved(sender: type[Band], instance: Band, **kwargs: Any) -> None:
    # Its name and link also appear on the pages of the bands it played with
    rebuild([instance.pk, *bands_playing_with(instance)])


@receiver(pre_delete, sender=Band)
def band_deleting(sender: type[Band], instance: Band, **kwargs: Any) -> None:
    # It still played those nights: the other bands' concerts keep its name, without a link
    others = bands_playing_with(instance)
    OtherBand.objects.filter(band=instance).update(band=None, name=instance.name)
    rebuild(others)


@receiver(post_delete, sender=Band)
def band_deleted(sender: type[Band], instance: Band, **kwargs: Any) -> None:
    transaction.on_commit(lambda: public.remove_band(instance.slug))
    transaction.on_commit(public.build_home)


@receiver([post_save, post_delete], sender=Concert)
@receiver([post_save, post_delete], sender=BandLink)
# A first member claims the page: the "maintained by On the Road" notice goes
@receiver([post_save, post_delete], sender=Membership)
def band_part_changed(
    sender: type[Concert | BandLink | Membership], instance: Concert | BandLink | Membership, **kwargs: Any
) -> None:
    rebuild([instance.band_id])


@receiver([post_save, post_delete], sender=OtherBand)
def other_band_changed(sender: type[OtherBand], instance: OtherBand, **kwargs: Any) -> None:
    # Through the concert, which may be gone already when a band and its concerts are deleted
    rebuild(Concert.objects.filter(pk=instance.concert_id).values_list("band_id", flat=True))


@receiver(post_save, sender=Venue)
@receiver(post_save, sender=Event)
def venue_saved(sender: type[Venue | Event], instance: Venue | Event, **kwargs: Any) -> None:
    rebuild(instance.concerts.values_list("band_id", flat=True).distinct())
