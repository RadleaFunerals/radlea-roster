"""
Rostering rules.

Each check returns a list of Issues. An ERROR stops the change being saved;
a WARNING is shown to the person building the roster but allowed (they may
know something the system doesn't).

These rules are about who can be rostered, not pay. Pay is still calculated
from timesheets against the Award, the Staff Pay Master Register and Xero.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q

from .models import (
    GentleWayAttendance,
    OnCallNight,
    RequestStatus,
    Shift,
    UnavailabilityRequest,
)
from .timeutils import fmt_date, local_date, overlaps, week_days, week_start

ERROR = "error"
WARNING = "warning"

# Casual minimum engagement is 4 hours (Staff Pay Master Register, Award cl.11.3).
CASUAL_MIN_HOURS = Decimal("4")
# More consecutive after-hours nights than this gets a fatigue warning.
# [ASSUMPTION: confirm the limit Radlea wants]
MAX_CONSECUTIVE_ONCALL_NIGHTS = 7


@dataclass(frozen=True)
class Issue:
    level: str
    message: str
    # Issues with the same key are the same problem seen from different
    # items (e.g. weekly hours) and are shown once on the week check.
    key: str = ""

    @property
    def is_error(self):
        return self.level == ERROR


def errors(issues):
    return [i for i in issues if i.is_error]


def warnings(issues):
    return [i for i in issues if not i.is_error]


def _exclude_self(qs, obj):
    return qs.exclude(pk=obj.pk) if obj.pk else qs


# ---------------------------------------------------------------- shared checks

def _person_checks(staff, location):
    issues = []
    if not staff.active or not staff.on_roster:
        issues.append(Issue(ERROR, f"{staff} is not set up to be rostered (inactive or not on roster)."))
    if not staff.can_work_at(location):
        issues.append(
            Issue(
                ERROR,
                f"{staff} is not set up to work at {location}. "
                f"Add {location} to their locations in Staff if this is intended.",
            )
        )
    return issues


def _unavailability_issues(staff, start, end):
    issues = []
    candidates = UnavailabilityRequest.objects.filter(
        staff=staff,
        status__in=[RequestStatus.APPROVED, RequestStatus.PENDING],
        start_date__lte=local_date(end),
        end_date__gte=local_date(start) - timedelta(days=1),
    )
    for u in candidates:
        us, ue = u.interval()
        if not overlaps(start, end, us, ue):
            continue
        if u.status == RequestStatus.APPROVED:
            issues.append(Issue(ERROR, f"{staff} is unavailable: {u.get_kind_display().lower()}, {u.period_label}."))
        else:
            issues.append(
                Issue(WARNING, f"{staff} has a pending unavailability request ({u.period_label}). Decide it first.")
            )
    return issues


def _oncall_nights_for(staff):
    return OnCallNight.objects.filter(Q(staff_one=staff) | Q(staff_two=staff))


# ---------------------------------------------------------------------- shifts

def check_shift(shift):
    staff, location = shift.staff, shift.location
    if shift.start == shift.end:
        return [Issue(ERROR, "Start and finish times are the same.")]
    issues = _person_checks(staff, location)
    s, e = shift.interval()

    nearby = _exclude_self(
        Shift.objects.filter(staff=staff, date__range=(shift.date - timedelta(days=1), shift.date + timedelta(days=1))),
        shift,
    ).select_related("location")
    for other in nearby:
        os_, oe = other.interval()
        if overlaps(s, e, os_, oe):
            issues.append(
                Issue(
                    ERROR,
                    f"Overlaps {staff}'s shift on {fmt_date(other.date)} {other.time_label} at {other.location}.",
                )
            )

    issues += _unavailability_issues(staff, s, e)

    if staff.is_casual and shift.paid_hours < CASUAL_MIN_HOURS:
        issues.append(
            Issue(
                WARNING,
                f"{shift.paid_hours} paid hours is under the 4-hour casual minimum engagement; "
                "the timesheet will be topped up to 4 hours.",
            )
        )

    if staff.contracted_weekly_hours and not staff.is_casual:
        ws = week_start(shift.date)
        others = _exclude_self(Shift.objects.filter(staff=staff, date__range=(ws, ws + timedelta(days=6))), shift)
        total = sum((o.paid_hours for o in others), Decimal("0")) + shift.paid_hours
        if total > staff.contracted_weekly_hours:
            issues.append(
                Issue(
                    WARNING,
                    f"{staff} would be rostered {total} paid hours in the week starting {fmt_date(ws)}, "
                    f"above their {staff.contracted_weekly_hours} ordinary hours. Check overtime.",
                    key=f"hours:{staff.pk}:{ws.isoformat()}",
                )
            )
    return issues


# ---------------------------------------------------------------- after hours

def _consecutive_nights(staff, d, exclude_pk=None):
    """How many consecutive dates (incl. d) the person is on an after-hours team."""
    window = _oncall_nights_for(staff).filter(
        date__range=(d - timedelta(days=31), d + timedelta(days=31))
    )
    if exclude_pk:
        window = window.exclude(pk=exclude_pk)
    dates = set(window.values_list("date", flat=True))
    dates.add(d)
    run = 1
    x = d - timedelta(days=1)
    while x in dates:
        run += 1
        x -= timedelta(days=1)
    x = d + timedelta(days=1)
    while x in dates:
        run += 1
        x += timedelta(days=1)
    return run


def check_oncall(night):
    issues = []
    location = night.location
    if night.staff_one_id and night.staff_one_id == night.staff_two_id:
        issues.append(Issue(ERROR, "The same person is entered twice. The after-hours team is a pair."))
    members = {m.pk: m for m in night.members}.values()
    if not members:
        issues.append(Issue(WARNING, f"No after-hours team for {location} on {fmt_date(night.date)}."))
    elif len(night.members) < 2 or len(members) < 2:
        issues.append(Issue(WARNING, f"Only one person on the {location} after-hours team for {fmt_date(night.date)}."))

    s, e = night.interval()
    for m in members:
        issues += _person_checks(m, location)
        if not m.can_do_transfers:
            issues.append(Issue(ERROR, f"{m} is not marked as able to do after-hours transfers."))

        others = _exclude_self(
            _oncall_nights_for(m).filter(
                date__range=(night.date - timedelta(days=1), night.date + timedelta(days=1))
            ),
            night,
        ).select_related("location")
        for o in others:
            os_, oe = o.interval()
            if overlaps(s, e, os_, oe):
                issues.append(
                    Issue(ERROR, f"{m} is already on the {o.location} after-hours team for {fmt_date(o.date)}.")
                )

        issues += _unavailability_issues(m, s, e)

        run = _consecutive_nights(m, night.date, exclude_pk=night.pk)
        if run > MAX_CONSECUTIVE_ONCALL_NIGHTS:
            issues.append(
                Issue(WARNING, f"{m} would be on after hours {run} nights in a row. Check fatigue.")
            )
    return issues


# ---------------------------------------------------------------- Gentle Way

def check_gentle_way(att):
    issues = []
    if att.status == GentleWayAttendance.CANCELLED:
        return issues
    if att.staff is None:
        return [Issue(WARNING, f"Gentle Way attendance on {fmt_date(att.date)} {att.start:%H:%M} has no one assigned.")]
    staff = att.staff
    issues += _person_checks(staff, att.location)
    if not staff.gentle_way_trained:
        issues.append(Issue(ERROR, f"{staff} is not marked as Gentle Way trained."))

    s, e = att.interval()
    others = _exclude_self(
        GentleWayAttendance.objects.filter(
            staff=staff, date__range=(att.date - timedelta(days=1), att.date + timedelta(days=1))
        ).exclude(status=GentleWayAttendance.CANCELLED),
        att,
    )
    for o in others:
        os_, oe = o.interval()
        if overlaps(s, e, os_, oe):
            issues.append(Issue(ERROR, f"{staff} already has a Gentle Way attendance at {o.start:%H:%M} that day."))
    issues += _unavailability_issues(staff, s, e)
    return issues


# ---------------------------------------------------------------- whole week

def week_issues(location, ws):
    """All issues for a location's week, as (date, Issue) pairs, errors first."""
    days = week_days(ws)
    found = []
    nights = {n.date: n for n in OnCallNight.objects.filter(location=location, date__range=(days[0], days[-1]))
              .select_related("staff_one", "staff_two", "location")}
    for d in days:
        night = nights.get(d) or OnCallNight(location=location, date=d)
        for i in check_oncall(night):
            found.append((d, i))
    for sh in Shift.objects.filter(location=location, date__range=(days[0], days[-1])).select_related(
        "staff", "location"
    ):
        for i in check_shift(sh):
            if i.key:
                found.append((ws, i))
            else:
                found.append((sh.date, Issue(i.level, f"{sh.staff.short_name} {sh.time_label}: {i.message}")))
    for g in GentleWayAttendance.objects.filter(location=location, date__range=(days[0], days[-1])).select_related(
        "staff", "location"
    ):
        for i in check_gentle_way(g):
            found.append((g.date, i))

    # De-duplicate (an overlap is reported from both shifts) and sort.
    seen, unique = set(), []
    for d, i in found:
        key = i.key or (d, i.level, i.message)
        if key not in seen:
            seen.add(key)
            unique.append((d, i))
    unique.sort(key=lambda x: (0 if x[1].is_error else 1, x[0]))
    return unique
