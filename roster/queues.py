"""Which staff requests each roster editor is responsible for."""
from django.db.models import Q

from .models import RequestStatus, SwapRequest, UnavailabilityRequest


def unavailability_for(location_ids, status=RequestStatus.PENDING):
    qs = UnavailabilityRequest.objects.filter(
        Q(staff__home_location_id__in=location_ids) | Q(staff__extra_locations__in=location_ids)
    ).distinct()
    if status:
        qs = qs.filter(status=status)
    return qs.select_related("staff", "staff__home_location", "decided_by")


def swaps_for(location_ids, status=RequestStatus.PENDING):
    qs = SwapRequest.objects.select_related(
        "requester",
        "proposed_staff",
        "decided_by",
        "shift__location",
        "oncall__location",
        "gentle_way__location",
    )
    if status:
        qs = qs.filter(status=status)
    return [s for s in qs if s.location.pk in set(location_ids)]
