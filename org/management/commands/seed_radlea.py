"""
Load Radlea's sites, business areas, skills, staff, usual shifts and
Queensland public holidays.

Sources: "Radlea Staff Pay Master Register" v0.2 (02/11/2026), the payrun of
02/11/2026 (who did standby), and Robin's answers (08/11/2026 and 25/11/2026).
Starting skills are taken from job titles and recent standby: CHECK THEM on
the Skills page. Pay rates are deliberately NOT loaded.

Safe to run more than once, and it runs on every deploy: it only ADDS what is
missing and never changes anything edited later in Setup or on the Skills
page. Use --with-logins to create a login for each person; the temporary
passwords are printed once.

    python manage.py seed_radlea
    python manage.py seed_radlea --with-logins
"""
from datetime import date, time
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from org.logins import create_login
from org.models import Area, Location, Skill, StaffMember, StaffSkill
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

# (code, name, description)
SKILLS = [
    (Skill.AH_TRANSFER, "After-hours transfers", "Can be on the after-hours transfer team"),
    (Skill.DAY_TRANSFER, "Daytime transfers", "Hospital, nursing home and home transfers in business hours"),
    (Skill.MORTUARY, "Mortuary & care", "Preparation, dressing and care of the deceased"),
    (Skill.ARRANGING, "Arranging", "Arrangement meetings with families"),
    (Skill.CONDUCTING, "Conducting", "Conducting / funeral directing on the day"),
    ("SERVICE", "Service attendant", "Bearer, usher and service support"),
    ("HEARSE", "Hearse driving", "Driving the hearse and funeral vehicles"),
    (Skill.CELEBRANT, "Celebrant", "Leading services as celebrant"),
    (Skill.GENTLE_WAY, "Gentle Way operation", "Running and checking the alkaline hydrolysis unit"),
    (Skill.OFFICE, "Office & reception", "Phones, office and paperwork"),
    ("PALM_ISLAND", "Palm Island liaison", "Palm Island community liaison"),
]

# (code, name, colour, required skill code or None)
# A required skill means only people with that skill can be rostered in the area.
AREAS = [
    ("OPS", "Operations", "#dbe7f7", None),
    ("ARRANGE", "Arrangements", "#fde9c8", Skill.ARRANGING),
    ("FUNERAL", "Funeral services", "#d9f0e3", None),
    ("TRANSFER", "Transfers", "#e6e9f4", Skill.DAY_TRANSFER),
    ("MORTUARY", "Mortuary & care", "#f6dede", Skill.MORTUARY),
    ("OFFICE", "Office & admin", "#ececec", None),
    ("GENTLEWAY", "Gentle Way", "#efe6f4", Skill.GENTLE_WAY),
    ("TRAINING", "Training / meeting", "#fff6c2", None),
]

FT, CAS, DIR = StaffMember.FULL_TIME, StaffMember.CASUAL, StaffMember.DIRECTOR
T, C, TR = StaffSkill.TRAINEE, StaffSkill.COMPETENT, StaffSkill.TRAINER

