"""
Tests for the roster rules that matter most in practice:
who can be rostered where, the after-hours pair, leave blocking, Gentle Way
training, swaps, publishing and who can see what.

Run:  python manage.py test
"""
from datetime import date, time, timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from org.models import Location, StaffMember

from . import rules
from .models import (
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
        get = lambda f, l: StaffMember.objects.get(first_name=f, last_name=l)  # noqa: E731
        cls.natalie = get("Natalie", "Miller")
        cls.shane = get("Shane", "Marshall")
        cls.jarrod = get("Jarrod", "Nelson")
        cls.james = get("James", "Brennan")
        cls.amie = get("Amie", "Griggs")
        cls.danny = get("Danny", "Parfitt")
        cls.chloe = get("Chloe", "Taiters")
        cls.robin = get("Robin", "Drury")
        cls.leanne = get("Leanne", "Drury")

    def shift(self, staff, d=MON, start=time(9), end=time(17, 6), location=None, **kw):
        return Shift(staff=staff, location=location or staff.home_location, date=d, start=start, end=end, **kw)

    def messages_of(self, issues, level=None):
        return [i.message for i in issues if level is None or i.level == level]


class SeedTests(RosterTestBase):
    def test_staff_loaded_from_register(self):
        self.assertEqual(StaffMember.objects.filter(home_location=self.tvl, on_roster=True).exclude(employment_type="DIR").count(), 8)
        self.assertEqual(StaffMember.objects.filter(home_location=self.bur).count(), 5)
        self.assertTrue(self.chloe.can_work_at(self.tvl))
        self.assertFalse(self.danny.can_work_at(self.tvl))
        self.assertFalse(self.leanne.on_roster)

    def test_gentle_way_operators_are_shane_and_robin(self):
        trained = set(StaffMember.objects.filter(gentle_way_trained=True).values_list("first_name", flat=True))
        self.assertEqual(trained, {"Shane", "Robin"})

    def test_seed_is_repeatable_and_never_overwrites_setup_changes(self):
        self.jarrod.role_title = "Senior FDA"
        self.jarrod.can_do_transfers = False
        self.jarrod.save()
        self.tvl.weeknight_standby_start = time(18)
        self.tvl.save()
        call_command("seed_radlea", stdout=StringIO())
        self.assertEqual(StaffMember.objects.count(), 15)
        self.jarrod.refresh_from_db()
        self.tvl.refresh_from_db()
        self.assertEqual((self.jarrod.role_title, self.jarrod.can_do_transfers), ("Senior FDA", False))
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
        self.assertEqual(rules.errors(rules.check_shift(self.shift(self.jarrod))), [])

    def test_overlap_is_an_error(self):
        self.shift(self.jarrod).save()
        issues = rules.check_shift(self.shift(self.jarrod, start=time(16), end=time(18)))
        self.assertTrue(any("Overlaps" in m for m in self.messages_of(issues, rules.ERROR)))

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
    def test_pair_is_fine(self):
        n = OnCallNight(location=self.tvl, date=MON, staff_one=self.robin, staff_two=self.james)
        self.assertEqual(rules.check_oncall(n), [])

    def test_same_person_twice(self):
        n = OnCallNight(location=self.tvl, date=MON, staff_one=self.james, staff_two=self.james)
        self.assertTrue(rules.errors(rules.check_oncall(n)))

    def test_one_person_warns(self):
        n = OnCallNight(location=self.tvl, date=MON, staff_one=self.james)
        issues = rules.check_oncall(n)
        self.assertFalse(rules.errors(issues))
        self.assertTrue(any("Only one person" in m for m in self.messages_of(issues)))

    def test_not_transfer_capable(self):
        n = OnCallNight(location=self.tvl, date=MON, staff_one=self.leanne, staff_two=self.james)
        self.assertTrue(rules.errors(rules.check_oncall(n)))

    def test_cannot_be_on_both_locations_same_night(self):
        OnCallNight.objects.create(location=self.bur, date=MON, staff_one=self.chloe, staff_two=self.danny)
        n = OnCallNight(location=self.tvl, date=MON, staff_one=self.chloe, staff_two=self.james)
        self.assertTrue(any("already on the Ayr" in m for m in self.messages_of(rules.check_oncall(n), rules.ERROR)))

    def test_friday_night_and_saturday_do_not_clash(self):
        fri = MON + timedelta(days=4)
        OnCallNight.objects.create(location=self.tvl, date=fri, staff_one=self.james, staff_two=self.natalie)
        n = OnCallNight(location=self.tvl, date=SAT, staff_one=self.james, staff_two=self.natalie)
        self.assertFalse(rules.errors(rules.check_oncall(n)))

    def test_long_run_of_nights_warns(self):
        for i in range(7):
            OnCallNight.objects.create(location=self.tvl, date=MON + timedelta(days=i), staff_one=self.james, staff_two=self.robin)
        n = OnCallNight(location=self.tvl, date=MON + timedelta(days=7), staff_one=self.james, staff_two=self.natalie)
        self.assertTrue(any("nights in a row" in m for m in self.messages_of(rules.check_oncall(n), rules.WARNING)))


class GentleWayRuleTests(RosterTestBase):
    def test_robin_can_be_rostered_as_second_operator(self):
        att = GentleWayAttendance(location=self.tvl, date=MON, start=time(23), staff=self.robin)
        self.assertFalse(rules.errors(rules.check_gentle_way(att)))

    def test_untrained_is_an_error_trained_is_fine(self):
        att = GentleWayAttendance(location=self.tvl, date=MON, start=time(23), staff=self.jarrod)
        self.assertTrue(rules.errors(rules.check_gentle_way(att)))
        self.jarrod.gentle_way_trained = True
        self.jarrod.save()
        self.assertFalse(rules.errors(rules.check_gentle_way(att)))

    def test_unassigned_warns(self):
        att = GentleWayAttendance(location=self.tvl, date=MON, start=time(23))
        self.assertEqual(rules.check_gentle_way(att)[0].level, rules.WARNING)


class ViewTests(RosterTestBase):
    def login(self, staff):
        self.client.force_login(staff.user)

    def week_url(self, loc, d=MON):
        return reverse("roster:week", args=[loc.code, d.isoformat()])

    def test_all_pages_render_for_a_director(self):
        self.login(self.robin)
        OnCallNight.objects.create(location=self.tvl, date=MON, staff_one=self.robin, staff_two=self.james)
        Shift.objects.create(staff=self.jarrod, location=self.tvl, date=MON, start=time(8), end=time(16, 6))
        GentleWayAttendance.objects.create(location=self.tvl, date=MON, start=time(23))
        for url in [
            self.week_url(self.tvl),
            self.week_url(self.bur),
            reverse("roster:print_week", args=["TVL", MON.isoformat()]),
            reverse("roster:my_roster"),
            reverse("roster:tonight"),
            reverse("roster:requests"),
            reverse("roster:shift_create", args=["TVL"]),
            reverse("roster:oncall_edit", args=["TVL", MON.isoformat()]),
            reverse("roster:gw_create", args=["TVL"]),
            reverse("roster:unavailability_new"),
            reverse("roster:swap_new"),
        ]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_staff_cannot_see_draft_week(self):
        Shift.objects.create(staff=self.jarrod, location=self.tvl, date=MON, start=time(8), end=time(16, 6))
        self.login(self.jarrod)
        r = self.client.get(self.week_url(self.tvl))
        self.assertContains(r, "hasn't been published")
        RosterWeek.objects.create(location=self.tvl, week_start=MON, published_at=timezone.now())
        r = self.client.get(self.week_url(self.tvl))
        self.assertContains(r, "08:00")
        self.assertNotContains(r, "Fill usual shifts")

    def test_amie_edits_ayr_not_townsville(self):
        self.login(self.amie)
        self.assertEqual(self.client.get(reverse("roster:shift_create", args=["BUR"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("roster:shift_create", args=["TVL"])).status_code, 403)

    def test_fill_usual_skips_public_holiday_and_leave(self):
        self.login(self.natalie)
        ph_week = date(2027, 1, 25)  # Tue 26/01/2027 is Australia Day
        UnavailabilityRequest.objects.create(
            staff=self.jarrod, start_date=ph_week, end_date=ph_week, status=RequestStatus.APPROVED
        )
        self.client.post(reverse("roster:fill_usual", args=["TVL", ph_week.isoformat()]))
        self.assertFalse(Shift.objects.filter(date=date(2027, 1, 26)).exists())
        self.assertFalse(Shift.objects.filter(staff=self.jarrod, date=ph_week).exists())
        # 7 full-timers with usual shifts x 4 working days, less Jarrod's Monday
        self.assertEqual(Shift.objects.filter(location=self.tvl).count(), 7 * 4 - 1)

    def test_save_shift_via_form_blocks_errors(self):
        self.login(self.robin)
        Shift.objects.create(staff=self.jarrod, location=self.tvl, date=MON, start=time(8), end=time(16, 6))
        r = self.client.post(
            reverse("roster:shift_create", args=["TVL"]),
            {"staff": self.jarrod.pk, "date": MON.isoformat(), "start": "15:00", "end": "18:00",
             "unpaid_break_minutes": 0, "shift_type": "SVC"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Overlaps")
        self.assertEqual(Shift.objects.count(), 1)

    def test_publish_blocked_by_errors_unless_overridden(self):
        self.login(self.robin)
        OnCallNight.objects.create(location=self.tvl, date=MON, staff_one=self.leanne, staff_two=self.james)
        url = reverse("roster:publish", args=["TVL", MON.isoformat()])
        self.client.post(url)
        self.assertFalse(RosterWeek.objects.filter(published_at__isnull=False).exists())
        self.client.post(url, {"override": "1"})
        self.assertTrue(RosterWeek.objects.filter(published_at__isnull=False).exists())

    def test_swap_request_and_approval(self):
        today = timezone.localdate()
        night = OnCallNight.objects.create(location=self.tvl, date=today + timedelta(days=2), staff_one=self.james, staff_two=self.robin)
        RosterWeek.objects.create(location=self.tvl, week_start=today - timedelta(days=today.weekday()), published_at=timezone.now())
        RosterWeek.objects.create(location=self.tvl, week_start=today - timedelta(days=today.weekday()) + timedelta(days=7), published_at=timezone.now())
        self.login(self.james)
        self.client.post(
            reverse("roster:swap_new"),
            {"item": f"oncall:{night.pk}", "proposed_staff": self.natalie.pk, "reason": "Family event"},
        )
        req = SwapRequest.objects.get()
        self.login(self.natalie)
        self.client.post(reverse("roster:decide_swap", args=[req.pk]), {"action": "approve"})
        night.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual(req.status, RequestStatus.APPROVED)
        self.assertEqual(night.staff_one, self.natalie)

    def test_swap_refused_when_replacement_unavailable(self):
        d = timezone.localdate() + timedelta(days=3)
        shift = Shift.objects.create(staff=self.jarrod, location=self.tvl, date=d, start=time(8), end=time(16, 6))
        UnavailabilityRequest.objects.create(staff=self.shane, start_date=d, end_date=d, status=RequestStatus.APPROVED)
        req = SwapRequest.objects.create(requester=self.jarrod, shift=shift, proposed_staff=self.shane)
        self.login(self.robin)
        self.client.post(reverse("roster:decide_swap", args=[req.pk]), {"action": "approve"})
        shift.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual(shift.staff, self.jarrod)
        self.assertEqual(req.status, RequestStatus.PENDING)

    def test_unavailability_request_flow(self):
        self.login(self.danny)
        self.client.post(
            reverse("roster:unavailability_new"),
            {"kind": "UNAVAIL", "start_date": MON.isoformat(), "end_date": TUE.isoformat()},
        )
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
        self.assertEqual(self.client.post(reverse("roster:publish", args=["TVL", MON.isoformat()])).status_code, 403)

    def test_weekly_hours_warning_shown_once(self):
        for i in range(6):
            Shift.objects.create(staff=self.jarrod, location=self.tvl, date=MON + timedelta(days=i), start=time(8), end=time(16, 6))
        hours = [i for _, i in rules.week_issues(self.tvl, MON) if "ordinary hours" in i.message]
        self.assertEqual(len(hours), 1)

    def test_on_call_now_says_unpublished(self):
        self.login(self.jarrod)
        self.assertContains(self.client.get(reverse("roster:tonight")), "hasn't been published")

    def test_publish_emails_rostered_staff_with_an_address(self):
        from django.core import mail

        self.jarrod.email = "jarrod@example.com"
        self.jarrod.save()
        Shift.objects.create(staff=self.jarrod, location=self.tvl, date=MON, start=time(8), end=time(16, 6))
        OnCallNight.objects.create(location=self.tvl, date=MON, staff_one=self.robin, staff_two=self.james)
        self.login(self.robin)
        r = self.client.post(reverse("roster:publish", args=["TVL", MON.isoformat()]), follow=True)
        to = sorted(m.to[0] for m in mail.outbox)
        self.assertEqual(to, ["jarrod@example.com", "robin@radlea.com.au"])
        robin_mail = [m for m in mail.outbox if m.to == ["robin@radlea.com.au"]][0]
        self.assertIn("AFTER HOURS 17:00 to 08:00 Tue with James Brennan", robin_mail.body)
        self.assertContains(r, "James Brennan")  # listed as having no email
        mail.outbox.clear()
        self.client.post(reverse("roster:publish", args=["TVL", MON.isoformat()]))
        self.assertTrue(mail.outbox[0].subject.startswith("UPDATED"))

    def test_director_creates_and_resets_logins(self):
        from django.contrib.auth import authenticate

        StaffMember.objects.filter(pk=self.james.pk).update(user=None)
        self.login(self.robin)
        r = self.client.post(reverse("roster:staff_logins"), {"staff": self.james.pk, "action": "create"})
        pw = r.context["shown"]["password"]
        self.james.refresh_from_db()
        self.assertIsNotNone(authenticate(username=self.james.user.username, password=pw))
        r = self.client.post(reverse("roster:staff_logins"), {"staff": self.james.pk, "action": "reset"})
        self.assertIsNotNone(authenticate(username=self.james.user.username, password=r.context["shown"]["password"]))

    def test_only_directors_manage_logins(self):
        self.login(self.natalie)
        self.assertEqual(self.client.get(reverse("roster:staff_logins")).status_code, 403)

    def test_change_log_written(self):
        self.login(self.robin)
        self.client.post(
            reverse("roster:oncall_edit", args=["TVL", MON.isoformat()]),
            {"staff_one": self.robin.pk, "staff_two": self.james.pk, "note": ""},
        )
        from .models import ChangeLog

        self.assertTrue(ChangeLog.objects.filter(action="after hours set").exists())


class BootstrapDirectorTests(TestCase):
    def test_creates_robin_once_from_environment(self):
        import os
        from unittest import mock

        from django.contrib.auth import authenticate

        call_command("seed_radlea", stdout=StringIO())
        with mock.patch.dict(os.environ, {"RADLEA_DIRECTOR_PASSWORD": "Correct-Horse-27"}):
            call_command("bootstrap_director", stdout=StringIO())
            robin = StaffMember.objects.get(first_name="Robin")
            self.assertTrue(robin.user.is_superuser)
            self.assertIsNotNone(authenticate(username="robin.drury", password="Correct-Horse-27"))
            robin.user.set_password("Changed-Later-99")
            robin.user.save()
            call_command("bootstrap_director", stdout=StringIO())  # must not reset it
        self.assertIsNotNone(authenticate(username="robin.drury", password="Changed-Later-99"))
