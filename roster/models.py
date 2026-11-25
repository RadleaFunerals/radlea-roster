"""
Roster data.

Three kinds of rostered work:
  * Shift            - normal hours (office, mortuary, services, daytime transfers)
  * OnCallNight      - the after-hours transfer team: a pair of people per
                       location per night (weeknights) or per day (weekends
                       and public holidays), matching how standby is paid
  * GentleWayAttendance - a pre-arranged attendance at the Gentle Way unit
                       (main process or short term system check), by any
                       trained staff member

Plus the staff side: UnavailabilityRequest and SwapRequest, and a ChangeLog
so every change to a published roster can be traced.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from org.models import Area, Location, StaffMember

from .timeutils import aware, fmt_date, interval


class PublicHoliday(models.Model):
    date = models.DateField()
    name = models.CharField(max_length=80)
    location = models.ForeignKey(
        Location,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        help_text="Leave blank for a Queensland-wide holiday. Set it for a local show holiday.",
    )
    part_day_from = models.TimeField(
        null=True,
        blank=True,
        help_text="For part-day holidays only (Christmas Eve is 6pm to midnight in Queensland).",
    )
    source = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["date"]

    def __str__(self):
        where = self.location.short_name if self.location else "Qld"
        return f"{fmt_date(self.date)} {self.name} ({where})"

    @classmethod
    def applying(cls, d, location):
        return cls.objects.filter(date=d).filter(Q(location__isnull=True) | Q(location=location))

    @classmethod
    def is_full_day(cls, d, location):
        return cls.applying(d, location).filter(part_day_from__isnull=True).exists()


class RosterWeek(models.Model):
    """Draft/published state of one location's roster for one Monday-Sunday week.

    Staff only see weeks that have been published. Anything changed after
    publishing is flagged "updated" so staff notice it.
    """

    location = models.ForeignKey(Location, on_delete=models.CASCADE)
    week_start = models.DateField()
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        unique_together = [("location", "week_start")]
        ordering = ["-week_start"]

    def __str__(self):
        return f"{self.location} week of {fmt_date(self.week_start)}"

    @property
    def is_published(self):
        return self.published_at is not None


class ShiftPattern(models.Model):
    """A person's usual shift on a weekday. Used to fill a week in one click."""

    staff = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name="patterns")
    location = models.ForeignKey(Location, on_delete=models.CASCADE)
    weekday = models.PositiveSmallIntegerField(
        choices=[(0, "Mon"), (1, "Tue"), (2, "Wed"), (3, "Thu"), (4, "Fri"), (5, "Sat"), (6, "Sun")]
    )
    start = models.TimeField()
    end = models.TimeField()
    unpaid_break_minutes = models.PositiveSmallIntegerField(default=30)
    area = models.ForeignKey(Area, on_delete=models.PROTECT)

    class Meta:
        ordering = ["staff__last_name", "weekday", "start"]
        verbose_name = "usual shift"

    def __str__(self):
        return f"{self.staff} {self.get_weekday_display()} {self.start:%H:%M}-{self.end:%H:%M}"


