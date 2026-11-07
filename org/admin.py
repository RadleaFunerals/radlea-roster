from django.contrib import admin

from .models import Location, StaffMember


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "short_name",
        "name",
        "has_gentle_way",
        "weeknight_standby_start",
        "weekend_standby_start",
        "standby_end",
        "active",
    )
    list_editable = ("active",)


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "role_title",
        "employment_type",
        "award_grade",
        "home_location",
        "can_do_transfers",
        "gentle_way_trained",
        "on_roster",
        "active",
        "user",
    )
    list_filter = ("home_location", "employment_type", "can_do_transfers", "gentle_way_trained", "active")
    search_fields = ("first_name", "last_name", "preferred_name", "role_title")
    filter_horizontal = ("extra_locations", "editor_locations")
    fieldsets = (
        ("Person", {"fields": ("first_name", "last_name", "preferred_name", "mobile", "email", "user")}),
        (
            "Employment",
            {"fields": ("role_title", "employment_type", "award_grade", "start_date", "contracted_weekly_hours")},
        ),
        ("Where they work", {"fields": ("home_location", "extra_locations")}),
        (
            "What they can be rostered for",
            {"fields": ("on_roster", "can_do_transfers", "gentle_way_trained", "editor_locations")},
        ),
        ("Status", {"fields": ("active", "notes")}),
    )
