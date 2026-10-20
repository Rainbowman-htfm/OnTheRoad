"""Static public pages: one HTML file per band, written to settings.PUBLIC_DIR."""

import json
import shutil
from datetime import datetime, time
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.db.models import QuerySet
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.safestring import mark_safe

from .models import Band, Concert

EVENT_STATUS: dict[str, str] = {
    Concert.Status.PLANNED: "EventScheduled",
    Concert.Status.SOLD_OUT: "EventScheduled",
    Concert.Status.CANCELLED: "EventCancelled",
    Concert.Status.POSTPONED: "EventPostponed",
}


def upcoming_concerts(band: Band) -> QuerySet[Concert]:
    # From the start of today, so tonight's concert stays on the page all day
    today = datetime.combine(timezone.localdate(), time.min, timezone.get_current_timezone())
    return band.concerts.filter(starts_at__gte=today).select_related("venue")


def link_label(url: str) -> str:
    return (urlparse(url).hostname or url).removeprefix("www.")


def music_event(concert: Concert, same_as: list[str]) -> dict[str, Any]:
    """schema.org MusicEvent, read by search engines and aggregators; same_as: the band's other links."""
    band, venue = concert.band, concert.venue
    address: dict[str, str] = {"@type": "PostalAddress", "addressLocality": venue.city}
    if venue.address:
        address["streetAddress"] = venue.address
    if venue.postal_code:
        address["postalCode"] = venue.postal_code
    location: dict[str, Any] = {
        "@type": "MusicVenue",
        "name": venue.name,
        "address": address,
    }
    if venue.latitude is not None and venue.longitude is not None:
        location["geo"] = {
            "@type": "GeoCoordinates",
            "latitude": float(venue.latitude),
            "longitude": float(venue.longitude),
        }
    performer: dict[str, Any] = {"@type": "MusicGroup", "name": band.name}
    if band.website:
        performer["url"] = band.website
    if same_as:
        performer["sameAs"] = same_as
    event: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "MusicEvent",
        "name": f"{band.name} at {venue.name}",
        "startDate": timezone.localtime(concert.starts_at).isoformat(),
        "eventStatus": "https://schema.org/" + EVENT_STATUS[concert.status],
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": location,
        "performer": performer,
    }
    if concert.ticket_url:
        sold_out = concert.status == Concert.Status.SOLD_OUT
        event["offers"] = {
            "@type": "Offer",
            "url": concert.ticket_url,
            "availability": "https://schema.org/" + ("SoldOut" if sold_out else "InStock"),
        }
    return event


def band_page(band: Band) -> str:
    concerts = list(upcoming_concerts(band))
    # Loaded once for the whole page, not once per concert
    band_links = [link.url for link in band.links.all()]
    # "<" escaped so a name containing "</script>" cannot end the block early
    events = [
        mark_safe(json.dumps(music_event(concert, band_links), ensure_ascii=False).replace("<", "\\u003c"))
        for concert in concerts
    ]
    links = ([band.website] if band.website else []) + band_links
    return render_to_string(
        "concerts/band_page.html",
        {
            "band": band,
            "concerts": concerts,
            "events": events,
            "links": [(url, link_label(url)) for url in links],
        },
    )


def build_band(band: Band) -> None:
    path = settings.PUBLIC_DIR / band.slug / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(band_page(band), encoding="utf-8")


def remove_band(slug: str) -> None:
    shutil.rmtree(settings.PUBLIC_DIR / slug, ignore_errors=True)


def build_all() -> None:
    """Rebuild every page; also drops past dates and pages of renamed bands."""
    settings.PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    slugs: set[str] = set()
    for band in Band.objects.all():
        build_band(band)
        slugs.add(band.slug)
    for folder in settings.PUBLIC_DIR.iterdir():
        if folder.is_dir() and folder.name not in slugs:
            shutil.rmtree(folder)
