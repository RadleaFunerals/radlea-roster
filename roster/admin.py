from django.contrib import admin

from .models import (
    ChangeLog,
    GentleWayAttendance,
    OnCallNight,
    PublicHoliday,
    RosterWeek,
    Shift,
    ShiftPattern,
    SwapRequest,
    UnavailabilityRequest,
)


@admin.register(PublicHoliday)
class PublicHolidayAdmin(admin.ModelAdmin):
    list_display = ("date", "name", "location", "part_day_from", "source")
    list_filter = ("location",)
    date_hierarchy = "date"


@admin.register(ShiftPattern)
class ShiftPatternAdmin(admin.ModelAdmin):
    list_display = ("staff", "location", "weekday", "start", "end", "unpaid_break_minutes", "shift_type")
    list_filter = ("location", "weekday")


@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ("date", "staff", "location", "start", "end", "shift_type", "note")
    list_filter = ("location", "shift_type")
    date_hierarchy = "date"
    search_fields = ("staff__first_name", "staff__last_name", "note")


@admin.register(OnCallNight)
class OnCallNightAdmin(admin.ModelAdmin):
    list_display = ("date", "location", "staff_one", "staff_two", "note")
    list_filter = ("location",)
    date_hierarchy = "date"


@admin.register(GentleWayAttendance)
class GentleWayAttendanceAdmin(admin.ModelAdmin):
    list_display = ("date", "start", "process_type", "staff", "status", "case_reference")
    list_filter = ("process_type", "status")
    date_hierarchy = "date"


@admin.register(RosterWeek)
class RosterWeekAdmin(admin.ModelAdmin):
    list_display = ("week_start", "location", "published_at", "published_by")
    list_filter = ("location",)


@admin.register(UnavailabilityRequest)
class UnavailabilityRequestAdmin(admin.ModelAdmin):
    list_display = ("staff", "kind", "start_date", "end_date", "status", "decided_by")
    list_filter = ("status", "kind")


@admin.register(SwapRequest)
class SwapRequestAdmin(admin.ModelAdmin):
    list_display = ("requester", "item_label", "proposed_staff", "status", "decided_by")
    list_filter = ("status",)


@admin.register(ChangeLog)
class ChangeLogAdmin(admin.ModelAdmin):
    """Read-only: the change history must not be edited."""

    list_display = ("at", "user", "location", "action", "summary")
    list_filter = ("location", "action")
    search_fields = ("summary",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
