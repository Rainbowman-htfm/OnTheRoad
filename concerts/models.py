from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


class AccountManager(BaseUserManager):
    """Creates accounts from an email address, since there is no username."""

    def create_user(self, email, password=None, **fields):
        account = self.model(email=self.normalize_email(email), **fields)
        # Without a password, the account can only log in through an emailed link (step 4)
        account.set_password(password)
        account.save(using=self._db)
        return account

    def create_superuser(self, email, password=None, **fields):
        fields.setdefault("is_staff", True)
        fields.setdefault("is_superuser", True)
        return self.create_user(email, password, **fields)


class Account(AbstractUser):
    """A person who can log in, identified by their email address."""

    username = None
    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = AccountManager()

    def __str__(self):
        return self.email


class Band(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True, help_text="Public page address: /<slug>/")
    # MusicBrainz identifiers (MBID) are UUIDs; null rather than empty so uniqueness ignores bands without one
    musicbrainz_id = models.UUIDField("MusicBrainz ID", null=True, blank=True, unique=True)
    # Official site (schema.org "url"); other profiles go in BandLink (schema.org "sameAs")
    website = models.URLField(blank=True)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, through="Membership", related_name="bands", blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class BandLink(models.Model):
    """One row per link (Instagram, Bandcamp…); the platform is read from the address."""

    band = models.ForeignKey(Band, on_delete=models.CASCADE, related_name="links")
    url = models.URLField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["band", "url"], name="unique_band_link")]

    def __str__(self):
        return self.url


class Membership(models.Model):
    """Join table account ↔ band: a band has several members, a person can play in several bands."""

    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    band = models.ForeignKey(Band, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["account", "band"], name="unique_membership")]

    def __str__(self):
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
    website = models.URLField(blank=True)

    class Meta:
        ordering = ["city", "name"]

    def __str__(self):
        return f"{self.name} ({self.city})"


class VenueLink(models.Model):
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name="links")
    url = models.URLField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["venue", "url"], name="unique_venue_link")]

    def __str__(self):
        return self.url


class Concert(models.Model):
    class Status(models.TextChoices):
        PLANNED = "planned"
        CANCELLED = "cancelled"
        POSTPONED = "postponed"
        SOLD_OUT = "sold_out", "sold out"

    # Deleting a band deletes its concerts; a venue that still has concerts cannot be deleted
    band = models.ForeignKey(Band, on_delete=models.CASCADE, related_name="concerts")
    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="concerts")
    # Stored in UTC, displayed in the TIME_ZONE setting (USE_TZ = True)
    starts_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PLANNED)
    ticket_url = models.URLField(blank=True)
    support_acts = models.CharField(max_length=300, blank=True)
    announced_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["starts_at"]

    def __str__(self):
        return f"{self.band} — {self.venue} — {timezone.localtime(self.starts_at):%Y-%m-%d}"
