"""
Load Radlea's locations, staff, usual shifts and Queensland public holidays.

Source of staff data: "Radlea Staff Pay Master Register" v0.2 (2 Nov 2026)
and the payrun of 2 Nov 2026. Pay rates are deliberately NOT loaded.

Safe to run more than once, and it runs on every deploy: it only ADDS what
is missing and never changes anything edited later in Setup. Use --with-logins to create a login for each person; the
temporary passwords are printed once and must be changed.

    python manage.py seed_radlea
    python manage.py seed_radlea --with-logins
"""
from datetime import date, time
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from org.logins import create_login
from org.models import Location, StaffMember
from roster.models import PublicHoliday, ShiftPattern

LOCATIONS = [
    {
        "code": "TVL",
        "name": "Townsville Funerals, Cremations & Crematorium",
        "short_name": "Townsville",
        "sort_order": 1,
        "has_gentle_way": True,
    },
    {
        "code": "BUR",
        "name": "Burdekin Funerals",
        "short_name": "Ayr",
        "sort_order": 2,
        "has_gentle_way": False,
    },
]

FT, CAS, DIR = StaffMember.FULL_TIME, StaffMember.CASUAL, StaffMember.DIRECTOR

# (first, last, role, employment, grade, home, extra locations, usual Mon-Fri shift, flags)
# Usual shifts are from the register's "Usual shift" column. Shifts of 8h06m
# include a 30 minute unpaid meal break = 7.6 paid hours = 38 per week.
STAFF = [
    # Townsville
    ("Natalie", "Miller", "Office Manager / Funeral Coordinator", FT, "Grade 4", "TVL", [], ("08:30", "16:36"), {"editor": ["TVL"]}),
    ("Shane", "Marshall", "Operations Supervisor / Gentle Way Operator", FT, "Grade 3", "TVL", [], ("07:00", "15:06"), {"gentle_way": True}),
    ("Alan", "Mills", "", FT, "Grade 3", "TVL", [], ("08:30", "16:36"), {}),
    ("Kylie", "Young", "Arranger / Conductor / Celebrant", FT, "Grade 3", "TVL", [], ("09:00", "17:06"), {}),
    ("Michelle", "Staszinszky", "Arranger / Conductor / Palm Island Liaison", FT, "Grade 3", "TVL", [], ("09:00", "17:06"), {}),
    ("Beau", "Wedge", "", FT, "Grade 2", "TVL", [], ("09:00", "17:06"), {}),
    ("Jarrod", "Nelson", "FDA / Mortuary Assistant", FT, "Grade 2", "TVL", [], ("08:00", "16:06"), {}),
    ("James", "Brennan", "", CAS, "Grade 1", "TVL", [], None, {}),
    # Ayr (Burdekin)
    ("Amie", "Griggs", "Manager, Burdekin Funerals", FT, "Grade 3", "BUR", [], ("08:30", "16:36"), {"editor": ["BUR"]}),
    ("Danny", "Parfitt", "", CAS, "Grade 2", "BUR", [], None, {}),
    ("Chloe", "Taiters", "", CAS, "Grade 1", "BUR", ["TVL"], None, {}),
    ("Kara-Leagh", "Dennis", "", CAS, "Grade 1", "BUR", [], None, {}),
    ("Graham", "Brierley", "", CAS, "Grade 2", "BUR", ["TVL"], None, {}),
    # Directors. Robin is on the after-hours roster (weekday standby in
    # Townsville) and is the second Gentle Way operator (confirmed 08/11/2026).
    # Leanne is not rostered. [ASSUMPTION: confirm]
    ("Robin", "Drury", "Director / Funeral Director", DIR, "", "TVL", ["BUR"], None,
     {"superuser": True, "gentle_way": True, "email": "robin@radlea.com.au"}),
    ("Leanne", "Drury", "Director", DIR, "", "TVL", ["BUR"], None,
     {"superuser": True, "on_roster": False, "can_do_transfers": False, "email": "leanne@radlea.com.au"}),
]

