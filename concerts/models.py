from typing import TYPE_CHECKING, Any, ClassVar

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

if TYPE_CHECKING:
    from django.db.models.fields.related_descriptors import ManyRelatedManager, RelatedManager


class AccountManager(BaseUserManager["Account"]):
    """Creates accounts from an email address, since there is no username."""

    def create_user(self, email: str, password: str | None = None, **fields: Any) -> "Account":
        account = self.model(email=self.normalize_email(email), **fields)
        # Without a password, the account can only log in through an emailed link (step 4)
        account.set_password(password)
        account.save(using=self._db)
        return account

    def create_superuser(self, email: str, password: str | None = None, **fields: Any) -> "Account":
        fields.setdefault("is_staff", True)
        fields.setdefault("is_superuser", True)
        return self.create_user(email, password, **fields)


class Account(AbstractUser):
    """A person who can log in, identified by their email address."""

    username = None  # type: ignore[assignment]  # removed: the email is the identifier
    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    # Reverse relations, created by Django from the other side; declared for editors and type checkers
    bands: "ManyRelatedManager[Band, Membership]"

    objects: ClassVar[AccountManager] = AccountManager()  # type: ignore[assignment]  # no username to ask for

    def __str__(self) -> str:
        return self.email


# "#c4161c": checked before saving, since the colour ends up in the public pages' CSS
hex_color = RegexValidator(r"^#[0-9a-fA-F]{6}$", "Enter a colour like #c4161c.")


# Addresses used by the site itself: a band page there would be hidden by it (config/urls.py)
RESERVED_SLUGS = {"admin", "login", "static"}


def reserved_slug(value: str) -> None:
    if value in RESERVED_SLUGS:
        raise ValidationError("This address is used by the site itself; choose another one.")


class Band(models.Model):
    class Theme(models.TextChoices):
        LIGHT = "light"
        DARK = "dark"

    class Font(models.TextChoices):
        SANS = "sans", "sans-serif"
        SERIF = "serif"
        MONO = "mono", "monospace"

    class Corners(models.TextChoices):
        SLIGHT = "slight", "slightly rounded"
        SQUARE = "square"
        ROUND = "round"

    name = models.CharField(max_length=200)
    slug = models.SlugField(
        max_length=200,
        unique=True,
        help_text="Public page address: /<slug>/",
        validators=[reserved_slug],
    )
    # MusicBrainz identifiers (MBID) are UUIDs; null rather than empty so uniqueness ignores bands without one
    musicbrainz_id = models.UUIDField("MusicBrainz ID", null=True, blank=True, unique=True)
    # Official site (schema.org "url"); other profiles go in BandLink (schema.org "sameAs")
    website = models.URLField(blank=True)
    # Page style, also the widget's starting point; an empty colour follows the theme
    theme = models.CharField(max_length=10, choices=Theme.choices, default=Theme.LIGHT)
    accent_color = models.CharField(max_length=7, blank=True, validators=[hex_color], help_text="Buttons and links.")
    text_color = models.CharField(max_length=7, blank=True, validators=[hex_color])
    background_color = models.CharField(max_length=7, blank=True, validators=[hex_color], help_text="Page only.")
    font = models.CharField(max_length=10, choices=Font.choices, default=Font.SANS)
    corners = models.CharField(max_length=10, choices=Corners.choices, default=Corners.SLIGHT)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, through="Membership", related_name="bands", blank=True)
    # Last save of the band or one of its concerts in the admin: whoever saves has just checked its dates, even
    # without changing anything. Shown on the band's page: how fresh its dates are, whoever keeps them
    checked_at = models.DateTimeField("dates checked", null=True, blank=True, editable=False)

    # Reverse relations, created by Django from the other side; declared for editors and type checkers
    links: "RelatedManager[BandLink]"
    concerts: "RelatedManager[Concert]"

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class BandLink(models.Model):
    """One row per link (Instagram, Bandcamp…); the platform is read from the address."""

    band = models.ForeignKey(Band, on_delete=models.CASCADE, related_name="links")
    url = models.URLField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["band", "url"], name="unique_band_link")]

    def __str__(self) -> str:
        return self.url


