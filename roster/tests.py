"""
Tests for the roster rules that matter most in practice: who can be
rostered where and for what (skills and business areas), the after-hours
pair with a third person and phone holder, leave blocking, Gentle Way,
swaps, publishing, the combined view and who can see what.

Run:  python manage.py test
"""
import os
from datetime import date, time, timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import authenticate
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from org.models import Area, Location, Skill, StaffMember, StaffSkill

from . import rules
from .models import (
    ChangeLog,
    GentleWayAttendance,
    OnCallKind,
    OnCallNight,
    PublicHoliday,
    RequestStatus,
    RosterWeek,
    Shift,
    SwapRequest,
    UnavailabilityRequest,
)

# A plain week in early 2027: Mon 01/02/2027 to Sun 07/02/2027.
MON = date(2027, 2, 1)
TUE = MON + timedelta(days=1)
SAT = MON + timedelta(days=5)


class RosterTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_radlea", "--with-logins", stdout=StringIO())
        cls.tvl = Location.objects.get(code="TVL")
        cls.bur = Location.objects.get(code="BUR")
        get = lambda f: StaffMember.objects.get(first_name=f)  # noqa: E731  (first names are unique here)
        cls.natalie, cls.shane, cls.alan = get("Natalie"), get("Shane"), get("Alan")
        cls.jarrod, cls.james, cls.amie = get("Jarrod"), get("James"), get("Amie")
        cls.danny, cls.chloe, cls.graham = get("Danny"), get("Chloe"), get("Graham")
        cls.robin, cls.leanne, cls.kylie = get("Robin"), get("Leanne"), get("Kylie")
        cls.area = {a.code: a for a in Area.objects.all()}

    def shift(self, staff, d=MON, start=time(9), end=time(17, 6), location=None, area="OPS", **kw):
        return Shift(
            staff=staff, location=location or staff.home_location, date=d, start=start, end=end,
            area=self.area[area], **kw,
        )

    def night(self, location, d=MON, one=None, two=None, support=None, phones=None, role="TRAINER"):
        return OnCallNight(
            location=location, date=d, staff_one=one, staff_two=two, support_staff=support,
            support_role=role, phone_holder=phones,
        )

    def messages_of(self, issues, level=None):
        return [i.message for i in issues if level is None or i.level == level]


class SeedTests(RosterTestBase):
    def test_staff_loaded_from_register(self):
        self.assertEqual(
            StaffMember.objects.filter(home_location=self.tvl, on_roster=True).exclude(employment_type="DIR").count(), 8
        )
        self.assertEqual(StaffMember.objects.filter(home_location=self.bur).count(), 5)
        self.assertTrue(self.chloe.can_work_at(self.tvl))
        self.assertFalse(self.danny.can_work_at(self.tvl))
        self.assertFalse(self.leanne.on_roster)

    def test_skills_from_robins_answers(self):
        self.assertEqual(
            set(StaffMember.objects.filter(skills__skill__code=Skill.GENTLE_WAY).values_list("first_name", flat=True)),
            {"Shane", "Robin"},
        )
        self.assertEqual(self.chloe.skill_level(Skill.AH_TRANSFER), StaffSkill.TRAINEE)
        self.assertEqual(self.james.skill_level(Skill.AH_TRANSFER), StaffSkill.TRAINEE)
        self.assertTrue(self.alan.has_skill(Skill.DAY_TRANSFER))
        self.assertFalse(self.alan.has_skill(Skill.MORTUARY))

    def test_seed_is_repeatable_and_never_overwrites_setup_changes(self):
        self.jarrod.role_title = "Senior FDA"
        self.jarrod.save()
        StaffSkill.objects.filter(staff=self.chloe).update(level=StaffSkill.COMPETENT)
        self.tvl.weeknight_standby_start = time(18)
        self.tvl.save()
        call_command("seed_radlea", stdout=StringIO())
        self.assertEqual(StaffMember.objects.count(), 15)
        self.jarrod.refresh_from_db()
        self.tvl.refresh_from_db()
        self.assertEqual(self.jarrod.role_title, "Senior FDA")
        self.assertEqual(StaffSkill.objects.get(staff=self.chloe).level, StaffSkill.COMPETENT)
        self.assertEqual(self.tvl.weeknight_standby_start, time(18))


