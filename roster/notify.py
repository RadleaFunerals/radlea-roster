"""
Email each rostered person their week when a roster is published.

Only people with an email address in their staff record are emailed. If no
mail server is configured, emails are printed to the server log instead of
being sent (so nothing breaks). SMS can be added here later.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import GentleWayAttendance, OnCallNight, Shift
from .timeutils import fmt_date

log = logging.getLogger(__name__)


def _lines_for(staff, location, ws):
    end = ws + timedelta(days=6)
    lines = []
    for sh in Shift.objects.filter(staff=staff, location=location, date__range=(ws, end)):
        extra = "" if sh.shift_type == "ORD" else f" ({sh.get_shift_type_display()})"
        note = f" - {sh.note}" if sh.note else ""
        lines.append((sh.date, sh.start, f"{sh.date:%a} {fmt_date(sh.date)}  {sh.time_label}{extra}{note}"))
    for n in OnCallNight.objects.filter(Q(staff_one=staff) | Q(staff_two=staff), location=location, date__range=(ws, end)):
        s, e = n.interval()
        partner = n.partner_of(staff)
        with_ = f" with {partner.display_name}" if partner else " (partner to be rostered)"
        lines.append(
            (
                n.date,
                timezone.localtime(s).time(),
                f"{n.date:%a} {fmt_date(n.date)}  AFTER HOURS {timezone.localtime(s):%H:%M} to "
                f"{timezone.localtime(e):%H:%M %a}{with_}",
            )
        )
    for g in GentleWayAttendance.objects.filter(staff=staff, location=location, date__range=(ws, end)).exclude(
        status=GentleWayAttendance.CANCELLED
    ):
        lines.append(
            (g.date, g.start, f"{g.date:%a} {fmt_date(g.date)}  GENTLE WAY {g.start:%H:%M} {g.get_process_type_display()}")
        )
    lines.sort(key=lambda x: (x[0], x[1]))
    return [l[2] for l in lines]


def email_week(location, ws, republished=False):
    """Returns (number emailed, names with no email address, names that failed)."""
    from org.models import StaffMember

    end = ws + timedelta(days=6)
    ids = set(Shift.objects.filter(location=location, date__range=(ws, end)).values_list("staff_id", flat=True))
    for a, b in OnCallNight.objects.filter(location=location, date__range=(ws, end)).values_list("staff_one_id", "staff_two_id"):
        ids.update(x for x in (a, b) if x)
    ids.update(
        GentleWayAttendance.objects.filter(location=location, date__range=(ws, end), staff__isnull=False).values_list(
            "staff_id", flat=True
        )
    )
    site = getattr(settings, "SITE_URL", "").rstrip("/")
    link = f"{site}{reverse('roster:my_roster')}" if site else ""
    subject = f"{'UPDATED ' if republished else ''}{location.short_name} roster: week of {fmt_date(ws)}"

    sent, missing, failed = 0, [], []
    for staff in StaffMember.objects.filter(pk__in=ids, active=True).order_by("last_name"):
        if not staff.email:
            missing.append(staff.display_name)
            continue
        lines = _lines_for(staff, location, ws)
        body = "\n".join(
            [
                f"Hi {staff.preferred_name or staff.first_name},",
                "",
                f"The {location.short_name} roster for the week of {fmt_date(ws)} has been "
                f"{'updated' if republished else 'published'}. You are rostered:",
                "",
                *lines,
                "",
                f"See it any time: {link}" if link else "See it any time in the Radlea Roster app.",
                "If you can't work any of these, use 'Swap' or 'I'm not available' in the app.",
                "",
                "Radlea Pty Ltd",
            ]
        )
        try:
            send_mail(subject, body, None, [staff.email])
            sent += 1
        except Exception:  # a mail problem must never stop the roster being published
            log.exception("Roster email to %s failed", staff.email)
            failed.append(staff.display_name)
    return sent, missing, failed