class Membership(models.Model):
    """Join table account ↔ band: a band has several members, a person can play in several bands."""

    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    band = models.ForeignKey(Band, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["account", "band"], name="unique_membership")]

    def __str__(self) -> str:
        return f"{self.account} → {self.band}"


class Venue(models.Model):
    """Shared between bands."""

    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300, blank=True)
    city = models.CharField(max_length=100)
    postal_code = models.CharField(max_length=10, blank=True)
    # ISO 3166 code, "FR": short, the same in every language, and what schema.org and OpenStreetMap use.
    # Blank when not known: MusicBrainz's search does not give it
    country = models.CharField(
        max_length=2,
        blank=True,
        validators=[RegexValidator("^[A-Z]{2}$", "Two capital letters: FR, BE, DE…")],
        help_text="Two-letter code: FR, BE, DE…",
    )
    # Decimal rather than float: exact value, 6 decimal places ≈ 10 cm
    latitude = models.DecimalField(max_digits=8, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # MusicBrainz "place" identifier
    musicbrainz_id = models.UUIDField("MusicBrainz ID", null=True, blank=True, unique=True)
    # OpenStreetMap object, "node/3772940557"; null rather than empty so uniqueness ignores venues without one
    osm_id = models.CharField("OpenStreetMap ID", max_length=30, null=True, blank=True, unique=True)
    website = models.URLField(blank=True)

    # Reverse relations, created by Django from the other side; declared for editors and type checkers
    links: "RelatedManager[VenueLink]"
    concerts: "RelatedManager[Concert]"

    class Meta:
        ordering = ["city", "name"]

    def __str__(self) -> str:
        # The country tells apart "Paris" in France and "Paris" in Texas
        return f"{self.name} ({', '.join(part for part in (self.city, self.country) if part)})"


class VenueLink(models.Model):
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name="links")
    url = models.URLField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["venue", "url"], name="unique_venue_link")]

    def __str__(self) -> str:
        return self.url