class AfterHoursKindTests(RosterTestBase):
    def test_weeknight_weekend_and_public_holiday(self):
        self.assertEqual(OnCallNight.kind_for(self.tvl, MON), OnCallKind.WEEKNIGHT)
        self.assertEqual(OnCallNight.kind_for(self.tvl, SAT), OnCallKind.WEEKEND)
        self.assertEqual(OnCallNight.kind_for(self.tvl, date(2027, 1, 26)), OnCallKind.PUBLIC_HOLIDAY)

    def test_christmas_eve_part_day_stays_a_weeknight(self):
        self.assertEqual(OnCallNight.kind_for(self.tvl, date(2027, 12, 24)), OnCallKind.WEEKNIGHT)

    def test_local_show_holiday_only_applies_to_its_location(self):
        PublicHoliday.objects.create(date=MON, name="Townsville Show", location=self.tvl)
        self.assertEqual(OnCallNight.kind_for(self.tvl, MON), OnCallKind.PUBLIC_HOLIDAY)
        self.assertEqual(OnCallNight.kind_for(self.bur, MON), OnCallKind.WEEKNIGHT)

    def test_windows(self):
        s, e = OnCallNight.interval_for(self.tvl, MON)
        self.assertEqual((timezone.localtime(s).hour, timezone.localtime(e).hour), (17, 8))
        self.assertEqual(timezone.localtime(e).date(), TUE)
        s, e = OnCallNight.interval_for(self.tvl, SAT)
        self.assertEqual((timezone.localtime(s).hour, (e - s).total_seconds() / 3600), (8, 24))


class ShiftRuleTests(RosterTestBase):
    def test_clean_shift_has_no_errors(self):
        self.assertEqual(rules.errors(rules.check_shift(self.shift(self.jarrod, area="MORTUARY"))), [])

    def test_area_needs_the_skill(self):
        # Alan can do a daytime transfer but not mortuary work.
        self.assertFalse(rules.errors(rules.check_shift(self.shift(self.alan, area="TRANSFER"))))
        errs = rules.errors(rules.check_shift(self.shift(self.alan, area="MORTUARY")))
        self.assertTrue(any("Mortuary & care" in e.message for e in errs))

    def test_trainee_in_area_warns(self):
        StaffSkill.objects.create(staff=self.alan, skill=Skill.objects.get(code=Skill.MORTUARY), level=StaffSkill.TRAINEE)
        issues = rules.check_shift(self.shift(self.alan, area="MORTUARY"))
        self.assertFalse(rules.errors(issues))
        self.assertTrue(any("trainee" in m for m in self.messages_of(issues, rules.WARNING)))

    def test_overlap_is_an_error(self):
        self.shift(self.jarrod).save()
        issues = rules.check_shift(self.shift(self.jarrod, start=time(16), end=time(18)))
        self.assertTrue(any("Overlaps" in m for m in self.messages_of(issues, rules.ERROR)))

    def test_overlap_across_sites_is_an_error(self):
        self.shift(self.chloe, location=self.bur, start=time(8), end=time(12), unpaid_break_minutes=0).save()
        issues = rules.check_shift(self.shift(self.chloe, location=self.tvl, start=time(11), end=time(15)))
        self.assertTrue(rules.errors(issues))

    def test_overnight_shift_overlaps_next_morning(self):
        self.shift(self.jarrod, start=time(22), end=time(6)).save()
        issues = rules.check_shift(self.shift(self.jarrod, d=TUE, start=time(5), end=time(9)))
        self.assertTrue(rules.errors(issues))

    def test_wrong_location_is_an_error_but_dual_site_casual_is_fine(self):
        self.assertTrue(rules.errors(rules.check_shift(self.shift(self.danny, location=self.tvl))))
        self.assertFalse(rules.errors(rules.check_shift(self.shift(self.chloe, location=self.tvl, end=time(13)))))

    def test_approved_leave_blocks_pending_leave_warns(self):
        u = UnavailabilityRequest.objects.create(staff=self.jarrod, start_date=MON, end_date=MON, kind="ANNUAL")
        issues = rules.check_shift(self.shift(self.jarrod))
        self.assertFalse(rules.errors(issues))
        self.assertTrue(any("pending" in m for m in self.messages_of(issues, rules.WARNING)))
        u.status = RequestStatus.APPROVED
        u.save()
        self.assertTrue(rules.errors(rules.check_shift(self.shift(self.jarrod))))

    def test_casual_under_four_hours_warns(self):
        issues = rules.check_shift(self.shift(self.james, start=time(9), end=time(12), unpaid_break_minutes=0))
        self.assertTrue(any("4-hour casual minimum" in m for m in self.messages_of(issues, rules.WARNING)))

    def test_over_38_hours_warns_for_full_time(self):
        for i in range(5):
            self.shift(self.jarrod, d=MON + timedelta(days=i), start=time(8), end=time(16, 6)).save()
        issues = rules.check_shift(self.shift(self.jarrod, d=SAT, start=time(9), end=time(12), unpaid_break_minutes=0))
        self.assertTrue(any("ordinary hours" in m for m in self.messages_of(issues, rules.WARNING)))

    def test_paid_hours_deducts_break(self):
        self.assertEqual(str(self.shift(self.jarrod, start=time(8), end=time(16, 6)).paid_hours), "7.60")