class Shift(models.Model):
    staff = models.ForeignKey(StaffMember, on_delete=models.PROTECT, related_name="shifts")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="shifts")
    date = models.DateField()
    start = models.TimeField()
    end = models.TimeField(help_text="A finish earlier than the start is read as the next morning.")
    unpaid_break_minutes = models.PositiveSmallIntegerField(default=30)
    area = models.ForeignKey(Area, on_delete=models.PROTECT, help_text="The business area this shift is for.")
    note = models.CharField(max_length=200, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date", "start"]

    def __str__(self):
        return f"{self.staff} {fmt_date(self.date)} {self.time_label}"

    @property
    def time_label(self):
        return f"{self.start:%H:%M}-{self.end:%H:%M}"

    def interval(self):
        return interval(self.date, self.start, self.end)

    @property
    def paid_hours(self) -> Decimal:
        s, e = self.interval()
        minutes = (e - s).total_seconds() / 60 - (self.unpaid_break_minutes or 0)
        return (Decimal(max(minutes, 0)) / Decimal(60)).quantize(Decimal("0.01"))


class OnCallKind(models.TextChoices):
    WEEKNIGHT = "WN", "Weeknight"
    WEEKEND = "WE", "Weekend day"
    PUBLIC_HOLIDAY = "PH", "Public holiday"


SUPPORT_ROLES = [("TRAINER", "Trainer"), ("SUPERVISOR", "Supervisor"), ("OBSERVER", "Third person")]


class OnCallNight(models.Model):
    """The after-hours transfer team for one location on one date.

    Weeknight: from the location's weeknight start until standby end next
    morning. Weekend/public holiday: the whole day from the weekend start
    until standby end next morning. This mirrors how standby is paid
    (per period Mon-Fri, per day Sat/Sun/PH - Staff Pay Master Register).
    """

    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="oncall_nights")
    date = models.DateField()
    staff_one = models.ForeignKey(
        StaffMember,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name="team member 1",
    )
    staff_two = models.ForeignKey(
        StaffMember,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name="team member 2",
    )
    # Optional third person: a trainer or supervisor going out with a new pair
    # (e.g. two new casuals rostered together).
    support_staff = models.ForeignKey(
        StaffMember,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name="third person",
        help_text="Trainer or supervisor backing up this pair (optional).",
    )
    support_role = models.CharField(max_length=10, choices=SUPPORT_ROLES, default="TRAINER")
    # Who carries this site's after-hours phones. Can be someone outside the
    # pair (e.g. a director holding both sites' phones).
    phone_holder = models.ForeignKey(
        StaffMember,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name="has the phones",
        help_text="Who has this site's after-hours phones.",
    )
    note = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("location", "date")]
        ordering = ["date", "location__sort_order"]
        verbose_name = "after-hours team"
        verbose_name_plural = "after-hours teams"

    def __str__(self):
        names = " + ".join(m.display_name for m in self.members) or "unfilled"
        return f"{self.location} {fmt_date(self.date)}: {names}"

    @property
    def members(self):
        return [m for m in (self.staff_one, self.staff_two) if m is not None]

    @staticmethod
    def kind_for(location, d):
        if PublicHoliday.is_full_day(d, location):
            return OnCallKind.PUBLIC_HOLIDAY
        if d.weekday() >= 5:
            return OnCallKind.WEEKEND
        return OnCallKind.WEEKNIGHT

    @staticmethod
    def interval_for(location, d):
        kind = OnCallNight.kind_for(location, d)
        start_t = (
            location.weeknight_standby_start
            if kind == OnCallKind.WEEKNIGHT
            else location.weekend_standby_start
        )
        return aware(d, start_t), aware(d + timedelta(days=1), location.standby_end)

    @property
    def kind(self):
        return self.kind_for(self.location, self.date)

    def get_kind_display(self):
        return OnCallKind(self.kind).label

    def interval(self):
        return self.interval_for(self.location, self.date)

    ROLE_FIELDS = ("staff_one", "staff_two", "support_staff", "phone_holder")

    @property
    def everyone(self):
        """Pair, third person and phone holder, without repeats."""
        seen, out = set(), []
        for f in self.ROLE_FIELDS:
            m = getattr(self, f)
            if m is not None and m.pk not in seen:
                seen.add(m.pk)
                out.append(m)
        return out

    def roles_of(self, staff):
        roles = []
        if staff.pk in (self.staff_one_id, self.staff_two_id):
            roles.append("team")
        if self.support_staff_id == staff.pk:
            roles.append(self.get_support_role_display().lower())
        if self.phone_holder_id == staff.pk:
            roles.append("has the phones")
        return roles

    def has_member(self, staff):
        return staff is not None and staff.pk in (self.staff_one_id, self.staff_two_id)

    def involves(self, staff):
        return staff is not None and staff.pk in {getattr(self, f + "_id") for f in self.ROLE_FIELDS}

    def partner_of(self, staff):
        if self.staff_one_id == staff.pk:
            return self.staff_two
        if self.staff_two_id == staff.pk:
            return self.staff_one
        return None


