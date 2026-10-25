"""Regenerate public pages after each change made in the admin."""

from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import public
from .models import Band, BandLink, Concert, Festival, Membership, Venue


def rebuild(band_ids: Iterable[int]) -> None:
    # After the transaction commits, so a rolled-back save never reaches the public page
    def run() -> None:
        for band in Band.objects.filter(pk__in=band_ids):
            public.build_band(band)
        public.build_home()  # its "just announced" list shows concerts of every band

    transaction.on_commit(run)


@receiver(post_save, sender=Band)
def band_saved(sender: type[Band], instance: Band, **kwargs: Any) -> None:
    rebuild([instance.pk])


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


@receiver(post_save, sender=Venue)
@receiver(post_save, sender=Festival)
def venue_saved(sender: type[Venue | Festival], instance: Venue | Festival, **kwargs: Any) -> None:
    rebuild(instance.concerts.values_list("band_id", flat=True).distinct())
