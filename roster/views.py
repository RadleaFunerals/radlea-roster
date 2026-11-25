from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from org.logins import create_login, reset_password
from org.models import Area, Location, Skill, StaffMember, StaffSkill

from . import rules
from .forms import (
    DecisionForm,
    GentleWayForm,
    OnCallForm,
    ShiftForm,
    SkillsMatrixForm,
    SwapForm,
    UnavailabilityForm,
)
from .models import (
    ChangeLog,
    GentleWayAttendance,
    OnCallKind,
    OnCallNight,
    PublicHoliday,
    RequestStatus,
    RosterWeek,
    Shift,
    ShiftPattern,
    SwapRequest,
    UnavailabilityRequest,
)
from .notify import email_week
from .permissions import can_edit, current_staff, editable_locations, require_editor
from .queues import swaps_for, unavailability_for
from .timeutils import fmt_date, overlaps, week_days, week_start

NIGHT_RELATED = ("location", "staff_one", "staff_two", "support_staff", "phone_holder")


# ------------------------------------------------------------------ helpers

def _location(code):
    return get_object_or_404(Location, code=code.upper(), active=True)


def _parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise Http404("Bad date")


def _week_url(d, request=None):
    url = reverse("roster:week", args=[week_start(d).isoformat()])
    if request is not None:
        keep = {k: request.GET.get(k) for k in ("loc", "area") if request.GET.get(k)}
        if keep:
            url += "?" + "&".join(f"{k}={v}" for k, v in keep.items())
    return url


def _log(request, location, action, summary):
    ChangeLog.objects.create(user=request.user, location=location, action=action, summary=summary)


def _show_warnings(request, form):
    for w in getattr(form, "rule_warnings", []):
        messages.warning(request, w)


def _published_set(location_ids, start, end):
    return set(
        RosterWeek.objects.filter(
            location_id__in=location_ids,
            week_start__range=(week_start(start), end),
            published_at__isnull=False,
        ).values_list("location_id", "week_start")
    )


def _night_people(n):
    """For templates: [(label, person)] for the pair, third person and phones."""
    out = [("", m) for m in n.members]
    if n.support_staff:
        out.append((n.get_support_role_display(), n.support_staff))
    return out


# --------------------------------------------------------- combined roster

