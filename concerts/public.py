"""Static public files for each band, written to settings.PUBLIC_DIR/<slug>/:
page, widget, iCal and RSS feeds, and the MusicEvent data as JSON; and each genre's, in genre/<slug>/."""

import json
import re
import shutil
import zlib
from datetime import datetime, time, timedelta
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from django.conf import settings
from django.db.models import QuerySet
from django.template.loader import render_to_string
from django.utils import dateformat, timezone
from django.utils.feedgenerator import Rss201rev2Feed
from django.utils.safestring import SafeString, mark_safe
from icalendar import Calendar, Event

from .models import RESERVED_SLUGS, Band, Concert, Genre, Membership

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


def genre_url(genre: Genre) -> str:
    return f"{site_url()}/genre/{genre.slug}/"


def osm_credit(concerts: list[Concert]) -> str:
    """OpenStreetMap's licence (ODbL) asks for a credit wherever its data is shown: "all" when every venue on
    the page was imported from it, "some" when only some were, "" when none."""
    from_osm = [bool(concert.venue.osm_id) for concert in concerts]
    return "all" if from_osm and all(from_osm) else "some" if any(from_osm) else ""


def calendar_links(page: str, name: str) -> dict[str, str]:
    """Ways to subscribe to a band's or a genre's calendar, next to its page: the site cannot know which
    calendar a fan uses."""
    feed = page + "concerts.ics"
    # webcal:// opens the calendar app to subscribe, rather than downloading the file once; Android has no app
    # for it, so Google and Outlook get their own subscription pages, with the feed filled in
    webcal = "webcal://" + feed.split("://", 1)[1]
    return {
        "apple": webcal,
        "google": "https://calendar.google.com/calendar/r?" + urlencode({"cid": webcal}),
        "outlook": "https://outlook.live.com/calendar/0/addfromweb?" + urlencode({"url": feed, "name": name}),
        "feed": feed,
        "name": name,  # for screen readers: which band's "Add to my calendar", on the home page
    }


def monogram(band: Band) -> dict[str, Any]:
    """Initials of the first and last words ("Faith In Agony" → "FA"), with a colour that stays the same
    for a band across builds: crc32 rather than hash(), which changes at every Python start."""
    words = band.name.split() or ["?"]
    initials = words[0][0] + (words[-1][0] if len(words) > 1 else "")
    return {"initials": initials.upper(), "hue": zlib.crc32(band.slug.encode()) % 360}


# A band with no member account has its dates entered by the site, from its public announcements
UNCLAIMED_NOTE = "Listed by On the Road from the band's announcements, not by the band."


def claimed_band_ids() -> set[int]:
    """Bands with at least one member account: they manage their own page."""
    return set(Membership.objects.values_list("band_id", flat=True))


def mailto(subject: str, body: str) -> str:
    """Link opening an email to the site's contact, already filled in; "" without a contact address."""
    if not settings.CONTACT_EMAIL:
        return ""
    # quote rather than the default "+" for spaces, which mail apps would show as is
    return f"mailto:{settings.CONTACT_EMAIL}?" + urlencode({"subject": subject, "body": body}, quote_via=quote)


def unclaimed_links(band: Band) -> dict[str, str]:
    """Email links of an unclaimed band's page: claim it, help with it, or have it removed."""
    page = page_url(band)
    return {
        "claim": mailto(
            f"Claim {band.name}",
            f"I'm in {band.name} and would like to manage its page: {page}\n\n"
            "My role in the band:\nA link showing it (the band's official account or website):\n",
        ),
        "help": mailto(
            f"Help with {band.name}", f"I'd like to help keep the dates of {band.name} up to date: {page}\n"
        ),
        "remove": mailto(f"Remove {band.name}", f"Please remove the page of {band.name}: {page}\n"),
    }


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


# The other bands of the night and their pages, in one more query for all the concerts rather than one per concert
OTHER_BANDS = "other_bands__band"


def upcoming_concerts(band: Band) -> QuerySet[Concert]:
    return (
        band.concerts.filter(starts_at__gte=start_of_today())
        .select_related("venue", "event")
        .prefetch_related(OTHER_BANDS)
    )


def past_concerts(band: Band) -> QuerySet[Concert]:
    """The band's past shows, latest first; a cancelled one never happened."""
    return (
        band.concerts.filter(starts_at__lt=start_of_today())
        .exclude(status=Concert.Status.CANCELLED)
        .select_related("venue", "event")
        .prefetch_related(OTHER_BANDS)
        .order_by("-starts_at")
    )


