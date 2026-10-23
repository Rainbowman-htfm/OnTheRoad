"""Static public files for each band, written to settings.PUBLIC_DIR/<slug>/:
page, widget, iCal and RSS feeds, and the MusicEvent data as JSON."""

import json
import re
import shutil
import zlib
from datetime import datetime, time, timedelta
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.db.models import QuerySet
from django.template.loader import render_to_string
from django.utils import dateformat, timezone
from django.utils.feedgenerator import Rss201rev2Feed
from django.utils.safestring import SafeString, mark_safe
from icalendar import Calendar, Event

from .models import Band, Concert

EVENT_STATUS: dict[str, str] = {
    Concert.Status.PLANNED: "EventScheduled",
    Concert.Status.SOLD_OUT: "EventScheduled",
    Concert.Status.CANCELLED: "EventCancelled",
}
ICAL_STATUS: dict[str, str] = {
    Concert.Status.PLANNED: "CONFIRMED",
    Concert.Status.SOLD_OUT: "CONFIRMED",
    Concert.Status.CANCELLED: "CANCELLED",
}


# Style choices shared by the page and the widget (the widget's address settings use the same names)
FONTS = {
    "sans": "system-ui, sans-serif",
    "serif": "Georgia, 'Times New Roman', serif",
    "mono": "ui-monospace, Menlo, Consolas, monospace",
}
RADIUS = {"slight": "6px", "square": "0", "round": "14px"}
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def is_light(color: str) -> bool:
    """Whether dark text reads better than white text on this colour."""
    red, green, blue = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.299 * red + 0.587 * green + 0.114 * blue > 150


def band_style(band: Band) -> dict[str, str]:
    """The band's style as an HTML theme and CSS variables. Only known values reach the CSS: colours are
    checked again here, whatever saved them."""
    variables = {"--font": FONTS.get(band.font, FONTS["sans"]), "--radius": RADIUS.get(band.corners, RADIUS["slight"])}
    if HEX_COLOR.match(band.accent_color):
        variables["--accent"] = band.accent_color
        variables["--on-accent"] = "#111" if is_light(band.accent_color) else "#fff"
    if HEX_COLOR.match(band.text_color):
        variables["--fg"] = band.text_color
    if HEX_COLOR.match(band.background_color):
        variables["--bg"] = band.background_color
    return {
        "theme": "dark" if band.theme == Band.Theme.DARK else "light",
        "css": "; ".join(f"{name}: {value}" for name, value in variables.items()),
    }


def site_url() -> str:
    return settings.SITE_URL.rstrip("/")


def page_url(band: Band) -> str:
    return f"{site_url()}/{band.slug}/"


def osm_credit(concerts: list[Concert]) -> str:
    """OpenStreetMap's licence (ODbL) asks for a credit wherever its data is shown: "all" when every venue on
    the page was imported from it, "some" when only some were, "" when none."""
    from_osm = [bool(concert.venue.osm_id) for concert in concerts]
    return "all" if from_osm and all(from_osm) else "some" if any(from_osm) else ""


def calendar_url(band: Band) -> str:
    # webcal:// opens the phone's calendar app to subscribe, rather than downloading the file once
    return "webcal://" + page_url(band).split("://", 1)[1] + "concerts.ics"


def monogram(band: Band) -> dict[str, Any]:
    """Initials of the first and last words ("Faith In Agony" → "FA"), with a colour that stays the same
    for a band across builds: crc32 rather than hash(), which changes at every Python start."""
    words = band.name.split() or ["?"]
    initials = words[0][0] + (words[-1][0] if len(words) > 1 else "")
    return {"initials": initials.upper(), "hue": zlib.crc32(band.slug.encode()) % 360}


def concert_uid(concert: Concert) -> str:
    """Stable identifier, so calendars and feed readers update a concert instead of duplicating it."""
    return f"concert-{concert.pk}@{urlparse(settings.SITE_URL).hostname}"


# How long a newly announced concert is shown as new; the nightly build removes the badge afterwards
NEW_DAYS = 14


def start_of_today() -> datetime:
    # Concerts from the start of today, so tonight's concert stays on the page all day
    return datetime.combine(timezone.localdate(), time.min, timezone.get_current_timezone())


def new_since() -> datetime:
    return timezone.now() - timedelta(days=NEW_DAYS)


def upcoming_concerts(band: Band) -> QuerySet[Concert]:
    return band.concerts.filter(starts_at__gte=start_of_today()).select_related("venue")


