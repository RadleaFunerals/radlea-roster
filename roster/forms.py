import copy

from django import forms
from django.db.models import Q

from org.models import Area, Location, Skill, StaffMember

from . import rules
from .models import GentleWayAttendance, OnCallNight, Shift, UnavailabilityRequest

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
TIME = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")


def staff_for_location(location):
    return (
        StaffMember.objects.filter(active=True, on_roster=True)
        .filter(Q(home_location=location) | Q(extra_locations=location))
        .distinct()
    )


def with_skill(qs, code):
    return qs.filter(skills__skill__code=code).distinct()


class RuleCheckedForm(forms.ModelForm):
    """ModelForm that runs the rostering rules before saving.

    Errors block the save; warnings are kept on the form so the view can
    show them after saving.
    """

    rule_check = None

    def __init__(self, *args, location=None, **kwargs):
        self.location = location
        super().__init__(*args, **kwargs)
        self.rule_warnings = []

    def candidate(self, cd):
        cand = copy.copy(self.instance)
        for name in self._meta.fields:
            if name in cd:
                setattr(cand, name, cd[name])
        if self.location is not None:
            cand.location = self.location
        return cand

    def clean(self):
        cd = super().clean()
        if self.errors:
            return cd
        issues = type(self).rule_check(self.candidate(cd))
        errs = [i.message for i in rules.errors(issues)]
        if errs:
            raise forms.ValidationError(errs)
        self.rule_warnings = [i.message for i in rules.warnings(issues)]
        return cd


class ShiftForm(RuleCheckedForm):
    """A shift at either site. The site list only offers sites the user can edit."""

    rule_check = staticmethod(rules.check_shift)

    class Meta:
        model = Shift
        fields = ["location", "staff", "area", "date", "start", "end", "unpaid_break_minutes", "note"]
        widgets = {"date": DATE, "start": TIME, "end": TIME}
        labels = {"location": "Site", "area": "Business area", "end": "Finish", "unpaid_break_minutes": "Unpaid break (minutes)"}

    def __init__(self, *args, editable=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = editable if editable is not None else Location.objects.filter(active=True)
        self.fields["location"].empty_label = None
        self.fields["staff"].queryset = StaffMember.objects.filter(active=True, on_roster=True).select_related(
            "home_location"
        )
        self.fields["staff"].label_from_instance = lambda s: f"{s.display_name} ({s.home_location})"
        self.fields["area"].queryset = Area.objects.filter(active=True)
        self.fields["area"].empty_label = None


class OnCallForm(RuleCheckedForm):
    rule_check = staticmethod(rules.check_oncall)

    class Meta:
        model = OnCallNight
        fields = ["staff_one", "staff_two", "support_staff", "support_role", "phone_holder", "note"]
        labels = {"support_role": "Third person's role"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        team = with_skill(staff_for_location(self.location), Skill.AH_TRANSFER)
        self.fields["staff_one"].queryset = team
        self.fields["staff_two"].queryset = team
        self.fields["support_staff"].queryset = with_skill(
            StaffMember.objects.filter(active=True, on_roster=True), Skill.AH_TRANSFER
        )
        self.fields["support_staff"].empty_label = "None"
        # Anyone can carry the phones, from either site (e.g. a director holding both).
        self.fields["phone_holder"].queryset = StaffMember.objects.filter(active=True)
        for f in ("staff_one", "staff_two", "support_staff", "phone_holder"):
            self.fields[f].label_from_instance = self._label

    def _label(self, s):
        level = s.skill_level(Skill.AH_TRANSFER)
        tag = {"TRAINEE": " - trainee", "TRAINER": " - trainer"}.get(level, "")
        return f"{s.display_name} ({s.home_location}){tag}"

    def candidate(self, cd):
        cand = super().candidate(cd)
        cand.date = self.instance.date
        return cand


class GentleWayForm(RuleCheckedForm):
    rule_check = staticmethod(rules.check_gentle_way)

    class Meta:
        model = GentleWayAttendance
        fields = ["date", "start", "expected_minutes", "process_type", "staff", "case_reference", "status", "note"]
        widgets = {"date": DATE, "start": TIME}
        labels = {"start": "Attend at", "expected_minutes": "Expected time on site (minutes)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff"].queryset = with_skill(staff_for_location(self.location), Skill.GENTLE_WAY)
        self.fields["staff"].required = False
        self.fields["staff"].empty_label = "Unassigned"


class UnavailabilityForm(forms.ModelForm):
    class Meta:
        model = UnavailabilityRequest
        fields = ["kind", "start_date", "end_date", "start_time", "end_time", "note"]
        widgets = {"start_date": DATE, "end_date": DATE, "start_time": TIME, "end_time": TIME}
        labels = {
            "kind": "Type",
            "start_date": "From",
            "end_date": "To",
            "start_time": "From time (optional)",
            "end_time": "To time (optional)",
        }

    def clean(self):
        cd = super().clean()
        sd, ed = cd.get("start_date"), cd.get("end_date")
        if sd and ed and ed < sd:
            raise forms.ValidationError("The 'To' date is before the 'From' date.")
        st, et = cd.get("start_time"), cd.get("end_time")
        if bool(st) != bool(et):
            raise forms.ValidationError("Enter both times, or leave both blank for whole days.")
        if st and et and sd == ed and et <= st:
            raise forms.ValidationError("The 'To' time is before the 'From' time.")
        return cd


class SwapForm(forms.Form):
    item = forms.ChoiceField(label="What do you want to swap or give away?")
    proposed_staff = forms.ModelChoiceField(
        queryset=StaffMember.objects.none(),
        required=False,
        label="Who has agreed to take it? (optional)",
        empty_label="No one yet - manager to arrange",
    )
    reason = forms.CharField(max_length=200, required=False)

    def __init__(self, *args, choices=(), staff=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].choices = choices
        qs = StaffMember.objects.filter(active=True, on_roster=True)
        if staff is not None:
            qs = qs.exclude(pk=staff.pk)
        self.fields["proposed_staff"].queryset = qs


class DecisionForm(forms.Form):
    action = forms.ChoiceField(choices=[("approve", "Approve"), ("decline", "Decline")])
    replacement = forms.ModelChoiceField(
        queryset=StaffMember.objects.filter(active=True, on_roster=True), required=False
    )
    note = forms.CharField(max_length=200, required=False)


class SkillsMatrixForm(forms.Form):
    """One dropdown per person per skill. Field names are s<staff id>_<skill id>."""

    def __init__(self, *args, people=(), skills=(), current=None, **kwargs):
        super().__init__(*args, **kwargs)
        from org.models import StaffSkill

        choices = [("", "-")] + StaffSkill.LEVEL_CHOICES
        current = current or {}
        for p in people:
            for sk in skills:
                name = f"s{p.pk}_{sk.pk}"
                self.fields[name] = forms.ChoiceField(choices=choices, required=False, initial=current.get((p.pk, sk.pk), ""))