def _week_context(user, ws, loc_filter=None, area_filter=None):
    """Both sites on one page. Each site's part is shown if it is published
    or the user can edit it."""
    days = week_days(ws)
    end = days[-1]
    all_locations = list(Location.objects.filter(active=True))
    shown_locations = [l for l in all_locations if not loc_filter or l.code == loc_filter]
    editable_ids = set(editable_locations(user).values_list("pk", flat=True))
    weeks = {rw.location_id: rw for rw in RosterWeek.objects.filter(week_start=ws)}

    sites = []
    for loc in all_locations:
        rw = weeks.get(loc.pk)
        sites.append(
            {
                "location": loc,
                "editor": loc.pk in editable_ids,
                "published": bool(rw and rw.published_at),
                "published_at": rw.published_at if rw else None,
            }
        )
    visible_ids = {s["location"].pk for s in sites if s["editor"] or s["published"]}
    shown_ids = {l.pk for l in shown_locations} & visible_ids
    pub_at = {s["location"].pk: s["published_at"] for s in sites}

    shifts = list(
        Shift.objects.filter(location_id__in=shown_ids, date__range=(ws, end)).select_related(
            "staff", "location", "area"
        )
    )
    if area_filter:
        shifts = [sh for sh in shifts if sh.area.code == area_filter]
    for sh in shifts:
        pa = pub_at.get(sh.location_id)
        sh.changed = bool(pa and sh.updated_at > pa)
        sh.editable = sh.location_id in editable_ids

    # People: everyone who can work at a shown site, plus anyone with a shift shown.
    people = {
        s.pk: s
        for s in StaffMember.objects.filter(active=True, on_roster=True)
        .filter(Q(home_location_id__in=shown_ids) | Q(extra_locations__in=shown_ids))
        .select_related("home_location", "primary_area")
        .distinct()
    }
    for sh in shifts:
        people.setdefault(sh.staff_id, sh.staff)
    if area_filter:
        with_shift = {sh.staff_id for sh in shifts}
        people = {k: v for k, v in people.items() if k in with_shift or (v.primary_area and v.primary_area.code == area_filter)}

    leave = {}
    for u in UnavailabilityRequest.objects.filter(
        staff_id__in=people.keys(), status=RequestStatus.APPROVED, start_date__lte=end, end_date__gte=ws
    ):
        for d in days:
            if u.start_date <= d <= u.end_date:
                leave[(u.staff_id, d)] = u

    nights = list(
        OnCallNight.objects.filter(location_id__in=shown_ids, date__range=(ws, end)).select_related(*NIGHT_RELATED)
    )
    ah_count = {}
    for n in nights:
        for p in n.everyone:
            ah_count[p.pk] = ah_count.get(p.pk, 0) + 1

    groups = []
    for loc in all_locations:
        members = sorted(
            [p for p in people.values() if p.home_location_id == loc.pk],
            key=lambda s: (s.is_casual, s.employment_type == StaffMember.DIRECTOR, s.last_name, s.first_name),
        )
        if not members:
            continue
        rows = []
        for s in members:
            cells, total = [], 0
            for d in days:
                day_shifts = [sh for sh in shifts if sh.staff_id == s.pk and sh.date == d]
                total += sum(sh.paid_hours for sh in day_shifts)
                cells.append({"date": d, "shifts": day_shifts, "leave": leave.get((s.pk, d))})
            rows.append({"staff": s, "cells": cells, "hours": total, "ah": ah_count.get(s.pk, 0)})
        groups.append({"location": loc, "rows": rows})

    oncall_rows = []
    for loc in shown_locations:
        if loc.pk not in shown_ids:
            continue
        by_date = {n.date: n for n in nights if n.location_id == loc.pk}
        cells = []
        for d in days:
            n = by_date.get(d)
            s, e = OnCallNight.interval_for(loc, d)
            cells.append(
                {
                    "date": d,
                    "night": n,
                    "people": _night_people(n) if n else [],
                    "kind": OnCallNight.kind_for(loc, d),
                    "start": timezone.localtime(s),
                    "end": timezone.localtime(e),
                    "changed": bool(n and pub_at.get(loc.pk) and n.updated_at > pub_at[loc.pk]),
                }
            )
        oncall_rows.append({"location": loc, "editor": loc.pk in editable_ids, "cells": cells})

    gentle_rows = []
    for loc in shown_locations:
        if not loc.has_gentle_way or loc.pk not in shown_ids:
            continue
        atts = list(
            GentleWayAttendance.objects.filter(location=loc, date__range=(ws, end)).select_related("staff")
        )
        gentle_rows.append(
            {
                "location": loc,
                "editor": loc.pk in editable_ids,
                "cells": [{"date": d, "items": [a for a in atts if a.date == d]} for d in days],
            }
        )

    holidays = {}
    for h in PublicHoliday.objects.filter(date__range=(ws, end)).select_related("location"):
        holidays.setdefault(h.date, []).append(h)
    day_info = [
        {
            "date": d,
            "holidays": holidays.get(d, []),
            "full_ph": any(h.part_day_from is None and h.location_id is None for h in holidays.get(d, [])),
            "weekend": d.weekday() >= 5,
        }
        for d in days
    ]

    issues = []
    for loc in all_locations:
        if loc.pk in editable_ids and loc.pk in {l.pk for l in shown_locations}:
            for d, i in rules.week_issues(loc, ws):
                issues.append((d, rules.Issue(i.level, f"{loc.short_name}: {i.message}", i.key)))
    issues.sort(key=lambda x: (0 if x[1].is_error else 1, x[0]))

    hidden = [s["location"] for s in sites if s["location"].pk not in visible_ids]
    return {
        "ws": ws,
        "days": day_info,
        "groups": groups,
        "oncall_rows": oncall_rows,
        "gentle_rows": gentle_rows,
        "sites": sites,
        "hidden_sites": hidden,
        "issues": issues,
        "areas": Area.objects.filter(active=True),
        "locations": all_locations,
        "loc_filter": loc_filter or "",
        "area_filter": area_filter or "",
        "any_editor": bool(editable_ids),
        "ncols": 9,
    }


@login_required
def home(request):
    if editable_locations(request.user).exists():
        return redirect("roster:week_current")
    return redirect("roster:my_roster")


@login_required
def week_view(request, start=None):
    ws = week_start(_parse_date(start)) if start else week_start(timezone.localdate())
    ctx = _week_context(request.user, ws, request.GET.get("loc"), request.GET.get("area"))
    qs = request.GET.urlencode()
    ctx.update(
        {
            "prev_week": ws - timedelta(days=7),
            "next_week": ws + timedelta(days=7),
            "this_week": week_start(timezone.localdate()),
            "qs": f"?{qs}" if qs else "",
        }
    )
    return render(request, "roster/week.html", ctx)


@login_required
def print_week(request, start):
    ws = week_start(_parse_date(start))
    ctx = _week_context(request.user, ws, request.GET.get("loc"), request.GET.get("area"))
    ctx["printed_at"] = timezone.localtime()
    return render(request, "roster/print_week.html", ctx)


