import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.options import IS_POPUP_VAR, TO_FIELD_VAR
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import URLPattern, path, reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin

from . import musicbrainz, openstreetmap, public, venue_search
from .models import Account, Band, BandLink, Concert, Event, Membership, Venue, VenueLink

# "View site" in the admin, and the guide link on its home page: the public pages
admin.site.site_url = public.site_url() + "/"


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


class ConcertForm(forms.ModelForm):
    """A concert, on its own page or in its block on the band's page. The other bands are typed as one line of
    names, in order: Django cannot put a table of them inside the band page's blocks, and one line is quicker to
    type on a phone than a table anyway."""

    other_bands = forms.CharField(
        required=False,
        help_text="Separated by commas, in order. A band on this site links to its page.",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["other_bands"] = ", ".join(self.instance.other_band_names())

    def clean_other_bands(self) -> list[str]:
        return [name.strip() for name in self.cleaned_data["other_bands"].split(",") if name.strip()]

    def save(self, commit: bool = True) -> Concert:
        concert = super().save(commit)
        if commit:  # the band page's blocks; the concert's own page saves later, see ConcertAdmin.save_related
            self.save_other_bands()
        return concert

    def save_other_bands(self) -> None:
        concert = self.instance
        saved = [(act.band, act.name) for act in concert.other_bands.all()]
        # A band already linked stays linked, even when another band has the same name
        linked = {band.name.lower(): band for band, _ in saved if band}

        def row(name: str) -> tuple[Band | None, str]:
            band = linked.get(name.lower())
            if band is None:
                # Any band of the site, even one this account does not manage (band names are public); linked
                # only when the name is a single band: band names are not unique
                bands = list(Band.objects.filter(name__iexact=name)[:2])
                band = bands[0] if len(bands) == 1 else None
            return band, "" if band else name

        # Compared with the rows rather than with the text shown: an old row "A, B" looks like the line "A, B",
        # and a name typed before its band joined the site gets its link on the next save
        rows = [row(name) for name in self.cleaned_data["other_bands"]]
        if rows != saved:
            concert.other_bands.all().delete()
            for band, name in rows:
                concert.other_bands.create(band=band, name=name)


class ConcertInline(admin.StackedInline):
    """One block per concert rather than a wide table: easier to read and fill in on a phone."""

    model = Concert
    form = ConcertForm
    extra = 1
    fieldsets = [
        (
            None,
            {
                "fields": [
                    ("starts_at", "time_tba"),
                    "ends_at",
                    "venue",
                    "event",
                    "status",
                    ("ticket_url", "entry"),
                    "other_bands",
                ]
            },
        ),
        # Rarely needed: the previous date is set on its own, cleared by hand only to undo a typo
        ("More", {"fields": ["previous_starts_at", "announced_at"], "classes": ["collapse"]}),
    ]
    autocomplete_fields = ["venue", "event"]
    show_change_link = True

    def get_queryset(self, request: HttpRequest) -> QuerySet[Concert]:
        # Upcoming concerts only, so the page stays short; past ones are in the list of concerts.
        # Their other bands in one more query, rather than one per concert
        return (
            super()
            .get_queryset(request)
            .filter(starts_at__gte=public.start_of_today())
            .prefetch_related(public.OTHER_BANDS)
        )


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
    list_display = ["name", "slug", "checked_at"]  # sort by "dates checked": the bands to look at again
    search_fields = ["name"]
    prepopulated_fields = {"slug": ["name"]}
    inlines = [ConcertInline, BandLinkInline, MembershipInline]
    fields = ["name", "slug", "musicbrainz_id", "website"]  # the style has its own page

    def get_queryset(self, request: HttpRequest) -> QuerySet[Band]:
        # Also used by the change, delete and autocomplete views: other bands are "not found"
        return super().get_queryset(request).filter(pk__in=member_bands(request))

    def save_model(self, request: HttpRequest, obj: Band, form: Any, change: bool) -> None:
        obj.checked_at = timezone.now()
        super().save_model(request, obj, form, change)

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
        return HttpResponse(
            public.band_page(band, concerts, [link.url for link in band.links.all()], band.members.exists())
        )

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


def popup_markers(request: HttpRequest) -> dict[str, str]:
    """The search opened from the "+" next to a concert's venue, in a small window: Django marks such pages with
    these parameters. They travel along the search, so that saving the venue, or picking one already added,
    puts it into the concert and closes the window."""
    return {key: request.GET[key] for key in (IS_POPUP_VAR, TO_FIELD_VAR) if key in request.GET}


def popup_pick(request: HttpRequest, obj: Venue | Event) -> HttpResponse:
    """In the small window opened from a concert: put a venue or event already added into the concert, with
    the same page Django shows after saving a new one there, which fills the field and closes the window."""
    data = json.dumps({"value": str(obj.pk), "obj": str(obj)})
    return TemplateResponse(request, "admin/popup_response.html", {"popup_response_data": data})


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ["name", "city", "postal_code", "country"]
    list_filter = ["country", "city"]
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
            path("<int:venue_id>/pick/", self.admin_site.admin_view(self.pick), name="concerts_venue_pick"),
        ] + super().get_urls()

    def find_venue(self, request: HttpRequest) -> HttpResponse:
        """Search MusicBrainz and OpenStreetMap by name, and OpenStreetMap by address, in one list; nothing is
        saved here."""
        if not self.has_add_permission(request):
            raise PermissionDenied
        name, address, postal_code, city = (
            request.GET.get(key, "").strip() for key in ("name", "address", "postal_code", "city")
        )
        popup = popup_markers(request)
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
        if address:
            try:
                at_address = openstreetmap.search_address(address, postal_code, city)
            except openstreetmap.OpenStreetMapError as error:
                messages.warning(request, str(error))
            else:
                # A venue found by both its name and its address is listed once
                seen = {result["osm_id"] for result in found_openstreetmap}
                found_openstreetmap += [result for result in at_address if result["osm_id"] not in seen]
        results = venue_search.merge(found_musicbrainz, found_openstreetmap)
        mbids = [result["musicbrainz_id"] for result in results if result.get("musicbrainz_id")]
        osm_ids = [result["osm_id"] for result in results if result.get("osm_id")]
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
            elif not mbid:  # the address alone: its place on the map, to check it
                lat, lon = result["latitude"], result["longitude"]
                sources.append(
                    ("OpenStreetMap", f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=19/{lat}/{lon}")
                )
            fields = {key: value for key, value in result.items() if key != "type" and value}
            if not result["name"] and name:  # the address alone: the venue's name is the one searched for
                fields["name"] = name
            initial = urlencode(fields | popup)
            venue = known.get(mbid or "") or known.get(osm_id or "")
            venue_url = ""
            if venue:
                venue_url = (
                    reverse("admin:concerts_venue_pick", args=[venue.pk]) + "?" + urlencode(popup)
                    if popup
                    else reverse("admin:concerts_venue_change", args=[venue.pk])
                )
            rows.append(
                {
                    "result": result,
                    "sources": sources,
                    "venue": venue,
                    "venue_url": venue_url,
                    # A MusicBrainz search has no website: unless OpenStreetMap gave one, "Use this" fetches the
                    # place first, passing on the rest
                    "use_url": reverse("admin:concerts_venue_musicbrainz_prefill", args=[mbid]) + "?" + initial
                    if mbid and not result.get("website")
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
                "address": address,
                "postal_code": postal_code,
                "city": city,
                "rows": rows,
                "popup": popup,
                "is_popup": bool(popup),  # the admin then leaves out its header, as in its own small windows
            },
        )

    def musicbrainz_prefill(self, request: HttpRequest, mbid: UUID) -> HttpResponse:
        """ "Use this" on a MusicBrainz result without a website: its search results have none, so fetch the
        place first, then open the usual "Add venue" form, pre-filled; the person checks it and saves. (Other
        results already have everything: their "Use this" opens the form directly.)"""
        if not self.has_add_permission(request):
            raise PermissionDenied
        # The admin "add" form reads initial values from the query string; "type" is not a Venue field.
        # The search passed on what it found, so the form opens even if MusicBrainz does not answer now
        initial: dict[str, Any] = {key: value for key, value in request.GET.items() if key != "type"}
        initial["musicbrainz_id"] = str(mbid)
        try:
            fields = musicbrainz.place(str(mbid))
        except musicbrainz.MusicBrainzError as error:
            messages.warning(request, f"{error} The website could not be fetched: add it if you know it.")
        else:
            initial |= {key: value for key, value in fields.items() if value and key != "type"}
        return redirect(reverse("admin:concerts_venue_add") + "?" + urlencode(initial))

    def pick(self, request: HttpRequest, venue_id: int) -> HttpResponse:
        venue = get_object_or_404(Venue, pk=venue_id)
        if not self.has_view_permission(request, venue):
            raise PermissionDenied
        return popup_pick(request, venue)


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ["name", "website"]
    search_fields = ["name"]
    add_form_template = "admin/concerts/event/add_form.html"

    def get_urls(self) -> list[URLPattern]:
        return [
            path("search/", self.admin_site.admin_view(self.find_event), name="concerts_event_search"),
            path(
                "musicbrainz/<uuid:mbid>/",
                self.admin_site.admin_view(self.musicbrainz_prefill),
                name="concerts_event_musicbrainz_prefill",
            ),
            path("<int:event_id>/pick/", self.admin_site.admin_view(self.pick), name="concerts_event_pick"),
        ] + super().get_urls()

    def find_event(self, request: HttpRequest) -> HttpResponse:
        """Search MusicBrainz's festivals by name (it has no other events); nothing is saved here."""
        if not self.has_add_permission(request):
            raise PermissionDenied
        name = request.GET.get("name", "").strip()
        popup = popup_markers(request)
        found: list[musicbrainz.FestivalFields] = []
        if name:
            try:
                found = musicbrainz.search_festivals(name)
            except musicbrainz.MusicBrainzError as error:
                messages.warning(request, str(error))
        known = {
            str(event.musicbrainz_id): event
            for event in Event.objects.filter(musicbrainz_id__in=[result["musicbrainz_id"] for result in found])
        }
        rows = []
        for result in found:
            mbid = result["musicbrainz_id"]
            event = known.get(mbid)
            event_url = ""
            if event:
                event_url = (
                    reverse("admin:concerts_event_pick", args=[event.pk]) + "?" + urlencode(popup)
                    if popup
                    else reverse("admin:concerts_event_change", args=[event.pk])
                )
            rows.append(
                {
                    "result": result,
                    "musicbrainz_url": f"https://musicbrainz.org/series/{mbid}",
                    "event": event,
                    "event_url": event_url,
                    # The name travels along, so the form opens even if MusicBrainz does not answer for the website
                    "use_url": reverse("admin:concerts_event_musicbrainz_prefill", args=[mbid])
                    + "?"
                    + urlencode({"name": result["name"]} | popup),
                }
            )
        return TemplateResponse(
            request,
            "admin/concerts/event/search.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "Find a festival",
                "name": name,
                "rows": rows,
                "popup": popup,
                "is_popup": bool(popup),
            },
        )

    def musicbrainz_prefill(self, request: HttpRequest, mbid: UUID) -> HttpResponse:
        """ "Use this": fetch the festival's website, then open the "Add event" form, pre-filled."""
        if not self.has_add_permission(request):
            raise PermissionDenied
        initial: dict[str, Any] = dict(request.GET.items()) | {"musicbrainz_id": str(mbid)}
        try:
            initial["website"] = musicbrainz.festival_website(str(mbid))
        except musicbrainz.MusicBrainzError as error:
            messages.warning(request, f"{error} The website could not be fetched: add it if you know it.")
        return redirect(reverse("admin:concerts_event_add") + "?" + urlencode(initial))

    def pick(self, request: HttpRequest, event_id: int) -> HttpResponse:
        event = get_object_or_404(Event, pk=event_id)
        if not self.has_view_permission(request, event):
            raise PermissionDenied
        return popup_pick(request, event)