def just_announced() -> QuerySet[Concert]:
    """Upcoming concerts of every band announced in the last NEW_DAYS days, latest first."""
    return (
        Concert.objects.filter(announced_at__gte=new_since(), starts_at__gte=start_of_today())
        .exclude(status=Concert.Status.CANCELLED)
        .select_related("band", "venue")
        .order_by("-announced_at")
    )


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
    if venue.website:
        location["url"] = venue.website
    performer: dict[str, Any] = {"@type": "MusicGroup", "name": band.name}
    cancelled = concert.status == Concert.Status.CANCELLED
    if band.website:
        performer["url"] = band.website
    if same_as:
        performer["sameAs"] = same_as
    event: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "MusicEvent",
        "name": f"{band.name} at {venue.name}",
        # A date alone when the time is not announced, which schema.org accepts
        "startDate": (
            timezone.localtime(concert.starts_at).date().isoformat()
            if concert.time_tba
            else timezone.localtime(concert.starts_at).isoformat()
        ),
        **(
            {"endDate": timezone.localtime(concert.ends_at).isoformat()}
            if concert.ends_at and not concert.time_tba
            else {}
        ),
        "eventStatus": "https://schema.org/"
        + ("EventRescheduled" if concert.previous_starts_at and not cancelled else EVENT_STATUS[concert.status]),
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": location,
        "performer": performer,
        **({"isAccessibleForFree": True} if concert.free_entry else {}),
    }
    if concert.previous_starts_at:
        event["previousStartDate"] = timezone.localdate(concert.previous_starts_at).isoformat()
    if concert.ticket_url:
        sold_out = concert.status == Concert.Status.SOLD_OUT
        event["offers"] = {
            "@type": "Offer",
            "url": concert.ticket_url,
            "availability": "https://schema.org/" + ("SoldOut" if sold_out else "InStock"),
        }
    return event


def json_for_script(data: Any) -> SafeString:
    # "<" escaped so a name containing "</script>" cannot end the block early
    return mark_safe(json.dumps(data, ensure_ascii=False).replace("<", "\\u003c"))


def band_page(band: Band, concerts: list[Concert], band_links: list[str]) -> str:
    links = ([band.website] if band.website else []) + band_links
    return render_to_string(
        "concerts/band_page.html",
        {
            "band": band,
            "concerts": concerts,
            "events": [json_for_script(music_event(concert, band_links)) for concert in concerts],
            "links": [(url, link_label(url)) for url in links],
            "calendar_url": calendar_url(band),
            "style": band_style(band),
            "new_since": new_since(),
            "osm_credit": osm_credit(concerts),
        },
    )


def embed_code(band: Band, template: str = "concerts/embed_code.html") -> str:
    """Code a band pastes on its website: the widget, its height listener and the MusicEvent data.
    The share page replaces __QUERY__ with the widget settings, __ALLOW__ with the "Near me" permission
    and, in the iframe-only version (concerts/embed_iframe.html), __HEIGHT__ with a fixed height."""
    site = urlparse(settings.SITE_URL)
    return render_to_string(
        template,
        {
            "band": band,
            "frame_id": f"otr-{band.slug}",
            "widget_url": f"{page_url(band)}widget/",
            "events_url": f"{page_url(band)}events.json",
            # An origin has no path, even when the site is published under one
            "origin": f"{site.scheme}://{site.netloc}",
        },
    )


def widget_page(band: Band, concerts: list[Concert]) -> str:
    return render_to_string(
        "concerts/widget.html",
        {
            "band": band,
            "concerts": concerts,
            "page_url": page_url(band),
            "style": band_style(band),
            "choices": {"font": FONTS, "corners": RADIUS},
            "new_since": new_since(),
            "osm_credit": osm_credit(concerts),
        },
    )


def describe(concert: Concert) -> str:
    """Extra information for feeds: status if not planned, previous date, free entry and support acts."""
    parts = [] if concert.status == Concert.Status.PLANNED else [Concert.Status(concert.status).label.capitalize()]
    if concert.previous_starts_at:
        was = dateformat.format(timezone.localtime(concert.previous_starts_at), "M j")
        parts.append(f"Rescheduled (was {was})")
    if concert.free_entry:
        parts.append("Free entry")
    if concert.support_acts:
        parts.append(f"with {concert.support_acts}")
    return " · ".join(parts)


def calendar(band: Band) -> bytes:
    """iCal feed. Past concerts stay in it: a subscribed calendar drops whatever leaves the feed."""
    # ponytail: every past concert, so the file grows ~400 bytes per concert and calendars re-download it
    # every few hours; keep only the last 12 months or so once bands have hundreds of concerts
    cal = Calendar()
    cal.add("prodid", "-//OTR//concert dates//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", f"{band.name} concerts")
    now = timezone.now()
    for concert in band.concerts.select_related("venue"):
        venue = concert.venue
        event = Event()
        event.add("uid", concert_uid(concert))
        event.add("dtstamp", now)
        if concert.time_tba:
            # All-day event: calendars show the day without inventing a time
            day = timezone.localtime(concert.starts_at).date()
            event.add("dtstart", day)
            event.add("dtend", day + timedelta(days=1))
        else:
            event.add("dtstart", concert.starts_at)
            if concert.ends_at:
                event.add("dtend", concert.ends_at)
        event.add("summary", f"{band.name} at {venue.name}")
        city = f"{venue.postal_code} {venue.city}".strip()
        event.add("location", ", ".join(part for part in (venue.name, venue.address, city) if part))
        event.add("status", ICAL_STATUS[concert.status])
        if concert.ticket_url:
            event.add("url", concert.ticket_url)
        if description := describe(concert):
            event.add("description", description)
        cal.add_component(event)
    return cal.to_ical()


