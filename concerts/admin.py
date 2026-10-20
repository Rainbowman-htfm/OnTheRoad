from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

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


@admin.register(Concert)
class ConcertAdmin(admin.ModelAdmin):
    list_display = ["starts_at", "band", "venue", "status"]
    list_filter = ["status", "band", "venue__city"]
    search_fields = ["band__name", "venue__name", "venue__city", "support_acts"]
    date_hierarchy = "starts_at"
    autocomplete_fields = ["band", "venue"]
