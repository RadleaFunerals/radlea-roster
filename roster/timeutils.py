"""Date and time helpers. Everything is Queensland time (no daylight saving)."""
from datetime import date, datetime, time, timedelta

from django.utils import timezone


def aware(d: date, t: time) -> datetime:
    return timezone.make_aware(datetime.combine(d, t), timezone.get_default_timezone())


def interval(d: date, start: time, end: time):
    """Start/end as aware datetimes. A finish at or before the start runs past midnight."""
    s = aware(d, start)
    e = aware(d, end)
    if e <= s:
        e += timedelta(days=1)
    return s, e


def overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def week_start(d: date) -> date:
    """Rosters run Monday to Sunday, matching the pay period."""
    return d - timedelta(days=d.weekday())


def week_days(start: date):
    return [start + timedelta(days=i) for i in range(7)]


def local_date(dt: datetime) -> date:
    return timezone.localtime(dt).date()


def fmt_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")