class AfterHoursRuleTests(RosterTestBase):
    def test_full_team_is_fine(self):
        n = self.night(self.tvl, one=self.robin, two=self.natalie, phones=self.robin)
        self.assertEqual(rules.check_oncall(n), [])

    def test_same_person_twice(self):
        n = self.night(self.tvl, one=self.natalie, two=self.natalie, phones=self.robin)
        self.assertTrue(rules.errors(rules.check_oncall(n)))

    def test_one_person_warns(self):
        issues = rules.check_oncall(self.night(self.tvl, one=self.natalie, phones=self.robin))
        self.assertFalse(rules.errors(issues))
        self.assertTrue(any("Only one person" in m for m in self.messages_of(issues)))

    def test_no_transfer_skill_is_an_error(self):
        n = self.night(self.tvl, one=self.alan, two=self.natalie, phones=self.robin)
        self.assertTrue(any("After-hours transfers" in m for m in self.messages_of(rules.check_oncall(n), rules.ERROR)))

    def test_two_trainees_need_a_trainer(self):
        # Chloe and James are both new: Robin rosters a third person with them.
        n = self.night(self.tvl, one=self.chloe, two=self.james, phones=self.robin)
        self.assertTrue(any("add a trainer" in m for m in self.messages_of(rules.check_oncall(n), rules.WARNING)))
        n.support_staff = self.robin
        self.assertEqual(rules.check_oncall(n), [])

    def test_trainee_with_a_trainer_in_the_pair_is_fine(self):
        n = self.night(self.tvl, one=self.james, two=self.robin, phones=self.robin)
        self.assertEqual(rules.check_oncall(n), [])

    def test_third_person_must_not_be_in_the_pair(self):
        n = self.night(self.tvl, one=self.robin, two=self.natalie, support=self.robin, phones=self.robin)
        self.assertTrue(rules.errors(rules.check_oncall(n)))

    def test_third_person_not_a_trainer_warns(self):
        n = self.night(self.tvl, one=self.chloe, two=self.james, support=self.natalie, phones=self.robin)
        self.assertTrue(any("isn't marked as a trainer" in m for m in self.messages_of(rules.check_oncall(n))))

    def test_missing_phone_holder_warns(self):
        n = self.night(self.tvl, one=self.robin, two=self.natalie)
        self.assertTrue(any("phones" in m for m in self.messages_of(rules.check_oncall(n), rules.WARNING)))

    def test_one_person_can_hold_both_sites_phones(self):
        OnCallNight.objects.create(location=self.bur, date=MON, staff_one=self.danny, staff_two=self.graham, phone_holder=self.robin)
        n = self.night(self.tvl, one=self.natalie, two=self.james, phones=self.robin)
        self.assertFalse(rules.errors(rules.check_oncall(n)))

    def test_cannot_be_in_both_sites_pairs_same_night(self):
        OnCallNight.objects.create(location=self.bur, date=MON, staff_one=self.graham, staff_two=self.danny)
        n = self.night(self.tvl, one=self.graham, two=self.natalie, phones=self.robin)
        self.assertTrue(any("already on the Ayr" in m for m in self.messages_of(rules.check_oncall(n), rules.ERROR)))

    def test_friday_night_and_saturday_do_not_clash(self):
        fri = MON + timedelta(days=4)
        OnCallNight.objects.create(location=self.tvl, date=fri, staff_one=self.robin, staff_two=self.natalie)
        n = self.night(self.tvl, d=SAT, one=self.robin, two=self.natalie, phones=self.robin)
        self.assertFalse(rules.errors(rules.check_oncall(n)))

    def test_long_run_of_nights_warns(self):
        for i in range(7):
            OnCallNight.objects.create(location=self.tvl, date=MON + timedelta(days=i), staff_one=self.natalie, staff_two=self.robin)
        n = self.night(self.tvl, d=MON + timedelta(days=7), one=self.natalie, two=self.robin, phones=self.robin)
        self.assertTrue(any("nights in a row" in m for m in self.messages_of(rules.check_oncall(n), rules.WARNING)))