@login_required
def tonight(request):
    """Who is on after hours right now (or next), per site, plus today's shifts."""
    now = timezone.now()
    today = timezone.localdate()
    editable_ids = set(editable_locations(request.user).values_list("pk", flat=True))
    locations = list(Location.objects.filter(active=True))
    published = _published_set([l.pk for l in locations], today - timedelta(days=1), today + timedelta(days=1))
    boards = []
    for loc in locations:
        def visible(d, loc=loc):
            return loc.pk in editable_ids or (loc.pk, week_start(d)) in published

        current, upcoming = None, None
        for d in (today - timedelta(days=1), today, today + timedelta(days=1)):
            if not visible(d):
                continue
            s, e = OnCallNight.interval_for(loc, d)
            night = OnCallNight.objects.filter(location=loc, date=d).select_related(*NIGHT_RELATED).first()
            entry = {
                "date": d,
                "night": night,
                "kind": OnCallNight.kind_for(loc, d),
                "start": timezone.localtime(s),
                "end": timezone.localtime(e),
            }
            if s <= now < e:
                current = entry
            elif s > now and upcoming is None:
                upcoming = entry
        shifts = (
            Shift.objects.filter(date=today, location=loc).select_related("staff", "area") if visible(today) else []
        )
        gw = []
        if loc.has_gentle_way:
            gw = [
                g
                for g in GentleWayAttendance.objects.filter(location=loc, date__range=(today, today + timedelta(days=1)))
                .exclude(status=GentleWayAttendance.CANCELLED)
                .select_related("staff")
                if visible(g.date)
            ]
        boards.append(
            {
                "location": loc,
                "current": current,
                "upcoming": upcoming,
                "shifts": shifts,
                "gentle": gw,
                "unpublished": not (visible(today - timedelta(days=1)) or visible(today)),
            }
        )
    return render(request, "roster/tonight.html", {"boards": boards, "now": timezone.localtime(now)})


def _my_items(staff, user, start, end):
    """A person's rostered items between two dates that they are allowed to see."""
    editable_ids = set(editable_locations(user).values_list("pk", flat=True))
    loc_ids = list(Location.objects.values_list("pk", flat=True))
    published = _published_set(loc_ids, start, end)
    pub_times = {
        (rw.location_id, rw.week_start): rw.published_at
        for rw in RosterWeek.objects.filter(week_start__range=(week_start(start), end), published_at__isnull=False)
    }

    def visible(loc_id, d):
        return loc_id in editable_ids or (loc_id, week_start(d)) in published

    def flags(loc_id, d, updated):
        pub = pub_times.get((loc_id, week_start(d)))
        return {"draft": pub is None, "changed": bool(pub and updated and updated > pub)}

    items = []
    for sh in Shift.objects.filter(staff=staff, date__range=(start, end)).select_related("location", "area"):
        if visible(sh.location_id, sh.date):
            s, e = sh.interval()
            items.append(
                {
                    "sort": s,
                    "ends": e,
                    "date": sh.date,
                    "what": sh.area.name,
                    "colour": sh.area.colour,
                    "when": sh.time_label,
                    "where": sh.location,
                    "detail": sh.note,
                    "ref": f"shift:{sh.pk}",
                    "label": f"Shift {fmt_date(sh.date)} {sh.time_label} ({sh.location})",
                    **flags(sh.location_id, sh.date, sh.updated_at),
                }
            )
    q = Q(staff_one=staff) | Q(staff_two=staff) | Q(support_staff=staff) | Q(phone_holder=staff)
    for n in OnCallNight.objects.filter(q, date__range=(start, end)).select_related(*NIGHT_RELATED):
        if not visible(n.location_id, n.date):
            continue
        s, e = n.interval()
        roles = n.roles_of(staff)
        others = [p for p in n.members if p.pk != staff.pk]
        bits = []
        if others:
            bits.append("With " + " and ".join(p.display_name + (f" ({p.mobile})" if p.mobile else "") for p in others))
        if n.support_staff and n.support_staff.pk != staff.pk:
            bits.append(f"{n.get_support_role_display()}: {n.support_staff.display_name}")
        if n.phone_holder and n.phone_holder.pk != staff.pk:
            bits.append(f"Phones: {n.phone_holder.display_name}")
        what = "After hours"
        if roles and roles != ["team"]:
            what += " - " + ", ".join(r for r in roles if r != "team")
        items.append(
            {
                "sort": s,
                "ends": e,
                "date": n.date,
                "what": f"{what} ({n.get_kind_display().lower()})",
                "colour": "#e6e9f4",
                "when": f"{timezone.localtime(s):%H:%M} to {timezone.localtime(e):%H:%M %a}",
                "where": n.location,
                "detail": " · ".join(bits),
                "ref": f"oncall:{n.pk}",
                "label": f"After hours {fmt_date(n.date)} ({n.location})",
                **flags(n.location_id, n.date, n.updated_at),
            }
        )
    for g in GentleWayAttendance.objects.filter(staff=staff, date__range=(start, end)).exclude(
        status=GentleWayAttendance.CANCELLED
    ).select_related("location"):
        if visible(g.location_id, g.date):
            s, e = g.interval()
            items.append(
                {
                    "sort": s,
                    "ends": e,
                    "date": g.date,
                    "what": f"Gentle Way: {g.get_process_type_display()}",
                    "colour": "#efe6f4",
                    "when": f"{g.start:%H:%M} (about {g.expected_minutes} min)",
                    "where": g.location,
                    "detail": g.case_reference,
                    "ref": f"gw:{g.pk}",
                    "label": f"Gentle Way {fmt_date(g.date)} {g.start:%H:%M}",
                    **flags(g.location_id, g.date, g.updated_at),
                }
            )
    now = timezone.now()
    items = [i for i in items if i["ends"] > now]  # drop anything already finished
    items.sort(key=lambda x: x["sort"])
    return items