def just_announced() -> QuerySet[Concert]:
    """Upcoming concerts of every band announced in the last NEW_DAYS days, latest first."""
    return (
        Concert.objects.filter(announced_at__gte=new_since(), starts_at__gte=start_of_today())
        .exclude(status=Concert.Status.CANCELLED)
        .select_related("band", "venue", "event")
        .prefetch_related(OTHER_BANDS)
        .order_by("-announced_at")
    )


def announced_by_band(concerts: list[Concert]) -> list[dict[str, Any]]:
    """Just announced, one line per band so a whole tour stays one line; the band that announced last first,
    as concerts come from just_announced(). Like the Mastodon bot's posts."""
    bands: dict[int, list[Concert]] = {}
    for concert in concerts:
        bands.setdefault(concert.band_id, []).append(concert)
    lines = []
    for dates in bands.values():
        dates.sort(key=lambda concert: concert.starts_at)
        cities = list(dict.fromkeys(concert.venue.city for concert in dates))
        lines.append({"band": dates[0].band, "dates": dates, "cities": cities[:3], "more_cities": len(cities) > 3})
    return lines


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
    if venue.country:
        address["addressCountry"] = venue.country
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
        # The same as the calendars' UID: a tool reading this recognises a concert even after its date changes
        "identifier": concert_uid(concert),
        "name": f"{band.name} at {(concert.event or venue).name}",
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
        **({"isAccessibleForFree": True} if concert.entry == Concert.Entry.FREE else {}),
    }
    if concert.event:
        # A series: the event across years, not one edition (the concert has the date). A plain "Event" without a
        # date and place is checked by Google as an event of its own, and marked invalid
        event["superEvent"] = {"@type": "EventSeries", "name": concert.event.name}
        if concert.event.website:
            event["superEvent"]["url"] = concert.event.website
    if concert.previous_starts_at:
        event["previousStartDate"] = timezone.localdate(concert.previous_starts_at).isoformat()
    if concert.ticket_url and concert.entry != Concert.Entry.TICKETS:
        event["url"] = concert.ticket_url  # the event's page: nothing to buy
    elif concert.ticket_url:
        sold_out = concert.status == Concert.Status.SOLD_OUT
        event["offers"] = {
            "@type": "Offer",
            "url": concert.ticket_url,
            "availability": "https://schema.org/" + ("SoldOut" if sold_out else "InStock"),
        }
    return event


def link_other_bands(concerts: list[Concert]) -> None:
    """Each other band of the night, with its page when it is on this site, for the page and the widget."""
    for concert in concerts:
        concert.other_band_links = [
            (str(act), page_url(act.band) if act.band else "") for act in concert.other_bands.all()
        ]


def json_for_script(data: Any) -> SafeString:
    # "<" escaped so a name containing "</script>" cannot end the block early
    return mark_safe(json.dumps(data, ensure_ascii=False).replace("<", "\\u003c"))


