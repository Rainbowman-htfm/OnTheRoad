from collections.abc import Sequence
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import URLPattern, path, reverse
from django.views.decorators.clickjacking import xframe_options_sameorigin

from . import musicbrainz, openstreetmap, public, venue_search
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


class ConcertInline(admin.StackedInline):
    """One block per concert rather than a wide table: easier to read and fill in on a phone."""

    model = Concert
    extra = 1
    fieldsets = [
        (
            None,
            {
                "fields": [
                    ("starts_at", "time_tba"),
                    "ends_at",
                    "venue",
                    "status",
                    ("ticket_url", "free_entry"),
                    "support_acts",
                ]
            },
        ),
        # Rarely needed: the previous date is set on its own, cleared by hand only to undo a typo
        ("More", {"fields": ["previous_starts_at", "announced_at"], "classes": ["collapse"]}),
    ]
    autocomplete_fields = ["venue"]


def member_bands(request: HttpRequest) -> QuerySet[Band]:
    """The bands this account may manage: all of them for a superuser, otherwise the ones it is a member of."""
    if request.user.is_superuser:
        return Band.objects.all()
    return Band.objects.filter(members=request.user.pk)


class StyleForm(forms.ModelForm):
    """A colour picker always sends a colour, so each colour has a "use it" box; unticked, it follows the theme."""

    COLORS = {"accent_color": "#111111", "text_color": "#111111", "background_color": "#ffffff"}

    use_accent_color = forms.BooleanField(required=False)
    use_text_color = forms.BooleanField(required=False)
    use_background_color = forms.BooleanField(required=False)

    class Meta:
        model = Band
        fields = ["theme", "font", "corners", "accent_color", "text_color", "background_color"]
        widgets = {
            name: forms.TextInput(attrs={"type": "color"})
            for name in ["accent_color", "text_color", "background_color"]
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for name, default in self.COLORS.items():
            self.initial["use_" + name] = bool(self.initial.get(name))
            self.initial[name] = self.initial.get(name) or default

    def clean(self) -> dict[str, Any]:
        for name in self.COLORS:
            if not self.cleaned_data.get("use_" + name):
                self.cleaned_data[name] = ""
        return self.cleaned_data


@admin.register(Band)
class BandAdmin(admin.ModelAdmin):
    list_display = ["name", "slug"]
    search_fields = ["name"]
    prepopulated_fields = {"slug": ["name"]}
    inlines = [ConcertInline, BandLinkInline, MembershipInline]
    fields = ["name", "slug", "musicbrainz_id", "website"]  # the style has its own page

    def get_queryset(self, request: HttpRequest) -> QuerySet[Band]:
        # Also used by the change, delete and autocomplete views: other bands are "not found"
        return super().get_queryset(request).filter(pk__in=member_bands(request))

    def get_readonly_fields(self, request: HttpRequest, obj: Band | None = None) -> list[str]:
        # Changing the address would break fans' subscribed feeds: only an administrator does it
        return [] if request.user.is_superuser else ["slug"]

    def get_prepopulated_fields(self, request: HttpRequest, obj: Band | None = None) -> dict[str, Sequence[str]]:
        return self.prepopulated_fields if request.user.is_superuser else {}

    def get_urls(self) -> list[URLPattern]:
        return [
            path("<int:band_id>/share/", self.admin_site.admin_view(self.share), name="concerts_band_share"),
            path("<int:band_id>/style/", self.admin_site.admin_view(self.style), name="concerts_band_style"),
            path(
                "<int:band_id>/style/preview/",
                # Shown in a frame of the style page, so allowed in frames from this site
                self.admin_site.admin_view(xframe_options_sameorigin(self.style_preview)),
                name="concerts_band_style_preview",
            ),
        ] + super().get_urls()

    def style(self, request: HttpRequest, band_id: int) -> HttpResponse:
        """Page style with a live preview; saving rebuilds the public page like any band change."""
        band = get_object_or_404(self.get_queryset(request), pk=band_id)
        if not self.has_change_permission(request, band):
            raise PermissionDenied
        form = StyleForm(request.POST or None, instance=band)
        if form.is_valid():
            form.save()
            messages.success(request, "Page style saved.")
            return redirect(reverse("admin:concerts_band_style", args=[band.pk]))
        return TemplateResponse(
            request,
            "admin/concerts/band/style.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "original": band,
                "title": f"Page style of {band.name}",
                "band": band,
                "form": form,
                "colors": [(form[name], form["use_" + name]) for name in StyleForm.COLORS],
            },
        )

    def style_preview(self, request: HttpRequest, band_id: int) -> HttpResponse:
        """The band page with the style from the address, without saving it."""
        band = get_object_or_404(self.get_queryset(request), pk=band_id)
        if not self.has_change_permission(request, band):
            raise PermissionDenied
        form = StyleForm(request.GET, instance=band)
        form.is_valid()  # copies the valid values onto this unsaved band; invalid ones keep the saved style
        concerts = list(public.upcoming_concerts(band))
        return HttpResponse(public.band_page(band, concerts, [link.url for link in band.links.all()]))

    def share(self, request: HttpRequest, band_id: int) -> HttpResponse:
        """Links, widget settings with a live preview, and the code to paste on the band's website."""
        band = get_object_or_404(self.get_queryset(request), pk=band_id)
        if not self.has_view_permission(request, band):
            raise PermissionDenied
        concerts = list(public.upcoming_concerts(band))
        band_links = [link.url for link in band.links.all()]
        return TemplateResponse(
            request,
            "admin/concerts/band/share.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "original": band,
                "title": f"Share {band.name}",
                "band": band,
                "site_url": public.site_url(),
                "page_url": public.page_url(band),
                "embed_code": public.embed_code(band),
                "embed_iframe": public.embed_code(band, "concerts/embed_iframe.html"),
                "events_json": public.json_for_script([public.music_event(c, band_links) for c in concerts]),
            },
        )


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ["name", "city", "postal_code"]
    list_filter = ["city"]
    search_fields = ["name", "city"]
    inlines = [VenueLinkInline]
    add_form_template = "admin/concerts/venue/add_form.html"

    def get_urls(self) -> list[URLPattern]:
        return [
            path("search/", self.admin_site.admin_view(self.find_venue), name="concerts_venue_search"),
            path(
                "musicbrainz/<uuid:mbid>/",
                self.admin_site.admin_view(self.musicbrainz_prefill),
                name="concerts_venue_musicbrainz_prefill",
            ),
        ] + super().get_urls()

    def find_venue(self, request: HttpRequest) -> HttpResponse:
        """Search MusicBrainz and OpenStreetMap by name and city, in one list; nothing is saved here."""
        if not self.has_add_permission(request):
            raise PermissionDenied
        name, city = request.GET.get("name", "").strip(), request.GET.get("city", "").strip()
        found_musicbrainz: list[musicbrainz.VenueFields] = []
        found_openstreetmap: list[openstreetmap.VenueFields] = []
        if name:
            # One source down: the other's results still show
            try:
                found_musicbrainz = musicbrainz.search_places(name, city)
            except musicbrainz.MusicBrainzError as error:
                messages.warning(request, str(error))
            try:
                found_openstreetmap = openstreetmap.search_places(name, city)
            except openstreetmap.OpenStreetMapError as error:
                messages.warning(request, str(error))
        results = venue_search.merge(found_musicbrainz, found_openstreetmap)
        mbids = [result["musicbrainz_id"] for result in results if "musicbrainz_id" in result]
        osm_ids = [result["osm_id"] for result in results if "osm_id" in result]
        existing = Venue.objects.filter(Q(musicbrainz_id__in=mbids) | Q(osm_id__in=osm_ids))
        known = {str(venue.musicbrainz_id): venue for venue in existing} | {venue.osm_id: venue for venue in existing}
        rows = []
        for result in results:
            mbid, osm_id = result.get("musicbrainz_id"), result.get("osm_id")
            sources = []
            if mbid:
                sources.append(("MusicBrainz", f"https://musicbrainz.org/place/{mbid}"))
            if osm_id:
                sources.append(("OpenStreetMap", f"https://www.openstreetmap.org/{osm_id}"))
            initial = urlencode({key: value for key, value in result.items() if key != "type"})
            rows.append(
                {
                    "result": result,
                    "sources": sources,
                    "venue": known.get(mbid or "") or known.get(osm_id or ""),
                    # A MusicBrainz search has no website: "Use this" fetches the place first, passing on the rest
                    "use_url": reverse("admin:concerts_venue_musicbrainz_prefill", args=[mbid]) + "?" + initial
                    if mbid
                    else reverse("admin:concerts_venue_add") + "?" + initial,
                }
            )
        return TemplateResponse(
            request,
            "admin/concerts/venue/search.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "Find a venue",
                "name": name,
                "city": city,
                "rows": rows,
            },
        )

    def musicbrainz_prefill(self, request: HttpRequest, mbid: UUID) -> HttpResponse:
        """ "Use this" on a MusicBrainz result: its search results have no website, so fetch the place first,
        then open the usual "Add venue" form, pre-filled; the person checks it and saves. (An OpenStreetMap
        result already has everything: its "Use this" opens the form directly.)"""
        if not self.has_add_permission(request):
            raise PermissionDenied
        try:
            fields = musicbrainz.place(str(mbid))
        except musicbrainz.MusicBrainzError as error:
            messages.error(request, str(error))
            return redirect(reverse("admin:concerts_venue_search"))
        # The admin "add" form reads initial values from the query string; "type" is not a Venue field.
        # Values passed on from the search (the same venue on OpenStreetMap) fill what MusicBrainz lacks
        found = {key: value for key, value in fields.items() if value and key != "type"}
        initial = {key: value for key, value in request.GET.items() if key != "type"} | found
        return redirect(reverse("admin:concerts_venue_add") + "?" + urlencode(initial))


@admin.register(Concert)
class ConcertAdmin(admin.ModelAdmin):
    list_display = ["starts_at", "band", "venue", "status"]
    list_filter = ["status", ("band", admin.RelatedOnlyFieldListFilter), "venue__city"]
    search_fields = ["band__name", "venue__name", "venue__city", "support_acts"]
    date_hierarchy = "starts_at"
    autocomplete_fields = ["band", "venue"]

    def get_queryset(self, request: HttpRequest) -> QuerySet[Concert]:
        return super().get_queryset(request).filter(band__in=member_bands(request))

    def formfield_for_foreignkey(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        # The form refuses any other band, even one typed into the request by hand
        if db_field.name == "band":
            kwargs["queryset"] = member_bands(request)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