@login_required
def my_roster(request):
    staff = current_staff(request.user)
    today = timezone.localdate()
    ctx = {"staff": staff, "items": [], "requests": [], "swaps": [], "skills": []}
    if staff is not None:
        # Start from yesterday so an after-hours shift still running this morning shows.
        ctx["items"] = _my_items(staff, request.user, today - timedelta(days=1), today + timedelta(days=27))
        ctx["requests"] = staff.unavailability.all()[:10]
        ctx["swaps"] = staff.swap_requests.select_related(
            "shift__location", "oncall__location", "gentle_way__location", "proposed_staff"
        )[:10]
        ctx["skills"] = staff.skills.select_related("skill")
    return render(request, "roster/my_roster.html", ctx)


# ------------------------------------------------------------ skills

@login_required
def skills(request):
    """Who can be called on for what. Everyone can see it; directors edit it."""
    people = list(
        StaffMember.objects.filter(active=True, on_roster=True)
        .select_related("home_location", "primary_area")
        .order_by("home_location__sort_order", "employment_type", "last_name")
    )
    all_skills = list(Skill.objects.all())
    current = {(ss.staff_id, ss.skill_id): ss.level for ss in StaffSkill.objects.all()}
    editing = request.user.is_superuser and (request.GET.get("edit") == "1" or request.method == "POST")
    if request.method == "POST":
        if not request.user.is_superuser:
            raise PermissionDenied("Directors only.")
        form = SkillsMatrixForm(request.POST, people=people, skills=all_skills, current=current)
        if form.is_valid():
            changes = []
            for p in people:
                for sk in all_skills:
                    new = form.cleaned_data.get(f"s{p.pk}_{sk.pk}") or ""
                    old = current.get((p.pk, sk.pk), "")
                    if new == old:
                        continue
                    if new:
                        StaffSkill.objects.update_or_create(staff=p, skill=sk, defaults={"level": new})
                    else:
                        StaffSkill.objects.filter(staff=p, skill=sk).delete()
                    changes.append(f"{p.display_name} - {sk.name}: {old or 'none'} -> {new or 'none'}")
            if changes:
                _log(request, None, "skills changed", "; ".join(changes))
            messages.success(request, f"Skills saved ({len(changes)} change(s)).")
            return redirect("roster:skills")
    form = SkillsMatrixForm(people=people, skills=all_skills, current=current) if editing else None
    rows = []
    for p in people:
        cells = []
        for sk in all_skills:
            cells.append(
                {
                    "level": current.get((p.pk, sk.pk), ""),
                    "field": form[f"s{p.pk}_{sk.pk}"] if form else None,
                }
            )
        rows.append({"staff": p, "cells": cells})
    return render(
        request,
        "roster/skills.html",
        {"rows": rows, "skills": all_skills, "editing": editing, "form": form, "areas": Area.objects.filter(active=True)},
    )


# ------------------------------------------------------------ reports

@login_required
def after_hours_report(request):
    """How the after-hours load is spread across people (fairness check)."""
    if not editable_locations(request.user).exists():
        raise PermissionDenied("Roster editors only.")
    try:
        weeks = max(1, min(int(request.GET.get("weeks", 8)), 52))
    except ValueError:
        weeks = 8
    end = week_start(timezone.localdate()) + timedelta(days=6)
    start = end - timedelta(days=7 * weeks - 1)
    stats = {}

    def row(p):
        return stats.setdefault(
            p.pk, {"staff": p, "WN": 0, "WE": 0, "PH": 0, "third": 0, "phones": 0, "total": 0}
        )

    for n in OnCallNight.objects.filter(date__range=(start, end)).select_related(*NIGHT_RELATED):
        kind = n.kind
        for m in {m.pk: m for m in n.members}.values():
            r = row(m)
            r[kind] += 1
            r[n.location.code] = r.get(n.location.code, 0) + 1
            r["total"] += 1
        if n.support_staff:
            row(n.support_staff)["third"] += 1
        if n.phone_holder:
            row(n.phone_holder)["phones"] += 1
        # Total = nights the person was involved at all (in any role), counted once.
        team_ids = {m.pk for m in n.members}
        for p in n.everyone:
            if p.pk not in team_ids:
                row(p)["total"] += 1
    locations = list(Location.objects.filter(active=True))
    rows = sorted(stats.values(), key=lambda r: (-r["total"], r["staff"].last_name))
    for r in rows:
        r["by_loc"] = [r.get(l.code, 0) for l in locations]
    return render(
        request,
        "roster/after_hours_report.html",
        {"rows": rows, "weeks": weeks, "start": start, "end": end, "locations": locations},
    )