class GentleWayRuleTests(RosterTestBase):
    def test_robin_and_shane_can_attend(self):
        for p in (self.robin, self.shane):
            att = GentleWayAttendance(location=self.tvl, date=MON, start=time(23), staff=p)
            self.assertFalse(rules.errors(rules.check_gentle_way(att)), p)

    def test_without_the_skill_is_an_error(self):
        att = GentleWayAttendance(location=self.tvl, date=MON, start=time(23), staff=self.jarrod)
        self.assertTrue(rules.errors(rules.check_gentle_way(att)))

    def test_unassigned_warns(self):
        att = GentleWayAttendance(location=self.tvl, date=MON, start=time(23))
        self.assertEqual(rules.check_gentle_way(att)[0].level, rules.WARNING)


class ViewTests(RosterTestBase):
    def login(self, staff):
        self.client.force_login(staff.user)

    def week_url(self, d=MON, qs=""):
        return reverse("roster:week", args=[d.isoformat()]) + qs

    def publish(self, location, d=MON):
        RosterWeek.objects.update_or_create(location=location, week_start=d, defaults={"published_at": timezone.now()})

    def test_all_pages_render_for_a_director(self):
        self.login(self.robin)
        OnCallNight.objects.create(location=self.tvl, date=MON, staff_one=self.chloe, staff_two=self.james,
                                   support_staff=self.robin, phone_holder=self.robin)
        Shift.objects.create(staff=self.jarrod, location=self.tvl, date=MON, start=time(8), end=time(16, 6), area=self.area["MORTUARY"])
        GentleWayAttendance.objects.create(location=self.tvl, date=MON, start=time(23))
        for url in [
            self.week_url(),
            self.week_url(qs="?loc=BUR"),
            self.week_url(qs="?area=MORTUARY"),
            reverse("roster:print_week", args=[MON.isoformat()]),
            reverse("roster:my_roster"),
            reverse("roster:tonight"),
            reverse("roster:requests"),
            reverse("roster:skills"),
            reverse("roster:skills") + "?edit=1",
            reverse("roster:after_hours_report"),
            reverse("roster:shift_create"),
            reverse("roster:oncall_edit", args=["TVL", MON.isoformat()]),
            reverse("roster:gw_create", args=["TVL"]),
            reverse("roster:unavailability_new"),
            reverse("roster:swap_new"),
            reverse("roster:staff_logins"),
        ]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_both_sites_on_one_page(self):
        Shift.objects.create(staff=self.chloe, location=self.tvl, date=MON, start=time(9), end=time(13), area=self.area["FUNERAL"])
        Shift.objects.create(staff=self.amie, location=self.bur, date=MON, start=time(8, 30), end=time(16, 36), area=self.area["ARRANGE"])
        OnCallNight.objects.create(location=self.tvl, date=MON, staff_one=self.chloe, staff_two=self.james,
                                   support_staff=self.robin, phone_holder=self.robin)
        self.publish(self.tvl)
        self.publish(self.bur)
        self.login(self.danny)  # ordinary staff member at Ayr
        r = self.client.get(self.week_url())
        self.assertContains(r, "Townsville staff")
        self.assertContains(r, "Ayr staff")
        self.assertContains(r, "@ Townsville")  # Chloe helping in Townsville
        self.assertContains(r, "Trainer:")
        self.assertContains(r, "08:30")  # Amie's Ayr shift
        self.assertNotContains(r, "Fill usual shifts")

    def test_unpublished_site_hidden_from_staff(self):
        Shift.objects.create(staff=self.jarrod, location=self.tvl, date=MON, start=time(8), end=time(16, 6), area=self.area["MORTUARY"])
        Shift.objects.create(staff=self.amie, location=self.bur, date=MON, start=time(8, 30), end=time(16, 36), area=self.area["ARRANGE"])
        self.publish(self.bur)
        self.login(self.jarrod)
        r = self.client.get(self.week_url())
        self.assertContains(r, "The Townsville roster for this week hasn't been published")
        self.assertNotContains(r, "16:06")  # Jarrod's draft Townsville shift
        self.assertNotContains(r, "Jarrod Nelson")
        self.assertContains(r, "08:30")

    def test_area_filter(self):
        Shift.objects.create(staff=self.jarrod, location=self.tvl, date=MON, start=time(8), end=time(16, 6), area=self.area["MORTUARY"])
        Shift.objects.create(staff=self.kylie, location=self.tvl, date=MON, start=time(9), end=time(17, 6), area=self.area["ARRANGE"])
        self.login(self.robin)
        r = self.client.get(self.week_url(qs="?area=MORTUARY"))
        self.assertContains(r, "Jarrod Nelson")
        self.assertNotContains(r, "Kylie Young")

    def test_amie_edits_ayr_not_townsville(self):
        self.login(self.amie)
        self.assertEqual(self.client.get(reverse("roster:oncall_edit", args=["BUR", MON.isoformat()])).status_code, 200)
        self.assertEqual(self.client.get(reverse("roster:oncall_edit", args=["TVL", MON.isoformat()])).status_code, 403)
        r = self.client.post(
            reverse("roster:shift_create"),
            {"location": self.tvl.pk, "staff": self.chloe.pk, "area": self.area["FUNERAL"].pk, "date": MON.isoformat(),
             "start": "09:00", "end": "13:00", "unpaid_break_minutes": 0},
        )
        self.assertEqual(r.status_code, 200)  # Townsville isn't one of her sites: form refuses it
        self.assertFalse(Shift.objects.exists())

    def test_natalie_rosters_chloe_to_help_in_townsville(self):
        self.login(self.natalie)
        self.client.post(
            reverse("roster:shift_create"),
            {"location": self.tvl.pk, "staff": self.chloe.pk, "area": self.area["FUNERAL"].pk, "date": MON.isoformat(),
             "start": "09:00", "end": "13:00", "unpaid_break_minutes": 0},
        )
        self.assertTrue(Shift.objects.filter(staff=self.chloe, location=self.tvl).exists())

    def test_shift_form_blocks_area_without_skill(self):
        self.login(self.robin)
        r = self.client.post(
            reverse("roster:shift_create"),
            {"location": self.tvl.pk, "staff": self.alan.pk, "area": self.area["MORTUARY"].pk, "date": MON.isoformat(),
             "start": "08:30", "end": "16:36", "unpaid_break_minutes": 30},
        )
        self.assertContains(r, "Mortuary &amp; care")
        self.assertFalse(Shift.objects.exists())

    def test_after_hours_form_saves_third_person_and_phones(self):
        self.login(self.robin)
        r = self.client.post(
            reverse("roster:oncall_edit", args=["TVL", MON.isoformat()]),
            {"staff_one": self.chloe.pk, "staff_two": self.james.pk, "support_staff": self.robin.pk,
             "support_role": "TRAINER", "phone_holder": self.robin.pk, "note": ""},
        )
        self.assertEqual(r.status_code, 302)
        n = OnCallNight.objects.get()
        self.assertEqual((n.support_staff, n.phone_holder), (self.robin, self.robin))

    def test_fill_usual_skips_public_holiday_and_leave(self):
        self.login(self.natalie)
        ph_week = date(2027, 1, 25)  # Tue 26/01/2027 is Australia Day
        UnavailabilityRequest.objects.create(staff=self.jarrod, start_date=ph_week, end_date=ph_week, status=RequestStatus.APPROVED)
        self.client.post(reverse("roster:fill_usual", args=[ph_week.isoformat(), "TVL"]))
        self.assertFalse(Shift.objects.filter(date=date(2027, 1, 26)).exists())
        self.assertFalse(Shift.objects.filter(staff=self.jarrod, date=ph_week).exists())
        # 7 full-timers with usual shifts x 4 working days, less Jarrod's Monday
        self.assertEqual(Shift.objects.filter(location=self.tvl).count(), 7 * 4 - 1)
        self.assertEqual(Shift.objects.get(staff=self.jarrod, date=ph_week + timedelta(days=2)).area.code, "MORTUARY")

    def test_copy_after_hours_copies_third_person_and_phones(self):
        prev = MON - timedelta(days=7)
        OnCallNight.objects.create(location=self.tvl, date=prev, staff_one=self.chloe, staff_two=self.james,
                                   support_staff=self.robin, support_role="SUPERVISOR", phone_holder=self.robin)
        self.login(self.robin)
        self.client.post(reverse("roster:copy_oncall", args=[MON.isoformat(), "TVL"]))
        n = OnCallNight.objects.get(date=MON)
        self.assertEqual((n.support_staff, n.support_role, n.phone_holder), (self.robin, "SUPERVISOR", self.robin))

    def test_publish_blocked_by_errors_unless_overridden(self):
        self.login(self.robin)
        OnCallNight.objects.create(location=self.tvl, date=MON, staff_one=self.alan, staff_two=self.natalie)
        url = reverse("roster:publish", args=[MON.isoformat(), "TVL"])
        self.client.post(url)
        self.assertFalse(RosterWeek.objects.filter(published_at__isnull=False).exists())
        self.client.post(url, {"override": "1"})
        self.assertTrue(RosterWeek.objects.filter(published_at__isnull=False).exists())

    def test_publish_emails_everyone_involved(self):
        self.jarrod.email = "jarrod@example.com"
        self.jarrod.save()
        Shift.objects.create(staff=self.jarrod, location=self.tvl, date=MON, start=time(8), end=time(16, 6), area=self.area["MORTUARY"])
        OnCallNight.objects.create(location=self.tvl, date=MON, staff_one=self.chloe, staff_two=self.james,
                                   support_staff=self.robin, phone_holder=self.robin)
        self.login(self.robin)
        r = self.client.post(reverse("roster:publish", args=[MON.isoformat(), "TVL"]), follow=True)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["jarrod@example.com", "robin@radlea.com.au"])
        robin_mail = [m for m in mail.outbox if m.to == ["robin@radlea.com.au"]][0]
        self.assertIn("AFTER HOURS (trainer, has the phones) 17:00 to 08:00 Tue with Chloe Taiters and James Brennan", robin_mail.body)
        self.assertIn("Mortuary & care", [m for m in mail.outbox if m.to == ["jarrod@example.com"]][0].body)
        self.assertContains(r, "James Brennan")  # listed as having no email

    def test_skills_page_directors_edit_staff_view(self):
        self.login(self.jarrod)
        self.assertNotContains(self.client.get(reverse("roster:skills")), "Edit skills")
        self.assertEqual(self.client.post(reverse("roster:skills"), {}).status_code, 403)
        self.login(self.robin)
        skill = Skill.objects.get(code=Skill.MORTUARY)
        field = f"s{self.alan.pk}_{skill.pk}"
        form = self.client.get(reverse("roster:skills") + "?edit=1").context["form"]
        data = {name: (f.initial or "") for name, f in form.fields.items()}
        data[field] = StaffSkill.TRAINEE
        self.client.post(reverse("roster:skills"), data)
        self.assertEqual(StaffSkill.objects.get(staff=self.alan, skill=skill).level, StaffSkill.TRAINEE)
        self.assertTrue(ChangeLog.objects.filter(action="skills changed").exists())
        self.assertEqual(StaffSkill.objects.filter(staff=self.chloe).count(), 1)  # untouched

    def test_after_hours_report_counts_roles(self):
        today = timezone.localdate()
        OnCallNight.objects.create(location=self.tvl, date=today, staff_one=self.chloe, staff_two=self.james,
                                   support_staff=self.robin, phone_holder=self.robin)
        self.login(self.robin)
        rows = {r["staff"].first_name: r for r in self.client.get(reverse("roster:after_hours_report")).context["rows"]}
        self.assertEqual((rows["Chloe"]["total"], rows["Robin"]["third"], rows["Robin"]["phones"], rows["Robin"]["total"]), (1, 1, 1, 1))
        self.login(self.jarrod)
        self.assertEqual(self.client.get(reverse("roster:after_hours_report")).status_code, 403)

    def test_swap_request_and_approval(self):
        today = timezone.localdate()
        night = OnCallNight.objects.create(location=self.tvl, date=today + timedelta(days=2), staff_one=self.natalie,
                                           staff_two=self.robin, phone_holder=self.natalie)
        ws = today - timedelta(days=today.weekday())
        self.publish(self.tvl, ws)
        self.publish(self.tvl, ws + timedelta(days=7))
        self.login(self.natalie)
        self.client.post(reverse("roster:swap_new"), {"item": f"oncall:{night.pk}", "proposed_staff": self.amie.pk, "reason": "Family event"})
        req = SwapRequest.objects.get()
        self.login(self.robin)
        self.client.post(reverse("roster:decide_swap", args=[req.pk]), {"action": "approve"})
        night.refresh_from_db()
        req.refresh_from_db()
        # Amie can't work in Townsville, so the swap is refused.
        self.assertEqual(req.status, RequestStatus.PENDING)
        self.client.post(reverse("roster:decide_swap", args=[req.pk]), {"action": "approve", "replacement": self.chloe.pk})
        night.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual(req.status, RequestStatus.APPROVED)
        # Natalie handed over both her roles that night: the pair and the phones.
        self.assertEqual((night.staff_one, night.phone_holder), (self.chloe, self.chloe))

    def test_unavailability_request_flow(self):
        self.login(self.danny)
        self.client.post(reverse("roster:unavailability_new"), {"kind": "UNAVAIL", "start_date": MON.isoformat(), "end_date": TUE.isoformat()})
        req = UnavailabilityRequest.objects.get()
        self.login(self.natalie)  # Townsville editor: not Danny's manager
        self.client.post(reverse("roster:decide_unavailability", args=[req.pk]), {"action": "approve"})
        req.refresh_from_db()
        self.assertEqual(req.status, RequestStatus.PENDING)
        self.login(self.amie)
        self.client.post(reverse("roster:decide_unavailability", args=[req.pk]), {"action": "approve"})
        req.refresh_from_db()
        self.assertEqual(req.status, RequestStatus.APPROVED)

    def test_staff_cannot_edit(self):
        self.login(self.jarrod)
        self.assertEqual(self.client.get(reverse("roster:oncall_edit", args=["TVL", MON.isoformat()])).status_code, 403)
        self.assertEqual(self.client.get(reverse("roster:shift_create")).status_code, 403)
        self.assertEqual(self.client.post(reverse("roster:publish", args=[MON.isoformat(), "TVL"])).status_code, 403)

    def test_weekly_hours_warning_shown_once(self):
        for i in range(6):
            Shift.objects.create(staff=self.jarrod, location=self.tvl, date=MON + timedelta(days=i), start=time(8), end=time(16, 6), area=self.area["MORTUARY"])
        hours = [i for _, i in rules.week_issues(self.tvl, MON) if "ordinary hours" in i.message]
        self.assertEqual(len(hours), 1)

    def test_on_call_now_shows_phones_and_unpublished(self):
        self.login(self.jarrod)
        self.assertContains(self.client.get(reverse("roster:tonight")), "hasn't been published")

    def test_director_creates_and_resets_logins(self):
        StaffMember.objects.filter(pk=self.james.pk).update(user=None)
        self.login(self.robin)
        r = self.client.post(reverse("roster:staff_logins"), {"staff": self.james.pk, "action": "create"})
        pw = r.context["shown"]["password"]
        self.james.refresh_from_db()
        self.assertIsNotNone(authenticate(username=self.james.user.username, password=pw))

    def test_only_directors_manage_logins(self):
        self.login(self.natalie)
        self.assertEqual(self.client.get(reverse("roster:staff_logins")).status_code, 403)


class BootstrapDirectorTests(TestCase):
    def test_creates_robin_once_from_environment(self):
        call_command("seed_radlea", stdout=StringIO())
        with mock.patch.dict(os.environ, {"RADLEA_DIRECTOR_PASSWORD": "Correct-Horse-27"}):
            call_command("bootstrap_director", stdout=StringIO())
            robin = StaffMember.objects.get(first_name="Robin")
            self.assertTrue(robin.user.is_superuser)
            robin.user.set_password("Changed-Later-99")
            robin.user.save()
            call_command("bootstrap_director", stdout=StringIO())  # must not reset it
        self.assertIsNotNone(authenticate(username="robin.drury", password="Changed-Later-99"))


class HostingTests(TestCase):
    def test_health_check_answers_without_a_host_name(self):
        r = self.client.get("/healthz", HTTP_HOST="10.244.0.7:8080")
        self.assertEqual((r.status_code, r.content), (200, b"ok"))