# Queensland-wide public holidays from qld.gov.au (checked 08/11/2026).
# The Royal Queensland Show holiday is Brisbane-area only, so it is left out.
# Townsville and Burdekin show holidays for 2027 are not loaded: add them in
# Setup > Public holidays (with the location set) once gazetted.
QLD_SOURCE = "qld.gov.au/recreation/travel/holidays/public (checked 08/11/2026)"
HOLIDAYS = [
    (date(2026, 12, 24), "Christmas Eve (part-day)", time(18, 0)),
    (date(2026, 12, 25), "Christmas Day", None),
    (date(2026, 12, 26), "Boxing Day", None),
    (date(2026, 12, 28), "Boxing Day (substitute)", None),
    (date(2027, 1, 1), "New Year's Day", None),
    (date(2027, 1, 26), "Australia Day", None),
    (date(2027, 3, 26), "Good Friday", None),
    (date(2027, 3, 27), "Day after Good Friday", None),
    (date(2027, 3, 28), "Easter Sunday", None),
    (date(2027, 3, 29), "Easter Monday", None),
    (date(2027, 4, 26), "Anzac Day (observed)", None),
    (date(2027, 5, 3), "Labour Day", None),
    (date(2027, 10, 4), "King's Birthday", None),
    (date(2027, 12, 24), "Christmas Eve (part-day)", time(18, 0)),
    (date(2027, 12, 25), "Christmas Day", None),
    (date(2027, 12, 26), "Boxing Day", None),
    (date(2027, 12, 27), "Christmas Day (substitute)", None),
    (date(2027, 12, 28), "Boxing Day (substitute)", None),
]


def _t(s):
    h, m = s.split(":")
    return time(int(h), int(m))


class Command(BaseCommand):
    help = "Load Radlea locations, staff, usual shifts and Queensland public holidays."

    def add_arguments(self, parser):
        parser.add_argument("--with-logins", action="store_true", help="Create a login for each person.")

    @transaction.atomic
    def handle(self, *args, **opts):
        # Create-only: this runs on every deploy, so it must never overwrite
        # changes made later in Setup (times, roles, flags, people who left).
        locs = {}
        for spec in LOCATIONS:
            code = spec["code"]
            loc, _ = Location.objects.get_or_create(code=code, defaults={k: v for k, v in spec.items() if k != "code"})
            locs[code] = loc

        passwords = []
        for first, last, role, emp, grade, home, extra, usual, flags in STAFF:
            staff = StaffMember.objects.filter(first_name=first, last_name=last).first()
            if staff is None:
                staff = StaffMember.objects.create(
                    first_name=first,
                    last_name=last,
                    role_title=role,
                    employment_type=emp,
                    award_grade=grade,
                    home_location=locs[home],
                    contracted_weekly_hours=Decimal("38") if emp == FT else None,
                    on_roster=flags.get("on_roster", True),
                    can_do_transfers=flags.get("can_do_transfers", True),
                    gentle_way_trained=flags.get("gentle_way", False),
                    email=flags.get("email", ""),
                )
                staff.extra_locations.add(*[locs[c] for c in extra])
                staff.editor_locations.add(*[locs[c] for c in flags.get("editor", [])])
                if usual:
                    start, end = _t(usual[0]), _t(usual[1])
                    for wd in range(5):
                        ShiftPattern.objects.create(
                            staff=staff, location=locs[home], weekday=wd, start=start, end=end, unpaid_break_minutes=30
                        )
                self.stdout.write(f"  added {staff.display_name} ({staff.home_location})")

            if opts["with_logins"] and staff.user is None:
                passwords.append(create_login(staff, superuser=flags.get("superuser", False)))

        for d, name, part in HOLIDAYS:
            PublicHoliday.objects.get_or_create(
                date=d, location=None, defaults={"name": name, "part_day_from": part, "source": QLD_SOURCE}
            )
        self.stdout.write(f"Public holidays loaded: {len(HOLIDAYS)}")

        if passwords:
            self.stdout.write(self.style.WARNING("\nTemporary passwords (shown once; change them on first use):"))
            for u, p in passwords:
                self.stdout.write(f"  {u:<24} {p}")
        self.stdout.write(self.style.SUCCESS("Roster data checked."))