# -------------------------------------------------------- editing: shifts

@login_required
def shift_create(request):
    editable = editable_locations(request.user)
    if not editable.exists():
        raise PermissionDenied("You can't edit the roster.")
    initial = {"unpaid_break_minutes": 30}
    if request.GET.get("date"):
        initial["date"] = _parse_date(request.GET["date"])
    staff = None
    if request.GET.get("staff"):
        staff = StaffMember.objects.filter(pk=request.GET["staff"]).select_related("home_location", "primary_area").first()
        if staff:
            initial["staff"] = staff.pk
            if staff.primary_area_id:
                initial["area"] = staff.primary_area_id
    loc = Location.objects.filter(code=(request.GET.get("loc") or "").upper()).first() or (
        staff.home_location if staff else None
    )
    if loc and editable.filter(pk=loc.pk).exists():
        initial["location"] = loc.pk
    form = ShiftForm(request.POST or None, initial=initial, editable=editable)
    if request.method == "POST" and form.is_valid():
        shift = form.save(commit=False)
        shift.created_by = request.user
        shift.save()
        _log(request, shift.location, "shift added", str(shift))
        _show_warnings(request, form)
        messages.success(request, f"Shift added: {shift}.")
        return redirect(_week_url(shift.date))
    return render(request, "roster/form.html", {"form": form, "title": "Add shift"})


@login_required
def shift_edit(request, pk):
    shift = get_object_or_404(Shift.objects.select_related("location", "staff"), pk=pk)
    require_editor(request.user, shift.location)
    before = str(shift)
    form = ShiftForm(request.POST or None, instance=shift, editable=editable_locations(request.user))
    if request.method == "POST" and form.is_valid():
        shift = form.save()
        _log(request, shift.location, "shift changed", f"{before} -> {shift}")
        _show_warnings(request, form)
        messages.success(request, "Shift updated.")
        return redirect(_week_url(shift.date))
    return render(
        request,
        "roster/form.html",
        {"form": form, "title": "Edit shift", "delete_url": reverse("roster:shift_delete", args=[shift.pk])},
    )


@login_required
@require_POST
def shift_delete(request, pk):
    shift = get_object_or_404(Shift, pk=pk)
    require_editor(request.user, shift.location)
    location, d, label = shift.location, shift.date, str(shift)
    shift.delete()
    _log(request, location, "shift removed", label)
    messages.success(request, f"Removed: {label}.")
    return redirect(_week_url(d))


# ---------------------------------------------------- editing: after hours

@login_required
def oncall_edit(request, code, day):
    location = _location(code)
    require_editor(request.user, location)
    d = _parse_date(day)
    night = OnCallNight.objects.filter(location=location, date=d).first() or OnCallNight(location=location, date=d)
    before = str(night) if night.pk else None
    form = OnCallForm(request.POST or None, instance=night, location=location)
    s, e = OnCallNight.interval_for(location, d)
    if request.method == "POST" and form.is_valid():
        night = form.save(commit=False)
        if not any(getattr(night, f) for f in OnCallNight.ROLE_FIELDS):
            if night.pk:
                night.delete()
                _log(request, location, "after hours cleared", before)
            messages.info(request, f"After-hours team for {fmt_date(d)} cleared.")
            return redirect(_week_url(d))
        night.save()
        _log(request, location, "after hours set", f"{before} -> {night}" if before else str(night))
        _show_warnings(request, form)
        messages.success(request, f"After-hours team saved: {night}.")
        return redirect(_week_url(d))
    title = (
        f"After hours - {location} - {d:%a} {fmt_date(d)} "
        f"({OnCallNight.kind_for(location, d).label}, {timezone.localtime(s):%H:%M} to "
        f"{timezone.localtime(e):%H:%M %a})"
    )
    return render(request, "roster/form.html", {"form": form, "title": title})


@login_required
@require_POST
def copy_oncall(request, start, code):
    """Copy last week's after-hours teams (pair, third person, phones) into empty places."""
    location = _location(code)
    require_editor(request.user, location)
    ws = week_start(_parse_date(start))
    prev = {
        n.date: n
        for n in OnCallNight.objects.filter(location=location, date__range=(ws - timedelta(days=7), ws - timedelta(days=1)))
    }
    copied, skipped = 0, []
    for d in week_days(ws):
        src = prev.get(d - timedelta(days=7))
        if not src:
            continue
        night = OnCallNight.objects.filter(location=location, date=d).first() or OnCallNight(location=location, date=d)
        changed = False
        for field in OnCallNight.ROLE_FIELDS:
            if getattr(night, field) is None and getattr(src, field) is not None:
                person = getattr(src, field)
                if field in ("staff_one", "staff_two") and night.has_member(person):
                    continue
                setattr(night, field, person)
                changed = True
        if night.support_staff_id == src.support_staff_id:
            night.support_role = src.support_role
        if not changed:
            continue
        errs = rules.errors(rules.check_oncall(night))
        if errs:
            skipped.append(f"{d:%a} {fmt_date(d)}: {errs[0].message}")
            continue
        night.save()
        copied += 1
    _log(request, location, "after hours copied", f"Week of {fmt_date(ws)}: {copied} nights copied")
    messages.success(request, f"{location}: copied last week's after-hours team into {copied} night(s).")
    for s in skipped:
        messages.warning(request, f"Not copied - {s}")
    return redirect(_week_url(ws))