def rss(concerts: list[Concert], band: Band | None = None) -> str:
    """RSS feed of upcoming concerts, latest announcements first: one band's, or the whole site's
    recent announcements (band=None), whose items start with the band's name."""
    if band:
        feed = Rss201rev2Feed(
            title=f"{band.name} – upcoming concerts",
            link=page_url(band),
            description=f"Upcoming concerts of {band.name}",
            language="en",
        )
    else:
        feed = Rss201rev2Feed(
            title="Just announced",
            link=site_url() + "/",
            description="Concerts just announced by the bands on this site",
            language="en",
        )
    for concert in sorted(concerts, key=lambda concert: concert.announced_at, reverse=True):
        date = dateformat.format(timezone.localtime(concert.starts_at), "D j M Y")
        details = describe(concert)
        title = f"{date} – {concert.venue.name}, {concert.venue.city}" + (f" ({details})" if details else "")
        feed.add_item(
            title=title if band else f"{concert.band.name}: {title}",
            link=concert.ticket_url or page_url(concert.band),
            description=details,
            unique_id=concert_uid(concert),
            unique_id_is_permalink=False,
            pubdate=concert.announced_at,
        )
    return feed.writeString("utf-8")


def build_band(band: Band) -> None:
    concerts = list(upcoming_concerts(band))
    # The band's links are loaded once, not once per concert
    band_links = [link.url for link in band.links.all()]
    events = [music_event(concert, band_links) for concert in concerts]
    folder = settings.PUBLIC_DIR / band.slug
    (folder / "widget").mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text(band_page(band, concerts, band_links), encoding="utf-8")
    (folder / "widget" / "index.html").write_text(widget_page(band, concerts), encoding="utf-8")
    # Same MusicEvent data as the page, for the band's own website (embed code)
    (folder / "events.json").write_text(json_for_script(events), encoding="utf-8")
    (folder / "concerts.ics").write_bytes(calendar(band))
    (folder / "concerts.xml").write_text(rss(concerts, band), encoding="utf-8")


def remove_band(slug: str) -> None:
    shutil.rmtree(settings.PUBLIC_DIR / slug, ignore_errors=True)


def home_bands() -> list[dict[str, Any]]:
    """Every band with its next concert, soonest first; bands without one last, by name."""
    upcoming = (
        Concert.objects.filter(starts_at__gte=start_of_today())
        .exclude(status=Concert.Status.CANCELLED)
        .select_related("venue")
        .order_by("starts_at")
    )
    next_concert: dict[int, Concert] = {}
    for concert in upcoming:
        next_concert.setdefault(concert.band_id, concert)
    bands = [
        {
            "band": band,
            "next": next_concert.get(band.pk),
            "monogram": monogram(band),
            "calendar_url": calendar_url(band),
        }
        for band in Band.objects.all()
    ]
    # Sorting is stable: bands keep their alphabetical order within each group
    return sorted(bands, key=lambda item: (item["next"] is None, item["next"].starts_at if item["next"] else 0))


def build_home() -> None:
    """Home page (just announced concerts, then the bands), the site's "just announced" feed and the
    "Your data" page, at the root of PUBLIC_DIR."""
    settings.PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    announced = list(just_announced())
    bands = home_bands()
    shown = announced + [item["next"] for item in bands if item["next"]]
    html = render_to_string(
        "concerts/home.html",
        {"bands": bands, "announced": announced, "osm_credit": osm_credit(shown), "contact": settings.CONTACT_EMAIL},
    )
    (settings.PUBLIC_DIR / "index.html").write_text(html, encoding="utf-8")
    (settings.PUBLIC_DIR / "just-announced.xml").write_text(rss(announced), encoding="utf-8")
    # A file, not a folder: no clash with a band called "data", and the nightly cleanup of folders leaves it
    data_page = render_to_string("concerts/data.html", {"contact": settings.CONTACT_EMAIL})
    (settings.PUBLIC_DIR / "data.html").write_text(data_page, encoding="utf-8")


def build_all() -> None:
    """Rebuild every band's files and the home page; also drops past dates and folders of renamed bands."""
    build_home()
    slugs: set[str] = set()
    for band in Band.objects.all():
        build_band(band)
        slugs.add(band.slug)
    for folder in settings.PUBLIC_DIR.iterdir():
        if folder.is_dir() and folder.name not in slugs:
            shutil.rmtree(folder)
