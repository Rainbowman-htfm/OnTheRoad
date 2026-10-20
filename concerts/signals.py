"""Regenerate public pages after each change made in the admin."""
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import public
from .models import Band, BandLink, Concert, Venue


def rebuild(band_ids):
    # After the transaction commits, so a rolled-back save never reaches the public page
    def run():
        for band in Band.objects.filter(pk__in=band_ids):
            public.build_band(band)
    transaction.on_commit(run)


@receiver(post_save, sender=Band)
def band_saved(sender, instance, **kwargs):
    rebuild([instance.pk])


@receiver(post_delete, sender=Band)
def band_deleted(sender, instance, **kwargs):
    transaction.on_commit(lambda: public.remove_band(instance.slug))


@receiver([post_save, post_delete], sender=Concert)
@receiver([post_save, post_delete], sender=BandLink)
def band_part_changed(sender, instance, **kwargs):
    rebuild([instance.band_id])


@receiver(post_save, sender=Venue)
def venue_saved(sender, instance, **kwargs):
    rebuild(instance.concerts.values_list("band_id", flat=True).distinct())