# ------------------------------------------------- editing: Gentle Way

@login_required
def gw_create(request, code):
    location = _location(code)
    require_editor(request.user, location)
    if not location.has_gentle_way:
        raise Http404("No Gentle Way unit at this location.")
    initial = {"date": _parse_date(request.GET["date"])} if request.GET.get("date") else {}
    form = GentleWayForm(request.POST or None, location=location, initial=initial)
    if request.method == "POST" and form.is_valid():
        att = form.save(commit=False)
        att.location = location
        att.save()
        _log(request, location, "Gentle Way added", str(att))
        _show_warnings(request, form)
        messages.success(request, f"Gentle Way attendance added: {att}.")
        return redirect(_week_url(att.date))
    return render(request, "roster/form.html", {"form": form, "title": f"Add Gentle Way attendance - {location}"})


@login_required
def gw_edit(request, pk):
    att = get_object_or_404(GentleWayAttendance.objects.select_related("location"), pk=pk)
    location = att.location
    require_editor(request.user, location)
    before = str(att)
    form = GentleWayForm(request.POST or None, instance=att, location=location)
    if request.method == "POST" and form.is_valid():
        att = form.save()
        _log(request, location, "Gentle Way changed", f"{before} -> {att}")
        _show_warnings(request, form)
        messages.success(request, "Gentle Way attendance updated.")
        return redirect(_week_url(att.date))
    return render(
        request,
        "roster/form.html",
        {
            "form": form,
            "title": f"Edit Gentle Way attendance - {location}",
            "delete_url": reverse("roster:gw_delete", args=[att.pk]),
        },
    )


@login_required
@require_POST
def gw_delete(request, pk):
    att = get_object_or_404(GentleWayAttendance, pk=pk)
    require_editor(request.user, att.location)
    location, d, label = att.location, att.date, str(att)
    att.delete()
    _log(request, location, "Gentle Way removed", label)
    messages.success(request, f"Removed: {label}.")
    return redirect(_week_url(d))


# ---------------------------------------------------- week-level actions

@login_required
@require_POST
def fill_usual(request, start, code):
    """Fill one site's week from each person's usual shifts, skipping anything that breaks a rule."""
    location = _location(code)
    require_editor(request.user, location)
    ws = week_start(_parse_date(start))
    created, skipped = 0, []
    patterns = ShiftPattern.objects.filter(location=location, staff__active=True, staff__on_roster=True).select_related(
        "staff", "area"
    )
    for p in patterns:
        d = ws + timedelta(days=p.weekday)
        if PublicHoliday.is_full_day(d, location):
            skipped.append(f"{p.staff.short_name} {d:%a}: public holiday")
            continue
        if Shift.objects.filter(staff=p.staff, date=d).exists():
            continue
        shift = Shift(
            staff=p.staff,
            location=location,
            date=d,
            start=p.start,
            end=p.end,
            unpaid_break_minutes=p.unpaid_break_minutes,
            area=p.area,
            created_by=request.user,
        )
        errs = rules.errors(rules.check_shift(shift))
        if errs:
            skipped.append(f"{p.staff.short_name} {d:%a}: {errs[0].message}")
            continue
        shift.save()
        created += 1
    _log(request, location, "week filled", f"Week of {fmt_date(ws)}: {created} usual shifts added")
    messages.success(request, f"{location}: added {created} usual shift(s).")
    for s in skipped:
        messages.warning(request, f"Skipped - {s}")
    return redirect(_week_url(ws))


@login_required
@require_POST
def publish_week(request, start, code):
    location = _location(code)
    require_editor(request.user, location)
    ws = week_start(_parse_date(start))
    errs = [i for _, i in rules.week_issues(location, ws) if i.is_error]
    if errs and request.POST.get("override") != "1":
        messages.error(
            request,
            f"{location} has {len(errs)} roster error(s) this week. Fix them, or tick 'Publish anyway'.",
        )
        return redirect(_week_url(ws))
    rw, _ = RosterWeek.objects.get_or_create(location=location, week_start=ws)
    first = rw.published_at is None
    rw.published_at = timezone.now()
    rw.published_by = request.user
    rw.save()
    _log(request, location, "published" if first else "republished", f"Week of {fmt_date(ws)}")
    messages.success(request, f"{location} roster for the week of {fmt_date(ws)} is published. Staff can now see it.")
    sent, missing, failed = email_week(location, ws, republished=not first)
    if sent:
        messages.info(request, f"Emailed {sent} rostered staff member(s) their week.")
    if missing:
        messages.warning(request, "No email address in Staff for: " + ", ".join(missing) + ". They weren't emailed.")
    if failed:
        messages.warning(request, "Email failed for: " + ", ".join(failed) + ". Check the mail settings.")
    return redirect(_week_url(ws))