# (first, last, role, employment, grade, home, extra sites, usual Mon-Fri shift, primary area, skills, flags)
# Usual shifts are from the register. Shifts of 8h06m include a 30 minute
# unpaid meal break = 7.6 paid hours = 38 per week.
# Skills: from job titles and who did standby in the 02/11/2026 payrun.
# Chloe and James are new (Robin, 25/11/2026) so are trainees for after hours.
# Alan does daytime transfers but not mortuary work (Robin, 25/11/2026).
STAFF = [
    # Townsville
    ("Natalie", "Miller", "Office Manager / Funeral Coordinator", FT, "Grade 4", "TVL", [], ("08:30", "16:36"), "OFFICE",
     {Skill.OFFICE: C, Skill.AH_TRANSFER: C}, {"editor": ["TVL"]}),
    ("Shane", "Marshall", "Operations Supervisor / Gentle Way Operator", FT, "Grade 3", "TVL", [], ("07:00", "15:06"), "OPS",
     {Skill.GENTLE_WAY: TR}, {}),
    ("Alan", "Mills", "", FT, "Grade 3", "TVL", [], ("08:30", "16:36"), "OPS",
     {Skill.DAY_TRANSFER: C}, {}),
    ("Kylie", "Young", "Arranger / Conductor / Celebrant", FT, "Grade 3", "TVL", [], ("09:00", "17:06"), "ARRANGE",
     {Skill.ARRANGING: C, Skill.CONDUCTING: C, Skill.CELEBRANT: C}, {}),
    ("Michelle", "Staszinszky", "Arranger / Conductor / Palm Island Liaison", FT, "Grade 3", "TVL", [], ("09:00", "17:06"), "ARRANGE",
     {Skill.ARRANGING: C, Skill.CONDUCTING: C, "PALM_ISLAND": C}, {}),
    ("Beau", "Wedge", "", FT, "Grade 2", "TVL", [], ("09:00", "17:06"), "OPS", {}, {}),
    ("Jarrod", "Nelson", "FDA / Mortuary Assistant", FT, "Grade 2", "TVL", [], ("08:00", "16:06"), "MORTUARY",
     {Skill.MORTUARY: C, Skill.DAY_TRANSFER: C}, {}),
    ("James", "Brennan", "", CAS, "Grade 1", "TVL", [], None, None,
     {Skill.AH_TRANSFER: T}, {}),
    # Ayr (Burdekin)
    ("Amie", "Griggs", "Manager, Burdekin Funerals", FT, "Grade 3", "BUR", [], ("08:30", "16:36"), "ARRANGE",
     {Skill.ARRANGING: C, Skill.CONDUCTING: C, Skill.AH_TRANSFER: C, Skill.OFFICE: C}, {"editor": ["BUR"]}),
    ("Danny", "Parfitt", "", CAS, "Grade 2", "BUR", [], None, None, {Skill.AH_TRANSFER: C}, {}),
    ("Chloe", "Taiters", "", CAS, "Grade 1", "BUR", ["TVL"], None, None, {Skill.AH_TRANSFER: T}, {}),
    ("Kara-Leagh", "Dennis", "", CAS, "Grade 1", "BUR", [], None, None, {Skill.AH_TRANSFER: C}, {}),
    ("Graham", "Brierley", "", CAS, "Grade 2", "BUR", ["TVL"], None, None, {Skill.AH_TRANSFER: C}, {}),
    # Directors. Robin does after hours and is the second Gentle Way operator
    # (08/11/2026). Leanne is not rostered. [ASSUMPTION: confirm]
    ("Robin", "Drury", "Director / Funeral Director", DIR, "", "TVL", ["BUR"], None, None,
     {Skill.AH_TRANSFER: TR, Skill.DAY_TRANSFER: TR, Skill.MORTUARY: TR, Skill.ARRANGING: TR, Skill.CONDUCTING: TR,
      Skill.CELEBRANT: C, Skill.GENTLE_WAY: TR},
     {"superuser": True, "email": "robin@radlea.com.au"}),
    ("Leanne", "Drury", "Director", DIR, "", "TVL", ["BUR"], None, None, {},
     {"superuser": True, "on_roster": False, "email": "leanne@radlea.com.au"}),
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
    help = "Load Radlea sites, areas, skills, staff, usual shifts and Queensland public holidays."

    def add_arguments(self, parser):
        parser.add_argument("--with-logins", action="store_true", help="Create a login for each person.")

    @transaction.atomic
    def handle(self, *args, **opts):
        # Create-only: this runs on every deploy, so it must never overwrite
        # changes made later in Setup (times, roles, skills, people who left).
        locs = {}
        for spec in LOCATIONS:
            code = spec["code"]
            loc, _ = Location.objects.get_or_create(code=code, defaults={k: v for k, v in spec.items() if k != "code"})
            locs[code] = loc

        skills = {}
        for i, (code, name, desc) in enumerate(SKILLS):
            skills[code], _ = Skill.objects.get_or_create(
                code=code, defaults={"name": name, "description": desc, "sort_order": i}
            )

        areas = {}
        for i, (code, name, colour, req) in enumerate(AREAS):
            areas[code], _ = Area.objects.get_or_create(
                code=code,
                defaults={"name": name, "colour": colour, "required_skill": skills.get(req), "sort_order": i},
            )

        passwords = []
        for first, last, role, emp, grade, home, extra, usual, area, staff_skills, flags in STAFF:
            staff = StaffMember.objects.filter(first_name=first, last_name=last).first()
            if staff is None:
                staff = StaffMember.objects.create(
                    first_name=first,
                    last_name=last,
                    role_title=role,
                    employment_type=emp,
                    award_grade=grade,
                    home_location=locs[home],
                    primary_area=areas.get(area),
                    contracted_weekly_hours=Decimal("38") if emp == FT else None,
                    on_roster=flags.get("on_roster", True),
                    email=flags.get("email", ""),
                )
                staff.extra_locations.add(*[locs[c] for c in extra])
                staff.editor_locations.add(*[locs[c] for c in flags.get("editor", [])])
                for code, level in staff_skills.items():
                    StaffSkill.objects.create(staff=staff, skill=skills[code], level=level)
                if usual:
                    start, end = _t(usual[0]), _t(usual[1])
                    for wd in range(5):
                        ShiftPattern.objects.create(
                            staff=staff,
                            location=locs[home],
                            weekday=wd,
                            start=start,
                            end=end,
                            unpaid_break_minutes=30,
                            area=areas.get(area) or areas["OPS"],
                        )
                self.stdout.write(f"  added {staff.display_name} ({staff.home_location})")

            if opts["with_logins"] and staff.user is None:
                passwords.append(create_login(staff, superuser=flags.get("superuser", False)))

        for d, name, part in HOLIDAYS:
            PublicHoliday.objects.get_or_create(
                date=d, location=None, defaults={"name": name, "part_day_from": part, "source": QLD_SOURCE}
            )

        if passwords:
            self.stdout.write(self.style.WARNING("\nTemporary passwords (shown once; change them on first use):"))
            for u, p in passwords:
                self.stdout.write(f"  {u:<24} {p}")
        self.stdout.write(self.style.SUCCESS("Roster data checked."))
