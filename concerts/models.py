from typing import TYPE_CHECKING, Any, ClassVar

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
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
    slug = models.SlugField(max_length=200, unique=True, help_text="Public page address: /<slug>/")
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
        return f"{self.name} ({self.city})"


class VenueLink(models.Model):
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name="links")
    url = models.URLField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["venue", "url"], name="unique_venue_link")]

    def __str__(self) -> str:
        return self.url


class Concert(models.Model):
    class Status(models.TextChoices):
        PLANNED = "planned"
        CANCELLED = "cancelled"
        SOLD_OUT = "sold_out", "sold out"

    # Deleting a band deletes its concerts; a venue that still has concerts cannot be deleted
    band = models.ForeignKey(Band, on_delete=models.CASCADE, related_name="concerts")
    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="concerts")
    # Stored in UTC, displayed in the TIME_ZONE setting (USE_TZ = True)
    starts_at = models.DateTimeField()
    # Filled in on save when the day changes, and kept through later moves: the date fans first had in mind.
    # Editable, so a typo fixed right after entering the concert does not show as a reschedule
    previous_starts_at = models.DateTimeField(
        "previous date", null=True, blank=True, help_text="Set automatically when the day changes."
    )
    # Optional: often unknown when a concert is announced
    ends_at = models.DateTimeField(null=True, blank=True)
    # The day is known but not the time: outputs show the date only, never the time in starts_at
    time_tba = models.BooleanField("time not announced yet", default=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PLANNED)
    ticket_url = models.URLField(blank=True)
    free_entry = models.BooleanField(default=False)
    support_acts = models.CharField(max_length=300, blank=True)
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

    def __str__(self) -> str:
        return f"{self.band} — {self.venue} — {timezone.localtime(self.starts_at):%Y-%m-%d}"

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
