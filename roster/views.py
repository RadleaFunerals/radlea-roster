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
from org.models import Location, StaffMember

from . import rules
from .forms import (
    DecisionForm,
    GentleWayForm,
    OnCallForm,
    ShiftForm,
    SwapForm,
    UnavailabilityForm,
)
from .models import (
    ChangeLog,
    GentleWayAttendance,
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


# ------------------------------------------------------------------ helpers

def _location(code):
    return get_object_or_404(Location, code=code.upper(), active=True)


def _parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise Http404("Bad date")


def _week_url(location, d):
    return reverse("roster:week", args=[location.code, week_start(d).isoformat()])


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


def _week_context(location, ws, editor):
    """Everything needed to draw one location's week (screen and print)."""
    days = week_days(ws)
    end = days[-1]
    shifts = list(
        Shift.objects.filter(location=location, date__range=(ws, end)).select_related("staff")
    )
    base_staff = list(
        StaffMember.objects.filter(active=True, on_roster=True)
        .filter(Q(home_location=location) | Q(extra_locations=location))
        .distinct()
    )
    by_id = {s.pk: s for s in base_staff}
    for sh in shifts:
        by_id.setdefault(sh.staff_id, sh.staff)
    staff = sorted(
        by_id.values(),
        key=lambda s: (s.home_location_id != location.pk, s.is_casual, s.last_name, s.first_name),
    )

    leave = {}
    for u in UnavailabilityRequest.objects.filter(
        staff__in=staff, status=RequestStatus.APPROVED, start_date__lte=end, end_date__gte=ws
    ):
        for d in days:
            if u.start_date <= d <= u.end_date:
                leave[(u.staff_id, d)] = u

    rows = []
    for s in staff:
        cells = []
        total = 0
        for d in days:
            day_shifts = [sh for sh in shifts if sh.staff_id == s.pk and sh.date == d]
            total += sum(sh.paid_hours for sh in day_shifts)
            cells.append({"date": d, "shifts": day_shifts, "leave": leave.get((s.pk, d))})
        rows.append({"staff": s, "cells": cells, "hours": total})

    nights = {
        n.date: n
        for n in OnCallNight.objects.filter(location=location, date__range=(ws, end)).select_related(
            "staff_one", "staff_two"
        )
    }
    oncall = []
    for d in days:
        n = nights.get(d)
        s, e = OnCallNight.interval_for(location, d)
        oncall.append(
            {
                "date": d,
                "night": n,
                "kind": OnCallNight.kind_for(location, d),
                "start": timezone.localtime(s),
                "end": timezone.localtime(e),
            }
        )

    gentle = []
    if location.has_gentle_way:
        atts = list(
            GentleWayAttendance.objects.filter(location=location, date__range=(ws, end)).select_related("staff")
        )
        gentle = [{"date": d, "items": [a for a in atts if a.date == d]} for d in days]

    holidays = {}
    for h in PublicHoliday.objects.filter(date__range=(ws, end)).filter(
        Q(location__isnull=True) | Q(location=location)
    ):
        holidays[h.date] = h
    day_info = [{"date": d, "holiday": holidays.get(d)} for d in days]

    roster_week = RosterWeek.objects.filter(location=location, week_start=ws).first()
    return {
        "location": location,
        "ws": ws,
        "days": day_info,
        "rows": rows,
        "oncall": oncall,
        "gentle": gentle,
        "roster_week": roster_week,
        "published": bool(roster_week and roster_week.published_at),
        "published_at": roster_week.published_at if roster_week else None,
        "issues": rules.week_issues(location, ws) if editor else [],
    }


# --------------------------------------------------------------- top pages

@login_required
def home(request):
    editable = editable_locations(request.user)
    if editable.exists():
        return redirect("roster:week_current", code=editable.first().code)
    return redirect("roster:my_roster")


@login_required
def week_view(request, code, start=None):
    location = _location(code)
    ws = week_start(_parse_date(start)) if start else week_start(timezone.localdate())
    editor = can_edit(request.user, location)
    ctx = _week_context(location, ws, editor)
    ctx.update(
        {
            "editor": editor,
            "visible": editor or ctx["published"],
            "prev_week": ws - timedelta(days=7),
            "next_week": ws + timedelta(days=7),
            "this_week": week_start(timezone.localdate()),
        }
    )
    return render(request, "roster/week.html", ctx)


@login_required
def print_week(request, code, start):
    location = _location(code)
    ws = week_start(_parse_date(start))
    editor = can_edit(request.user, location)
    ctx = _week_context(location, ws, editor=False)
    if not (editor or ctx["published"]):
        raise Http404("This roster hasn't been published.")
    ctx["printed_at"] = timezone.localtime()
    return render(request, "roster/print_week.html", ctx)


@login_required
def tonight(request):
    """Who is on after hours right now (or next), per location, plus today's shifts."""
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
            night = OnCallNight.objects.filter(location=loc, date=d).select_related("staff_one", "staff_two").first()
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
            Shift.objects.filter(location=loc, date=today).select_related("staff") if visible(today) else []
        )
        gw = []
        if loc.has_gentle_way:
            gw = [
                g
                for g in GentleWayAttendance.objects.filter(
                    location=loc, date__range=(today, today + timedelta(days=1))
                )
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
    for sh in Shift.objects.filter(staff=staff, date__range=(start, end)).select_related("location"):
        if visible(sh.location_id, sh.date):
            items.append(
                {
                    "sort": sh.interval()[0],
                    "ends": sh.interval()[1],
                    "date": sh.date,
                    "what": sh.get_shift_type_display(),
                    "when": sh.time_label,
                    "where": sh.location,
                    "detail": sh.note,
                    "ref": f"shift:{sh.pk}",
                    "label": f"Shift {fmt_date(sh.date)} {sh.time_label} ({sh.location})",
                    **flags(sh.location_id, sh.date, sh.updated_at),
                }
            )
    for n in OnCallNight.objects.filter(Q(staff_one=staff) | Q(staff_two=staff), date__range=(start, end)).select_related(
        "location", "staff_one", "staff_two"
    ):
        if visible(n.location_id, n.date):
            s, e = n.interval()
            partner = n.partner_of(staff)
            items.append(
                {
                    "sort": s,
                    "ends": e,
                    "date": n.date,
                    "what": f"After hours ({n.get_kind_display().lower()})",
                    "when": f"{timezone.localtime(s):%H:%M} to {timezone.localtime(e):%H:%M %a}",
                    "where": n.location,
                    "detail": f"With {partner.display_name}" + (f" ({partner.mobile})" if partner.mobile else "")
                    if partner
                    else "Partner not yet rostered",
                    "ref": f"oncall:{n.pk}",
                    "label": f"After hours {fmt_date(n.date)} ({n.location})",
                    **flags(n.location_id, n.date, n.updated_at),
                }
            )
    for g in GentleWayAttendance.objects.filter(staff=staff, date__range=(start, end)).exclude(
        status=GentleWayAttendance.CANCELLED
    ).select_related("location"):
        if visible(g.location_id, g.date):
            items.append(
                {
                    "sort": g.interval()[0],
                    "ends": g.interval()[1],
                    "date": g.date,
                    "what": f"Gentle Way: {g.get_process_type_display()}",
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
    ctx = {"staff": staff, "items": [], "requests": [], "swaps": []}
    if staff is not None:
        # Start from yesterday so an after-hours shift still running this morning shows.
        ctx["items"] = _my_items(staff, request.user, today - timedelta(days=1), today + timedelta(days=27))
        ctx["requests"] = staff.unavailability.all()[:10]
        ctx["swaps"] = staff.swap_requests.select_related(
            "shift__location", "oncall__location", "gentle_way__location", "proposed_staff"
        )[:10]
    return render(request, "roster/my_roster.html", ctx)


# -------------------------------------------------------- editing: shifts

@login_required
def shift_create(request, code):
    location = _location(code)
    require_editor(request.user, location)
    initial = {"unpaid_break_minutes": 30}
    if request.GET.get("date"):
        initial["date"] = _parse_date(request.GET["date"])
    if request.GET.get("staff"):
        initial["staff"] = request.GET["staff"]
    form = ShiftForm(request.POST or None, location=location, initial=initial)
    if request.method == "POST" and form.is_valid():
        shift = form.save(commit=False)
        shift.location = location
        shift.created_by = request.user
        shift.save()
        _log(request, location, "shift added", str(shift))
        _show_warnings(request, form)
        messages.success(request, f"Shift added: {shift}.")
        return redirect(_week_url(location, shift.date))
    return render(request, "roster/form.html", {"form": form, "title": f"Add shift - {location}", "location": location})


@login_required
def shift_edit(request, pk):
    shift = get_object_or_404(Shift.objects.select_related("location", "staff"), pk=pk)
    location = shift.location
    require_editor(request.user, location)
    before = str(shift)
    form = ShiftForm(request.POST or None, instance=shift, location=location)
    if request.method == "POST" and form.is_valid():
        shift = form.save()
        _log(request, location, "shift changed", f"{before} -> {shift}")
        _show_warnings(request, form)
        messages.success(request, "Shift updated.")
        return redirect(_week_url(location, shift.date))
    return render(
        request,
        "roster/form.html",
        {
            "form": form,
            "title": f"Edit shift - {location}",
            "location": location,
            "delete_url": reverse("roster:shift_delete", args=[shift.pk]),
        },
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
    return redirect(_week_url(location, d))


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
        if not night.staff_one and not night.staff_two:
            if night.pk:
                night.delete()
                _log(request, location, "after hours cleared", before)
            messages.info(request, f"After-hours team for {fmt_date(d)} cleared.")
            return redirect(_week_url(location, d))
        night.save()
        _log(request, location, "after hours set", f"{before} -> {night}" if before else str(night))
        _show_warnings(request, form)
        messages.success(request, f"After-hours team saved: {night}.")
        return redirect(_week_url(location, d))
    title = (
        f"After hours - {location} - {d:%a} {fmt_date(d)} "
        f"({OnCallNight.kind_for(location, d).label}, {timezone.localtime(s):%H:%M} to "
        f"{timezone.localtime(e):%H:%M %a})"
    )
    return render(request, "roster/form.html", {"form": form, "title": title, "location": location})


@login_required
@require_POST
def copy_oncall(request, code, start):
    """Copy last week's after-hours pairs into any empty places this week."""
    location = _location(code)
    require_editor(request.user, location)
    ws = week_start(_parse_date(start))
    prev = {n.date: n for n in OnCallNight.objects.filter(location=location, date__range=(ws - timedelta(days=7), ws - timedelta(days=1)))}
    copied, skipped = 0, []
    for d in week_days(ws):
        src = prev.get(d - timedelta(days=7))
        if not src:
            continue
        night = OnCallNight.objects.filter(location=location, date=d).first() or OnCallNight(location=location, date=d)
        changed = False
        for field in ("staff_one", "staff_two"):
            if getattr(night, field) is None and getattr(src, field) is not None:
                if not night.has_member(getattr(src, field)):
                    setattr(night, field, getattr(src, field))
                    changed = True
        if not changed:
            continue
        errs = rules.errors(rules.check_oncall(night))
        if errs:
            skipped.append(f"{d:%a} {fmt_date(d)}: {errs[0].message}")
            continue
        night.save()
        copied += 1
    _log(request, location, "after hours copied", f"Week of {fmt_date(ws)}: {copied} nights copied")
    messages.success(request, f"Copied last week's after-hours team into {copied} night(s).")
    for s in skipped:
        messages.warning(request, f"Not copied - {s}")
    return redirect(_week_url(location, ws))


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
        return redirect(_week_url(location, att.date))
    return render(
        request, "roster/form.html", {"form": form, "title": f"Add Gentle Way attendance - {location}", "location": location}
    )


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
        return redirect(_week_url(location, att.date))
    return render(
        request,
        "roster/form.html",
        {
            "form": form,
            "title": f"Edit Gentle Way attendance - {location}",
            "location": location,
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
    return redirect(_week_url(location, d))


# ---------------------------------------------------- week-level actions

@login_required
@require_POST
def fill_usual(request, code, start):
    """Fill the week from each person's usual shifts, skipping anything that breaks a rule."""
    location = _location(code)
    require_editor(request.user, location)
    ws = week_start(_parse_date(start))
    created, skipped = 0, []
    patterns = ShiftPattern.objects.filter(location=location, staff__active=True, staff__on_roster=True).select_related(
        "staff"
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
            shift_type=p.shift_type,
            created_by=request.user,
        )
        errs = rules.errors(rules.check_shift(shift))
        if errs:
            skipped.append(f"{p.staff.short_name} {d:%a}: {errs[0].message}")
            continue
        shift.save()
        created += 1
    _log(request, location, "week filled", f"Week of {fmt_date(ws)}: {created} usual shifts added")
    messages.success(request, f"Added {created} usual shift(s).")
    for s in skipped:
        messages.warning(request, f"Skipped - {s}")
    return redirect(_week_url(location, ws))


@login_required
@require_POST
def publish_week(request, code, start):
    location = _location(code)
    require_editor(request.user, location)
    ws = week_start(_parse_date(start))
    errs = [i for _, i in rules.week_issues(location, ws) if i.is_error]
    if errs and request.POST.get("override") != "1":
        messages.error(
            request,
            f"This week has {len(errs)} roster error(s). Fix them, or tick 'Publish anyway' to publish regardless.",
        )
        return redirect(_week_url(location, ws))
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
    return redirect(_week_url(location, ws))


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
    if request.GET.get("item"):
        initial = {"item": request.GET["item"]}
    else:
        initial = {}
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
                OnCallNight.objects.filter(Q(staff_one=staff) | Q(staff_two=staff)), pk=pk
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
        {"form": form, "title": "Swap or give away a shift", "empty_note": None if choices else "You have nothing rostered in the next 60 days."},
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
        for n in OnCallNight.objects.filter(
            Q(staff_one=req.staff) | Q(staff_two=req.staff),
            date__range=(req.start_date - timedelta(days=1), req.end_date),
        ):
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
            if item.staff_one_id == req.requester_id:
                item.staff_one = replacement
            elif item.staff_two_id == req.requester_id:
                item.staff_two = replacement
            else:
                messages.error(request, "The requester is no longer on that after-hours team.")
                return redirect("roster:requests")
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
