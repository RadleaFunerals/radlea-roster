"""
Locations and staff.

These two models are deliberately kept outside the roster app. When the
roster is folded into the full FMS, Location becomes the FMS foundations
Location (Townsville, Ayr, any future site) and StaffMember becomes the FMS
staff record, so nothing in the roster needs rewriting.

Pay rates are NOT stored here. They live in the Staff Pay Master Register
and Xero. The roster only needs to know who can work where and doing what.
"""
from datetime import time

from django.conf import settings
from django.db import models


class Location(models.Model):
    code = models.CharField(
        max_length=10, unique=True, help_text="Short code used on case numbers, e.g. TVL, BUR."
    )
    name = models.CharField(max_length=120, help_text="Full trading name shown on printed rosters.")
    short_name = models.CharField(max_length=40, help_text="Name used on screens, e.g. Townsville, Ayr.")
    sort_order = models.PositiveSmallIntegerField(default=0)
    active = models.BooleanField(default=True)
    has_gentle_way = models.BooleanField(
        default=False,
        help_text="Tick if this site has a Gentle Way (alkaline hydrolysis) unit that needs attendances rostered.",
    )
    # After-hours standby windows. Configurable per site because Ayr and
    # Townsville may close at different times. [ASSUMPTION: confirm times]
    weeknight_standby_start = models.TimeField(
        default=time(17, 0), help_text="When the after-hours team takes over on a weekday."
    )
    weekend_standby_start = models.TimeField(
        default=time(8, 0),
        help_text="When the weekend / public holiday standby day starts.",
    )
    standby_end = models.TimeField(
        default=time(8, 0), help_text="When standby hands back to the day staff the next morning."
    )

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.short_name


class StaffMember(models.Model):
    FULL_TIME = "FT"
    PART_TIME = "PT"
    CASUAL = "CAS"
    DIRECTOR = "DIR"
    EMPLOYMENT_CHOICES = [
        (FULL_TIME, "Full-time"),
        (PART_TIME, "Part-time"),
        (CASUAL, "Casual"),
        (DIRECTOR, "Director"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="staff",
        help_text="The login this person uses. Leave blank if they don't log in.",
    )
    first_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    preferred_name = models.CharField(max_length=60, blank=True)
    role_title = models.CharField(max_length=120, blank=True)
    employment_type = models.CharField(max_length=3, choices=EMPLOYMENT_CHOICES, default=FULL_TIME)
    award_grade = models.CharField(
        max_length=40, blank=True, help_text="Funeral Industry Award 2020 grade, e.g. Grade 3."
    )
    start_date = models.DateField(null=True, blank=True)
    contracted_weekly_hours = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        null=True,
        blank=True,
        help_text="Ordinary hours per week (38 for full-time). Leave blank for casuals and directors.",
    )

    home_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="home_staff")
    extra_locations = models.ManyToManyField(
        Location,
        blank=True,
        related_name="visiting_staff",
        help_text="Other sites this person can be rostered at (e.g. casuals who cover Ayr and Townsville).",
    )

    mobile = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)

    on_roster = models.BooleanField(
        default=True, help_text="Untick for people who are never rostered (e.g. a non-operational director)."
    )
    can_do_transfers = models.BooleanField(
        default=True, help_text="Can be on the after-hours transfer team."
    )
    gentle_way_trained = models.BooleanField(
        default=False, help_text="Trained and authorised to attend Gentle Way processes and system checks."
    )
    editor_locations = models.ManyToManyField(
        Location,
        blank=True,
        related_name="roster_editors",
        help_text="Sites whose roster this person can build, publish and approve requests for.",
    )

    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["last_name", "first_name"]
        verbose_name = "staff member"

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        return f"{self.preferred_name or self.first_name} {self.last_name}"

    @property
    def short_name(self):
        return f"{self.preferred_name or self.first_name} {self.last_name[:1]}"

    @property
    def is_casual(self):
        return self.employment_type == self.CASUAL

    def can_work_at(self, location):
        if location is None:
            return False
        if self.home_location_id == location.pk:
            return True
        if self.pk is None:
            return False
        return self.extra_locations.filter(pk=location.pk).exists()
