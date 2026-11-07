import copy

from django import forms
from django.db.models import Q

from org.models import StaffMember

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
    rule_check = staticmethod(rules.check_shift)

    class Meta:
        model = Shift
        fields = ["staff", "date", "start", "end", "unpaid_break_minutes", "shift_type", "note"]
        widgets = {"date": DATE, "start": TIME, "end": TIME}
        labels = {"end": "Finish", "unpaid_break_minutes": "Unpaid break (minutes)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.location is not None:
            self.fields["staff"].queryset = staff_for_location(self.location)


class OnCallForm(RuleCheckedForm):
    rule_check = staticmethod(rules.check_oncall)

    class Meta:
        model = OnCallNight
        fields = ["staff_one", "staff_two", "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = staff_for_location(self.location).filter(can_do_transfers=True)
        self.fields["staff_one"].queryset = qs
        self.fields["staff_two"].queryset = qs

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
        self.fields["staff"].queryset = staff_for_location(self.location).filter(gentle_way_trained=True)
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
