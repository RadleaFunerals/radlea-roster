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


class Skill(models.Model):
    """Something a person can be called on to do, e.g. daytime transfers, mortuary work."""

    # Codes the roster rules rely on. Names and descriptions can be edited.
    AH_TRANSFER = "AH_TRANSFER"
    DAY_TRANSFER = "DAY_TRANSFER"
    MORTUARY = "MORTUARY"
    ARRANGING = "ARRANGING"
    CONDUCTING = "CONDUCTING"
    CELEBRANT = "CELEBRANT"
    GENTLE_WAY = "GENTLE_WAY"
    OFFICE = "OFFICE"

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=60)
    description = models.CharField(max_length=200, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Area(models.Model):
    """A business area a shift is worked in: arrangements, transfers, mortuary, etc.

    If an area has a required skill, only people with that skill can be
    rostered in it (trainees allowed with a warning).
    """

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=60)
    colour = models.CharField(max_length=7, default="#e3f1f2", help_text="Background colour on the roster, e.g. #e3f1f2.")
    required_skill = models.ForeignKey(
        Skill, null=True, blank=True, on_delete=models.SET_NULL, help_text="Leave blank if anyone can work in this area."
    )
    sort_order = models.PositiveSmallIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


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
    primary_area = models.ForeignKey(
        Area, null=True, blank=True, on_delete=models.SET_NULL, help_text="The area they mostly work in."
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

    def skill_level(self, code):
        """TRAINEE / COMPETENT / TRAINER, or None. Uses prefetched skills when available."""
        if self.pk is None:
            return None
        cache = getattr(self, "_skill_cache", None)
        if cache is None:
            cache = {s.skill.code: s.level for s in self.skills.select_related("skill")}
            self._skill_cache = cache
        return cache.get(code)

    def has_skill(self, code):
        return self.skill_level(code) is not None

    def can_work_at(self, location):
        if location is None:
            return False
        if self.home_location_id == location.pk:
            return True
        if self.pk is None:
            return False
        return self.extra_locations.filter(pk=location.pk).exists()


class StaffSkill(models.Model):
    TRAINEE = "TRAINEE"
    COMPETENT = "COMPETENT"
    TRAINER = "TRAINER"
    LEVEL_CHOICES = [(TRAINEE, "Trainee"), (COMPETENT, "Competent"), (TRAINER, "Trainer / supervisor")]

    staff = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name="skills")
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="staff_skills")
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default=COMPETENT)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = [("staff", "skill")]
        ordering = ["skill__sort_order"]

    def __str__(self):
        return f"{self.staff}: {self.skill} ({self.get_level_display()})"