# ------------------------------------------------------------ staff logins

@login_required
def staff_logins(request):
    """Directors create logins and reset passwords from a phone. A temporary
    password is shown once on screen to pass on to the staff member."""
    if not request.user.is_superuser:
        raise PermissionDenied("Directors only.")
    shown = None
    if request.method == "POST":
        staff = get_object_or_404(StaffMember, pk=request.POST.get("staff"))
        if request.POST.get("action") == "create" and staff.user is None:
            username, password = create_login(staff)
            _log(request, staff.home_location, "login created", username)
        elif request.POST.get("action") == "reset" and staff.user is not None:
            if staff.user_id == request.user.pk:
                messages.error(request, "Change your own password from Setup > Change password.")
                return redirect("roster:staff_logins")
            username, password = reset_password(staff)
            _log(request, staff.home_location, "password reset", username)
        else:
            return redirect("roster:staff_logins")
        shown = {"staff": staff, "username": username, "password": password}
    people = StaffMember.objects.filter(active=True).select_related("user", "home_location").order_by(
        "home_location__sort_order", "last_name"
    )
    return render(request, "roster/staff_logins.html", {"people": people, "shown": shown})


# ------------------------------------------------------------ requests

@login_required
def unavailability_new(request):
    staff = current_staff(request.user)
    if staff is None:
        messages.error(request, "Your login isn't linked to a staff record. Ask a director to link it.")
        return redirect("roster:my_roster")
    form = UnavailabilityForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        req = form.save(commit=False)
        req.staff = staff
        req.save()
        _log(request, staff.home_location, "unavailability requested", str(req))
        messages.success(request, "Request sent to your manager.")
        return redirect("roster:my_roster")
    return render(request, "roster/form.html", {"form": form, "title": "I'm not available"})


@login_required
def swap_new(request):
    staff = current_staff(request.user)
    if staff is None:
        messages.error(request, "Your login isn't linked to a staff record. Ask a director to link it.")
        return redirect("roster:my_roster")
    today = timezone.localdate()
    items = _my_items(staff, request.user, today - timedelta(days=1), today + timedelta(days=60))
    choices = [(i["ref"], i["label"]) for i in items]
    initial = {"item": request.GET["item"]} if request.GET.get("item") else {}
    form = SwapForm(request.POST or None, choices=choices, staff=staff, initial=initial)
    if request.method == "POST" and form.is_valid():
        kind, pk = form.cleaned_data["item"].split(":")
        req = SwapRequest(
            requester=staff,
            proposed_staff=form.cleaned_data["proposed_staff"],
            reason=form.cleaned_data["reason"],
        )
        if kind == "shift":
            req.shift = get_object_or_404(Shift, pk=pk, staff=staff)
        elif kind == "oncall":
            req.oncall = get_object_or_404(
                OnCallNight.objects.filter(
                    Q(staff_one=staff) | Q(staff_two=staff) | Q(support_staff=staff) | Q(phone_holder=staff)
                ),
                pk=pk,
            )
        elif kind == "gw":
            req.gentle_way = get_object_or_404(GentleWayAttendance, pk=pk, staff=staff)
        else:
            raise Http404
        req.full_clean()
        req.save()
        _log(request, req.location, "swap requested", str(req))
        messages.success(request, "Swap request sent to your manager.")
        return redirect("roster:my_roster")
    return render(
        request,
        "roster/form.html",
        {
            "form": form,
            "title": "Swap or give away a shift",
            "empty_note": None if choices else "You have nothing rostered in the next 60 days.",
        },
    )


@login_required
@require_POST
def withdraw_request(request, kind, pk):
    staff = current_staff(request.user)
    model = {"unavailability": UnavailabilityRequest, "swap": SwapRequest}.get(kind)
    if model is None or staff is None:
        raise Http404
    owner_field = "staff" if model is UnavailabilityRequest else "requester"
    req = get_object_or_404(model, pk=pk, status=RequestStatus.PENDING, **{owner_field: staff})
    req.status = RequestStatus.WITHDRAWN
    req.save()
    messages.info(request, "Request withdrawn.")
    return redirect("roster:my_roster")


