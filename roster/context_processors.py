from org.models import Location

from .permissions import current_staff, editable_locations
from .queues import swaps_for, unavailability_for


def nav(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    editable = list(editable_locations(user))
    pending = 0
    if editable:
        ids = [loc.pk for loc in editable]
        pending = unavailability_for(ids).count() + len(swaps_for(ids))
    return {
        "nav_locations": Location.objects.filter(active=True),
        "nav_editable": editable,
        "nav_pending": pending,
        "nav_staff": current_staff(user),
    }