@admin.register(Concert)
class ConcertAdmin(admin.ModelAdmin):
    list_display = ["starts_at", "band", "venue", "status"]
    list_filter = ["status", ("band", admin.RelatedOnlyFieldListFilter), "venue__city"]
    search_fields = ["band__name", "venue__name", "venue__city", "other_bands__name", "other_bands__band__name"]
    form = ConcertForm
    date_hierarchy = "starts_at"
    autocomplete_fields = ["band", "venue", "event"]

    def get_queryset(self, request: HttpRequest) -> QuerySet[Concert]:
        return super().get_queryset(request).filter(band__in=member_bands(request))

    def formfield_for_foreignkey(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        # The form refuses any other band, even one typed into the request by hand
        if db_field.name == "band":
            kwargs["queryset"] = member_bands(request)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request: HttpRequest, obj: Concert, form: Any, change: bool) -> None:
        super().save_model(request, obj, form, change)
        # An update rather than a save: the concert's own save already rebuilds the band's page
        Band.objects.filter(pk=obj.band_id).update(checked_at=timezone.now())

    def save_related(self, request: HttpRequest, form: Any, formsets: Any, change: bool) -> None:
        # The concert is saved by now (the admin saves its form with commit=False, then the concert)
        super().save_related(request, form, formsets, change)
        form.save_other_bands()

    def response_add(self, request: HttpRequest, obj: Concert, post_url_continue: str | None = None) -> HttpResponse:
        response = super().response_add(request, obj, post_url_continue)
        # Opened with "Add a concert" from a band's page (?band=): back to that page after saving, or, with
        # "Save and add another", a new form for the same band
        if "band" not in request.GET or "_continue" in request.POST or IS_POPUP_VAR in request.POST:
            return response
        if "_addanother" in request.POST:
            return redirect(request.get_full_path())
        return redirect("admin:concerts_band_change", obj.band_id)