def band_page(band: Band, concerts: list[Concert], band_links: list[str], claimed: bool) -> str:
    past = list(past_concerts(band))
    link_other_bands(concerts + past)
    links = ([band.website] if band.website else []) + band_links
    return render_to_string(
        "concerts/band_page.html",
        {
            "band": band,
            "concerts": concerts,
            "past": past,
            "events": [json_for_script(music_event(concert, band_links)) for concert in concerts],
            "links": [(url, link_label(url)) for url in links],
            "calendar": calendar_links(page_url(band), band.name),
            "genres": list(band.genres.all()),
            "style": band_style(band),
            "new_since": new_since(),
            "osm_credit": osm_credit(concerts + past),
            "claimed": claimed,
            "unclaimed_links": {} if claimed else unclaimed_links(band),
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
    link_other_bands(concerts)
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
    """Extra information for feeds: status if not planned, previous date, free or pay what you can, other bands."""
    parts = [] if concert.status == Concert.Status.PLANNED else [Concert.Status(concert.status).label.capitalize()]
    if concert.previous_starts_at:
        was = dateformat.format(timezone.localtime(concert.previous_starts_at), "M j")
        parts.append(f"Rescheduled (was {was})")
    if concert.entry != Concert.Entry.TICKETS:
        parts.append(Concert.Entry(concert.entry).label.capitalize())
    if names := concert.other_band_names():
        parts.append("with " + ", ".join(names))
    return " · ".join(parts)


def feed_text(concert: Concert, claimed: bool) -> str:
    """Text of a concert in the feeds: its details, and where the date comes from when the band does not
    manage its page, so followers can judge it."""
    return "\n".join(part for part in (describe(concert), "" if claimed else UNCLAIMED_NOTE) if part)


def calendar(name: str, concerts: QuerySet[Concert], claimed: set[int]) -> bytes:
    """iCal feed of a band's or a genre's concerts. Past concerts stay in it: a subscribed calendar drops
    whatever leaves the feed. claimed: the bands managing their page, as from claimed_band_ids()."""
    # ponytail: every past concert, so the file grows ~400 bytes per concert and calendars re-download it
    # every few hours; keep only the last 12 months or so once bands have hundreds of concerts
    cal = Calendar()
    cal.add("prodid", "-//OTR//concert dates//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", f"{name} concerts")
    now = timezone.now()
    for concert in concerts.select_related("band", "venue", "event").prefetch_related(OTHER_BANDS):
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
        event.add("summary", f"{concert.band.name} at {(concert.event or venue).name}")
        city = f"{venue.postal_code} {venue.city}".strip()
        event.add("location", ", ".join(part for part in (venue.name, venue.address, city) if part))
        event.add("status", ICAL_STATUS[concert.status])
        if concert.ticket_url:
            event.add("url", concert.ticket_url)
        if description := feed_text(concert, concert.band_id in claimed):
            event.add("description", description)
        cal.add_component(event)
    return cal.to_ical()


def rss(concerts: list[Concert], claimed: set[int], band: Band | None = None, genre: Genre | None = None) -> str:
    """RSS feed of upcoming concerts, latest announcements first: one band's, one genre's, or the whole
    site's recent announcements; but for a band's, items start with the band's name. claimed: claimed_band_ids()."""
    if band:
        feed = Rss201rev2Feed(
            title=f"{band.name} – upcoming concerts",
            link=page_url(band),
            description=f"Upcoming concerts of {band.name}",
            language="en",
        )
    elif genre:
        feed = Rss201rev2Feed(
            title=f"{genre.name} – upcoming concerts",
            link=genre_url(genre),
            description=f"Upcoming concerts of the {genre.name} bands on this site",
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
        title = f"{date} – {(concert.event or concert.venue).name}, {concert.venue.city}" + (
            f" ({details})" if details else ""
        )
        feed.add_item(
            title=title if band else f"{concert.band.name}: {title}",
            link=concert.ticket_url or page_url(concert.band),
            description=feed_text(concert, concert.band_id in claimed),
            unique_id=concert_uid(concert),
            unique_id_is_permalink=False,
            pubdate=concert.announced_at,
        )
    return feed.writeString("utf-8")


def site_events() -> list[dict[str, Any]]:
    """Every upcoming concert of every band, soonest first, for tools built over the site (a Mastodon bot, a
    town's agenda): the schema.org MusicEvent, as in each band's events.json, and what schema.org has no
    place for: the band's page and genres, whether it manages its page, when the date was announced, the
    other bands' pages."""
    concerts = (
        Concert.objects.filter(starts_at__gte=start_of_today())
        .select_related("band", "venue", "event")
        .prefetch_related(OTHER_BANDS, "band__links", "band__genres")
        .order_by("starts_at", "pk")
    )
    claimed = claimed_band_ids()
    return [
        {
            "band": {
                "name": concert.band.name,
                "page": page_url(concert.band),
                "claimed": concert.band_id in claimed,
                "genres": [genre.name for genre in concert.band.genres.all()],
            },
            "announced_at": timezone.localtime(concert.announced_at).isoformat(),
            "other_bands": [
                {"name": str(act), "page": page_url(act.band) if act.band else ""} for act in concert.other_bands.all()
            ],
            "event": music_event(concert, [link.url for link in concert.band.links.all()]),
        }
        for concert in concerts
    ]


def build_band(band: Band) -> None:
    concerts = list(upcoming_concerts(band))
    # The band's links are loaded once, not once per concert
    band_links = [link.url for link in band.links.all()]
    claimed = {band.pk} if band.members.exists() else set()  # as claimed_band_ids(), for calendar() and rss()
    events = [music_event(concert, band_links) for concert in concerts]
    folder = settings.PUBLIC_DIR / band.slug
    (folder / "widget").mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text(band_page(band, concerts, band_links, bool(claimed)), encoding="utf-8")
    (folder / "widget" / "index.html").write_text(widget_page(band, concerts), encoding="utf-8")
    # Same MusicEvent data as the page, for the band's own website (embed code)
    (folder / "events.json").write_text(json_for_script(events), encoding="utf-8")
    (folder / "concerts.ics").write_bytes(calendar(band.name, band.concerts.all(), claimed))
    (folder / "concerts.xml").write_text(rss(concerts, claimed, band), encoding="utf-8")


def remove_band(slug: str) -> None:
    shutil.rmtree(settings.PUBLIC_DIR / slug, ignore_errors=True)


def home_bands() -> list[dict[str, Any]]:
    """Every band with its next concert, soonest first; bands without one last, by name."""
    upcoming = (
        Concert.objects.filter(starts_at__gte=start_of_today())
        .exclude(status=Concert.Status.CANCELLED)
        .select_related("venue", "event")
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
            "calendar": calendar_links(page_url(band), band.name),
        }
        for band in Band.objects.all()
    ]
    # Sorting is stable: bands keep their alphabetical order within each group
    return sorted(bands, key=lambda item: (item["next"] is None, item["next"].starts_at if item["next"] else 0))


def build_genres() -> None:
    """Each genre's page, with the upcoming concerts of its bands, and its iCal and RSS feeds, in genre/<slug>/;
    the folders of deleted or renamed genres go."""
    claimed = claimed_band_ids()
    slugs: set[str] = set()
    # ponytail: every genre at every save, a few queries each; only the changed bands' genres if they grow many
    # A genre without bands has no page: its folder goes, like a deleted genre's
    for genre in Genre.objects.filter(bands__isnull=False).distinct().prefetch_related("bands"):
        slugs.add(genre.slug)
        concerts = Concert.objects.filter(band__genres=genre)
        upcoming = list(
            concerts.filter(starts_at__gte=start_of_today())
            .select_related("band", "venue", "event")
            .prefetch_related(OTHER_BANDS)
            .order_by("starts_at")
        )
        link_other_bands(upcoming)
        folder = settings.PUBLIC_DIR / "genre" / genre.slug
        folder.mkdir(parents=True, exist_ok=True)
        html = render_to_string(
            "concerts/genre.html",
            {
                "genre": genre,
                "bands": genre.bands.all(),
                "unclaimed": any(concert.band_id not in claimed for concert in upcoming),
                "concerts": upcoming,
                "calendar": calendar_links(genre_url(genre), genre.name),
                "new_since": new_since(),
                "osm_credit": osm_credit(upcoming),
            },
        )
        (folder / "index.html").write_text(html, encoding="utf-8")
        (folder / "concerts.ics").write_bytes(calendar(genre.name, concerts, claimed))
        (folder / "concerts.xml").write_text(rss(upcoming, claimed, genre=genre), encoding="utf-8")
    for folder in (settings.PUBLIC_DIR / "genre").glob("*/"):
        if folder.name not in slugs:
            shutil.rmtree(folder)


def build_home() -> None:
    """Home page (just announced concerts, then the bands), the site's "just announced" feed and events.json,
    the "Your data" page, the guide for bands, at the root of PUBLIC_DIR; and the genres' pages, as they list
    concerts of several bands."""
    settings.PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    announced = list(just_announced())
    bands = home_bands()
    shown = announced + [item["next"] for item in bands if item["next"]]
    html = render_to_string(
        "concerts/home.html",
        {
            "bands": bands,
            "genres": Genre.objects.filter(bands__isnull=False).distinct(),  # a genre without bands has no page
            "announced": announced_by_band(announced),
            "osm_credit": osm_credit(shown),
            "contact": settings.CONTACT_EMAIL,
            "source": settings.SOURCE_URL,
            "mastodon": settings.MASTODON_PROFILE,
            "bluesky": settings.BLUESKY_PROFILE,
            "suggest": mailto(
                "Suggest a band",
                "Band:\nCity:\nOfficial link (website, Instagram…):\n\n"
                "Would you like to help keep its dates up to date? yes / no\n",
            ),
        },
    )
    (settings.PUBLIC_DIR / "index.html").write_text(html, encoding="utf-8")
    (settings.PUBLIC_DIR / "just-announced.xml").write_text(rss(announced, claimed_band_ids()), encoding="utf-8")
    (settings.PUBLIC_DIR / "events.json").write_text(json_for_script(site_events()), encoding="utf-8")
    # A file, not a folder: no clash with a band called "data", and the nightly cleanup of folders leaves it
    data_page = render_to_string(
        "concerts/data.html", {"contact": settings.CONTACT_EMAIL, "source": settings.SOURCE_URL}
    )
    (settings.PUBLIC_DIR / "data.html").write_text(data_page, encoding="utf-8")
    guide = render_to_string(
        "concerts/guide.html",
        {
            "contact": settings.CONTACT_EMAIL,
            "minutes": settings.PASSWORD_RESET_TIMEOUT // 60,
            "new_days": NEW_DAYS,
        },
    )
    (settings.PUBLIC_DIR / "guide.html").write_text(guide, encoding="utf-8")
    build_genres()


def build_all() -> None:
    """Rebuild every band's files and the home page; also drops past dates and folders of renamed bands."""
    build_home()
    slugs: set[str] = set()
    for band in Band.objects.all():
        build_band(band)
        slugs.add(band.slug)
    for folder in settings.PUBLIC_DIR.iterdir():
        if folder.is_dir() and folder.name not in slugs | RESERVED_SLUGS:  # genre/ is the site's
            shutil.rmtree(folder)
