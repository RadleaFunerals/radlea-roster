from django.contrib import admin

from .models import Area, Location, Skill, StaffMember, StaffSkill


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


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "description", "sort_order")


@admin.register(Area)
class AreaAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "colour", "required_skill", "sort_order", "active")


class StaffSkillInline(admin.TabularInline):
    model = StaffSkill
    extra = 1


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    inlines = [StaffSkillInline]
    list_display = (
        "display_name",
        "role_title",
        "employment_type",
        "award_grade",
        "home_location",
        "primary_area",
        "on_roster",
        "active",
        "user",
    )
    list_filter = ("home_location", "employment_type", "primary_area", "active")
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
            {"fields": ("primary_area", "on_roster", "editor_locations")},
        ),
        ("Status", {"fields": ("active", "notes")}),
    )