class Event(models.Model):
    """A named event concerts are part of: a festival ("Hellfest"), or a night an association organises in a
    village hall ("Concert 100% Girls"). Not one edition: the year is the concert's date, so the same event
    comes back every year. Shared between bands, like venues; each concert keeps its own venue."""

    # Not unique: there is a Hellfest in France and one in the USA. The search field avoids duplicates
    name = models.CharField(max_length=200)
    website = models.URLField(blank=True)
    # For a festival, the MusicBrainz "series" of type Festival, not one year's event; null rather than empty
    musicbrainz_id = models.UUIDField("MusicBrainz ID", null=True, blank=True, unique=True)

    # Reverse relation, created by Django from the other side; declared for editors and type checkers
    concerts: "RelatedManager[Concert]"

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Concert(models.Model):
    class Status(models.TextChoices):
        PLANNED = "planned"
        CANCELLED = "cancelled"
        SOLD_OUT = "sold_out", "sold out"

    # One choice rather than two boxes ("free", "pay what you can") that could both be ticked
    class Entry(models.TextChoices):
        TICKETS = "tickets"
        FREE = "free", "free entry"
        PAY_WHAT_YOU_CAN = "pay_what_you_can", "pay what you can"

    # Deleting a band deletes its concerts; a venue that still has concerts cannot be deleted
    band = models.ForeignKey(Band, on_delete=models.CASCADE, related_name="concerts")
    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="concerts")
    event = models.ForeignKey(
        Event,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="concerts",
        help_text="A festival, or a night with a name: shown before the venue.",
    )
    # Stored in UTC, displayed in the TIME_ZONE setting (USE_TZ = True)
    starts_at = models.DateTimeField("date and time")
    # Filled in on save when the day changes, and kept through later moves: the date fans first had in mind.
    # Editable, so a typo fixed right after entering the concert does not show as a reschedule
    previous_starts_at = models.DateTimeField(
        "previous date", null=True, blank=True, help_text="Set automatically when the day changes."
    )
    # Optional: often unknown when a concert is announced
    ends_at = models.DateTimeField("end", null=True, blank=True)
    # The day is known but not the time: outputs show the date only, never the time in starts_at
    time_tba = models.BooleanField("time not announced yet", default=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PLANNED)
    # The ticket page, or for a free or pay-what-you-can concert the event's page or announcement
    ticket_url = models.URLField(
        "link", blank=True, help_text="Tickets: the ticket page. Otherwise the event's page or announcement."
    )
    entry = models.CharField(max_length=20, choices=Entry.choices, default=Entry.TICKETS)
    announced_at = models.DateTimeField(
        default=timezone.now,
        help_text="Shown as new for 14 days. Set an earlier date for a concert already announced elsewhere.",
    )

    class Meta:
        ordering = ["starts_at"]
        constraints = [
            # Checked by the database itself, whatever saves the concert
            models.CheckConstraint(
                condition=models.Q(ends_at__isnull=True) | models.Q(ends_at__gt=models.F("starts_at")),
                name="concert_ends_after_start",
                violation_error_message="The end must be after the start.",
            )
        ]

    # Reverse relation, created by Django from the other side; declared for editors and type checkers
    other_bands: "RelatedManager[OtherBand]"
    # Not stored: each other band's name and page address, "" when not on this site (public.link_other_bands)
    other_band_links: list[tuple[str, str]]

    def __str__(self) -> str:
        return f"{self.band} — {self.venue} — {timezone.localtime(self.starts_at):%Y-%m-%d}"

    def other_band_names(self) -> list[str]:
        # .all() rather than a new query, so a prefetch_related("other_bands__band") is used
        return [str(act) for act in self.other_bands.all()]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk and self.previous_starts_at is None:
            saved = Concert.objects.filter(pk=self.pk).values_list("starts_at", flat=True).first()
            # The day, not the time: announcing the time of a "time TBA" concert is not a reschedule
            if saved and timezone.localdate(saved) != timezone.localdate(self.starts_at):
                self.previous_starts_at = saved
        # Moved back to its first day: not rescheduled after all
        if self.previous_starts_at and timezone.localdate(self.previous_starts_at) == timezone.localdate(
            self.starts_at
        ):
            self.previous_starts_at = None
        super().save(*args, **kwargs)


class OtherBand(models.Model):
    """Another band playing the same night ("other band" rather than "support act": the band of the page is not
    always the headliner): a band on this site, linked to its page, or just a name. One table
    for both, so they are listed in the order they were added."""

    concert = models.ForeignKey(Concert, on_delete=models.CASCADE, related_name="other_bands")
    # A band removed from the site stays in the other bands' concerts as a name: it still played those nights.
    # The name is written just before the deletion (signals.py). SET_NULL, not CASCADE: Django lists the rows to
    # delete before that signal, so CASCADE would delete them anyway
    band = models.ForeignKey(
        Band,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="A band on this site: its name will link to its page.",
    )
    name = models.CharField(max_length=300, blank=True, help_text="Any other band, not on this site.")

    class Meta:
        ordering = ["pk"]  # the order they were added
        constraints = [
            # Checked by the database: a band or a name, never both or neither
            models.CheckConstraint(
                condition=(models.Q(band__isnull=False) & models.Q(name=""))
                | (models.Q(band__isnull=True) & ~models.Q(name="")),
                name="other_band_band_or_name",
                violation_error_message="Choose a band on this site or type a name, not both.",
            )
        ]

    def __str__(self) -> str:
        return self.band.name if self.band else self.name
