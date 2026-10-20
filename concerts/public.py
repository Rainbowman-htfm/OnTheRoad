"""Static public pages: one HTML file per band, written to settings.PUBLIC_DIR."""
import json
import shutil
from datetime import datetime, time
from urllib.parse import urlparse

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.safestring import mark_safe

from .models import Band, Concert

EVENT_STATUS = {
    Concert.Status.PLANNED: "EventScheduled",
    Concert.Status.SOLD_OUT: "EventScheduled",
    Concert.Status.CANCELLED: "EventCancelled",
    Concert.Status.POSTPONED: "EventPostponed",
}


def upcoming_concerts(band):
    # From the start of today, so tonight's concert stays on the page all day
    today = datetime.combine(timezone.localdate(), time.min, timezone.get_current_timezone())
    return band.concerts.filter(starts_at__gte=today).select_related("venue")


def link_label(url):
    return urlparse(url).hostname.removeprefix("www.")


def music_event(concert):
    """schema.org MusicEvent, read by search engines and aggregators."""
    band, venue = concert.band, concert.venue
    address = {"@type": "PostalAddress", "addressLocality": venue.city}
    if venue.address:
        address["streetAddress"] = venue.address
    if venue.postal_code:
        address["postalCode"] = venue.postal_code
    location = {"@type": "MusicVenue", "name": venue.name, "address": address}
    if venue.latitude is not None and venue.longitude is not None:
        location["geo"] = {"@type": "GeoCoordinates", "latitude": float(venue.latitude), "longitude": float(venue.longitude)}
    performer = {"@type": "MusicGroup", "name": band.name}
    if band.website:
        performer["url"] = band.website
    same_as = [link.url for link in band.links.all()]
    if same_as:
        performer["sameAs"] = same_as
    event = {
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


def band_page(band):
    concerts = list(upcoming_concerts(band))
    for concert in concerts:
        # "<" escaped so a name containing "</script>" cannot end the block early
        concert.json_ld = mark_safe(json.dumps(music_event(concert), ensure_ascii=False).replace("<", "\\u003c"))
    links = ([band.website] if band.website else []) + [link.url for link in band.links.all()]
    return render_to_string("concerts/band_page.html", {
        "band": band,
        "concerts": concerts,
        "links": [(url, link_label(url)) for url in links],
    })


def build_band(band):
    path = settings.PUBLIC_DIR / band.slug / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(band_page(band), encoding="utf-8")


def remove_band(slug):
    shutil.rmtree(settings.PUBLIC_DIR / slug, ignore_errors=True)


def build_all():
    """Rebuild every page; also drops past dates and pages of renamed bands."""
    settings.PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    slugs = set()
    for band in Band.objects.all():
        build_band(band)
        slugs.add(band.slug)
    for folder in settings.PUBLIC_DIR.iterdir():
        if folder.is_dir() and folder.name not in slugs:
            shutil.rmtree(folder)