@login_required
def requests_queue(request):
    editable = list(editable_locations(request.user))
    if not editable:
        return redirect("roster:my_roster")
    ids = [l.pk for l in editable]
    recent_unavail = unavailability_for(ids, status=None).exclude(status=RequestStatus.PENDING)[:15]
    recent_swaps = [s for s in swaps_for(ids, status=None) if s.status != RequestStatus.PENDING][:15]
    return render(
        request,
        "roster/requests.html",
        {
            "unavailability": unavailability_for(ids),
            "swaps": swaps_for(ids),
            "recent_unavailability": recent_unavail,
            "recent_swaps": recent_swaps,
            "decision_form": DecisionForm(),
        },
    )


def _can_decide_unavailability(user, req):
    locs = [req.staff.home_location, *req.staff.extra_locations.all()]
    return any(can_edit(user, l) for l in locs)


@login_required
@require_POST
def decide_unavailability(request, pk):
    req = get_object_or_404(UnavailabilityRequest.objects.select_related("staff"), pk=pk, status=RequestStatus.PENDING)
    if not _can_decide_unavailability(request.user, req):
        raise Http404
    form = DecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose approve or decline.")
        return redirect("roster:requests")
    approve = form.cleaned_data["action"] == "approve"
    req.status = RequestStatus.APPROVED if approve else RequestStatus.DECLINED
    req.decided_by = request.user
    req.decided_at = timezone.now()
    req.decision_note = form.cleaned_data["note"]
    req.save()
    _log(request, req.staff.home_location, "unavailability " + req.status.lower(), str(req))
    messages.success(request, f"{req.get_status_display()}: {req}.")
    if approve:
        # Tell the manager about anything already rostered in that time.
        s, e = req.interval()
        clashes = []
        for sh in Shift.objects.filter(staff=req.staff, date__range=(req.start_date - timedelta(days=1), req.end_date)):
            if overlaps(s, e, *sh.interval()):
                clashes.append(str(sh))
        q = Q(staff_one=req.staff) | Q(staff_two=req.staff) | Q(support_staff=req.staff) | Q(phone_holder=req.staff)
        for n in OnCallNight.objects.filter(q, date__range=(req.start_date - timedelta(days=1), req.end_date)):
            if overlaps(s, e, *n.interval()):
                clashes.append(f"after hours {fmt_date(n.date)} ({n.location})")
        for g in GentleWayAttendance.objects.filter(staff=req.staff, date__range=(req.start_date, req.end_date)):
            if overlaps(s, e, *g.interval()):
                clashes.append(str(g))
        for c in clashes:
            messages.warning(request, f"Already rostered during this time - reassign: {c}")
    return redirect("roster:requests")


@login_required
@require_POST
def decide_swap(request, pk):
    req = get_object_or_404(
        SwapRequest.objects.select_related("shift__location", "oncall__location", "gentle_way__location", "requester"),
        pk=pk,
        status=RequestStatus.PENDING,
    )
    location = req.location
    require_editor(request.user, location)
    form = DecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose approve or decline.")
        return redirect("roster:requests")
    note = form.cleaned_data["note"]
    if form.cleaned_data["action"] == "decline":
        req.status = RequestStatus.DECLINED
        req.decided_by, req.decided_at, req.decision_note = request.user, timezone.now(), note
        req.save()
        _log(request, location, "swap declined", str(req))
        messages.success(request, "Swap request declined.")
        return redirect("roster:requests")

    replacement = form.cleaned_data["replacement"] or req.proposed_staff
    if replacement is None:
        messages.error(request, "Choose who takes it before approving.")
        return redirect("roster:requests")
    if replacement.pk == req.requester_id:
        messages.error(request, "The replacement is the person asking to swap.")
        return redirect("roster:requests")

    with transaction.atomic():
        item = req.item
        if req.shift:
            item.staff = replacement
            issues = rules.check_shift(item)
        elif req.oncall:
            # Hand over every role the requester had that night (pair, third person, phones).
            fields = [f for f in OnCallNight.ROLE_FIELDS if getattr(item, f + "_id") == req.requester_id]
            if not fields:
                messages.error(request, "The requester is no longer on that after-hours team.")
                return redirect("roster:requests")
            for f in fields:
                setattr(item, f, replacement)
            issues = rules.check_oncall(item)
        else:
            item.staff = replacement
            issues = rules.check_gentle_way(item)
        errs = rules.errors(issues)
        if errs:
            for e in errs:
                messages.error(request, f"Can't approve: {e.message}")
            return redirect("roster:requests")
        item.save()
        req.status = RequestStatus.APPROVED
        req.proposed_staff = replacement
        req.decided_by, req.decided_at, req.decision_note = request.user, timezone.now(), note
        req.save()
    _log(request, location, "swap approved", f"{req.item_label}: {req.requester} -> {replacement}")
    for w in rules.warnings(issues):
        messages.warning(request, w.message)
    messages.success(request, f"Swap approved. {replacement} now has: {req.item_label}.")
    return redirect("roster:requests")