class GentleWayAttendance(models.Model):
    MAIN = "MAIN"
    CHECK = "CHECK"
    OTHER = "OTHER"
    PROCESS_CHOICES = [
        (MAIN, "Process (main attendance)"),
        (CHECK, "Short term system check"),
        (OTHER, "Other attendance"),
    ]
    PLANNED = "PLANNED"
    DONE = "DONE"
    CANCELLED = "CANCELLED"
    STATUS_CHOICES = [(PLANNED, "Planned"), (DONE, "Done"), (CANCELLED, "Cancelled")]

    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="gentle_way_attendances")
    date = models.DateField()
    start = models.TimeField()
    expected_minutes = models.PositiveSmallIntegerField(default=60)
    process_type = models.CharField(max_length=6, choices=PROCESS_CHOICES, default=MAIN)
    staff = models.ForeignKey(
        StaffMember,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="gentle_way_attendances",
        help_text="Any staff member marked Gentle Way trained.",
    )
    # Case reference only, never the deceased's name: the roster is seen by
    # every staff member and printed for the noticeboard.
    case_reference = models.CharField(
        max_length=40, blank=True, help_text="Case number only (no names), e.g. TVL-2027-014."
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PLANNED)
    note = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date", "start"]
        verbose_name = "Gentle Way attendance"

    def __str__(self):
        who = self.staff.display_name if self.staff else "unassigned"
        return f"{self.get_process_type_display()} {fmt_date(self.date)} {self.start:%H:%M} ({who})"

    def interval(self):
        s = aware(self.date, self.start)
        return s, s + timedelta(minutes=self.expected_minutes or 1)


class RequestStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    DECLINED = "DECLINED", "Declined"
    WITHDRAWN = "WITHDRAWN", "Withdrawn"


class UnavailabilityRequest(models.Model):
    """A staff member telling the roster they can't work.

    This controls rostering only. Paid leave is still applied for and
    recorded in Xero as now.
    """

    KIND_CHOICES = [
        ("ANNUAL", "Annual leave"),
        ("PERSONAL", "Personal / carer's leave"),
        ("UNAVAIL", "Not available"),
        ("OTHER", "Other"),
    ]

    staff = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name="unavailability")
    start_date = models.DateField()
    end_date = models.DateField()
    start_time = models.TimeField(null=True, blank=True, help_text="Leave blank for whole days.")
    end_time = models.TimeField(null=True, blank=True)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default="UNAVAIL")
    note = models.CharField(max_length=200, blank=True)

    status = models.CharField(max_length=10, choices=RequestStatus.choices, default=RequestStatus.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "unavailability request"

    def __str__(self):
        return f"{self.staff}: {self.get_kind_display()} {self.period_label}"

    @property
    def period_label(self):
        if self.start_time and self.end_time:
            return f"{fmt_date(self.start_date)} {self.start_time:%H:%M} to {fmt_date(self.end_date)} {self.end_time:%H:%M}"
        if self.start_date == self.end_date:
            return fmt_date(self.start_date)
        return f"{fmt_date(self.start_date)} to {fmt_date(self.end_date)}"

    def interval(self):
        if self.start_time and self.end_time:
            return aware(self.start_date, self.start_time), aware(self.end_date, self.end_time)
        from datetime import time as _t

        return aware(self.start_date, _t(0, 0)), aware(self.end_date + timedelta(days=1), _t(0, 0))


class SwapRequest(models.Model):
    """A staff member asking to hand one rostered item to someone else."""

    requester = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name="swap_requests")
    shift = models.ForeignKey(Shift, null=True, blank=True, on_delete=models.CASCADE)
    oncall = models.ForeignKey(OnCallNight, null=True, blank=True, on_delete=models.CASCADE)
    gentle_way = models.ForeignKey(GentleWayAttendance, null=True, blank=True, on_delete=models.CASCADE)
    proposed_staff = models.ForeignKey(
        StaffMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Who has agreed to take it, if anyone.",
    )
    reason = models.CharField(max_length=200, blank=True)

    status = models.CharField(max_length=10, choices=RequestStatus.choices, default=RequestStatus.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "swap request"

    def clean(self):
        if sum(x is not None for x in (self.shift_id, self.oncall_id, self.gentle_way_id)) != 1:
            raise ValidationError("A swap request must be for exactly one rostered item.")

    @property
    def item(self):
        return self.shift or self.oncall or self.gentle_way

    @property
    def location(self):
        return self.item.location

    @property
    def item_label(self):
        if self.shift:
            return f"Shift {fmt_date(self.shift.date)} {self.shift.time_label} ({self.shift.location})"
        if self.oncall:
            return f"After hours {fmt_date(self.oncall.date)} ({self.oncall.location}, {self.oncall.get_kind_display()})"
        g = self.gentle_way
        return f"Gentle Way {g.get_process_type_display().lower()} {fmt_date(g.date)} {g.start:%H:%M}"

    def __str__(self):
        return f"{self.requester}: {self.item_label}"


class ChangeLog(models.Model):
    at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=40)
    summary = models.TextField()

    class Meta:
        ordering = ["-at"]

    def __str__(self):
        return f"{self.at:%d/%m/%Y %H:%M} {self.action}: {self.summary[:60]}"
