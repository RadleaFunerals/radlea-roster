"""Who can do what.

Directors (Django superusers) edit every location. Anyone else edits only the
locations listed in their staff record's "editor locations" (Natalie for
Townsville, Amie for Ayr by default). Everyone with a login can see
published rosters and make requests.
"""
from django.core.exceptions import PermissionDenied

from org.models import Location, StaffMember


def current_staff(user):
    if not user.is_authenticated:
        return None
    try:
        return user.staff
    except StaffMember.DoesNotExist:
        return None


def editable_locations(user):
    if not user.is_authenticated:
        return Location.objects.none()
    if user.is_superuser:
        return Location.objects.filter(active=True)
    staff = current_staff(user)
    if staff is None:
        return Location.objects.none()
    return staff.editor_locations.filter(active=True)


def can_edit(user, location):
    return editable_locations(user).filter(pk=location.pk).exists()


def require_editor(user, location):
    if not can_edit(user, location):
        raise PermissionDenied("You can't edit the roster for this location.")
