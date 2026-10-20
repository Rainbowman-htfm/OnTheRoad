from urllib.parse import urlencode

from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from . import musicbrainz
from .models import Account, Band, BandLink, Concert, Membership, Venue, VenueLink


@admin.register(Account)
class AccountAdmin(UserAdmin):
    # UserAdmin screens, with the email in place of the username
    ordering = ["email"]
    list_display = ["email", "first_name", "last_name", "is_staff"]
    search_fields = ["email", "first_name", "last_name"]
    fieldsets = [
        (None, {"fields": ["email", "password"]}),
        ("Identity", {"fields": ["first_name", "last_name"]}),
        ("Permissions", {"fields": ["is_active", "is_staff", "is_superuser", "groups", "user_permissions"]}),
        ("Dates", {"fields": ["last_login", "date_joined"]}),
    ]
    add_fieldsets = [(None, {"classes": ["wide"], "fields": ["email", "password1", "password2"]})]


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0


class BandLinkInline(admin.TabularInline):
    model = BandLink
    extra = 1


class VenueLinkInline(admin.TabularInline):
    model = VenueLink
    extra = 1


class ConcertInline(admin.TabularInline):
    model = Concert
    extra = 1
    fields = ["starts_at", "venue", "status", "ticket_url", "support_acts"]
    autocomplete_fields = ["venue"]


@admin.register(Band)
class BandAdmin(admin.ModelAdmin):
    list_display = ["name", "slug"]
    search_fields = ["name"]
    prepopulated_fields = {"slug": ["name"]}
    inlines = [ConcertInline, BandLinkInline, MembershipInline]


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ["name", "city", "postal_code"]
    list_filter = ["city"]
    search_fields = ["name", "city"]
    inlines = [VenueLinkInline]
    add_form_template = "admin/concerts/venue/add_form.html"

    def get_urls(self):
        return [
            path("musicbrainz/", self.admin_site.admin_view(self.musicbrainz_search), name="concerts_venue_musicbrainz"),
            path("musicbrainz/<uuid:mbid>/", self.admin_site.admin_view(self.musicbrainz_use),
                 name="concerts_venue_musicbrainz_use"),
        ] + super().get_urls()

    def musicbrainz_search(self, request):
        """Search MusicBrainz places by name and city; nothing is saved here."""
        if not self.has_add_permission(request):
            raise PermissionDenied
        name, city = request.GET.get("name", "").strip(), request.GET.get("city", "").strip()
        results = []
        if name:
            try:
                results = musicbrainz.search_places(name, city)
            except musicbrainz.MusicBrainzError as error:
                messages.error(request, str(error))
            existing = Venue.objects.filter(musicbrainz_id__in=[r["musicbrainz_id"] for r in results])
            known = {str(venue.musicbrainz_id): venue for venue in existing}
            for result in results:
                result["venue"] = known.get(result["musicbrainz_id"])
        return TemplateResponse(request, "admin/concerts/venue/musicbrainz.html", {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Search MusicBrainz",
            "name": name,
            "city": city,
            "results": results,
        })

    def musicbrainz_use(self, request, mbid):
        """Open the usual "Add venue" form, pre-filled; the person checks it and saves."""
        if not self.has_add_permission(request):
            raise PermissionDenied
        try:
            fields = musicbrainz.place(mbid)
        except musicbrainz.MusicBrainzError as error:
            messages.error(request, str(error))
            return redirect(reverse("admin:concerts_venue_musicbrainz"))
        fields.pop("type")
        # The admin "add" form reads initial values from the query string
        return redirect(reverse("admin:concerts_venue_add") + "?" + urlencode(fields))


@admin.register(Concert)
class ConcertAdmin(admin.ModelAdmin):
    list_display = ["starts_at", "band", "venue", "status"]
    list_filter = ["status", "band", "venue__city"]
    search_fields = ["band__name", "venue__name", "venue__city", "support_acts"]
    date_hierarchy = "starts_at"
    autocomplete_fields = ["band", "venue"]
