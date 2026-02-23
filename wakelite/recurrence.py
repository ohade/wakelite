from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, Iterable, List, Optional

WEEKDAY_MAP = {
    "Mon": 0,
    "Tue": 1,
    "Wed": 2,
    "Thu": 3,
    "Fri": 4,
    "Sat": 5,
    "Sun": 6,
}

_INTERVAL_RE = re.compile(r"^(\d+)\s*(s|m|h)$")
_INTERVAL_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600}


@dataclass(frozen=True)
class TimerRecurrence:
    frequency: str
    clock_time: time
    interval: int = 1
    every_seconds: Optional[int] = None
    weekly_days: Optional[List[int]] = None
    monthly_mode: Optional[str] = None
    day_of_month: Optional[int] = None
    nth: Optional[int] = None
    weekday: Optional[int] = None
    anchor_date: Optional[date] = None
    once_date: Optional[date] = None
    active_hours_start: Optional[time] = None
    active_hours_end: Optional[time] = None


class RecurrenceError(ValueError):
    pass


def parse_interval(value: str, allow_zero: bool = False) -> int:
    """Parse a human-readable interval string to seconds.

    Accepted formats: "10s", "5m", "2h", "90s". Minimum 10 seconds
    (or 0 if allow_zero=True, used for daemon timers).
    """
    m = _INTERVAL_RE.match(value.strip())
    if not m:
        raise RecurrenceError(
            f"Invalid interval format: {value!r}. Use Ns, Nm, or Nh (e.g. '10s', '5m', '2h')"
        )
    amount = int(m.group(1))
    unit = m.group(2)
    if amount < 0:
        raise RecurrenceError("interval amount must be >= 0")
    seconds = amount * _INTERVAL_MULTIPLIERS[unit]
    minimum = 0 if allow_zero else 10
    if seconds < minimum:
        raise RecurrenceError(
            f"interval of {seconds}s is too short — minimum is 10s. "
            "Use '10s' or higher to avoid spin-loops."
        )
    return seconds


def interval_is_due(last_fired_at: Optional[datetime], now: datetime, every_seconds: int) -> bool:
    """Check if an interval timer should fire now.

    Returns True if enough time has elapsed since last_fired_at (or if never fired).
    """
    if last_fired_at is None:
        return True
    elapsed = (now - last_fired_at).total_seconds()
    return elapsed >= every_seconds


def parse_time(value: str) -> time:
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise RecurrenceError(f"Invalid time format: {value}")
    hour = int(parts[0])
    minute = int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    return time(hour=hour, minute=minute, second=second)


def parse_recurrence(timer: Dict) -> TimerRecurrence:
    rec = timer.get("recurrence", {})
    frequency = rec.get("frequency", "daily")
    if frequency not in ("daily", "weekly", "monthly", "once", "interval"):
        raise RecurrenceError("frequency must be daily, weekly, monthly, once, or interval")

    if frequency == "interval":
        every_raw = rec.get("every")
        if not every_raw:
            raise RecurrenceError("'every' is required for interval frequency (e.g. '10s', '5m')")
        is_daemon = timer.get("timer_type") == "daemon"
        every_seconds = parse_interval(every_raw, allow_zero=is_daemon)
        ah_start = ah_end = None
        active_hours = rec.get("active_hours")
        if active_hours:
            if "start" not in active_hours or "end" not in active_hours:
                raise RecurrenceError("active_hours requires both 'start' and 'end' (HH:MM)")
            ah_start = parse_time(active_hours["start"])
            ah_end = parse_time(active_hours["end"])
            if ah_end <= ah_start:
                raise RecurrenceError("active_hours.end must be after active_hours.start")
        return TimerRecurrence(
            frequency="interval",
            clock_time=time(0, 0),
            every_seconds=every_seconds,
            active_hours_start=ah_start,
            active_hours_end=ah_end,
        )

    interval = int(rec.get("interval", 1))
    if interval < 1:
        raise RecurrenceError("interval must be >= 1")
    clock_time = parse_time(rec.get("time", "00:00"))

    anchor_date = None
    anchor = rec.get("anchor_date") or timer.get("created_at")
    if anchor:
        if "T" in anchor:
            anchor_date = datetime.fromisoformat(anchor.replace("Z", "+00:00")).date()
        else:
            anchor_date = datetime.fromisoformat(anchor).date()

    weekly_days = None
    monthly_mode = rec.get("monthly_mode")
    day_of_month = rec.get("day_of_month")
    nth = rec.get("nth")
    weekday = rec.get("weekday")
    once_date = None

    if frequency == "weekly":
        raw_days = rec.get("weekly_days") or ["Mon"]
        weekly_days = [WEEKDAY_MAP[d] if isinstance(d, str) else int(d) for d in raw_days]

    if frequency == "monthly":
        if monthly_mode not in ("day_of_month", "nth_weekday"):
            raise RecurrenceError("monthly_mode must be day_of_month or nth_weekday")
        if monthly_mode == "day_of_month":
            day_of_month = int(day_of_month or 1)
            if not 1 <= day_of_month <= 31:
                raise RecurrenceError("day_of_month must be 1..31")
        else:
            nth = int(nth)
            if nth not in (1, 2, 3, 4, -1):
                raise RecurrenceError("nth must be 1,2,3,4 or -1")
            weekday = WEEKDAY_MAP[weekday] if isinstance(weekday, str) else int(weekday)
            if not 0 <= weekday <= 6:
                raise RecurrenceError("weekday must be 0..6")

    if frequency == "once":
        raw_date = rec.get("date")
        if not raw_date:
            raise RecurrenceError("date is required for once frequency")
        try:
            once_date = date.fromisoformat(str(raw_date))
        except ValueError as exc:
            raise RecurrenceError("date must be YYYY-MM-DD for once frequency") from exc

    return TimerRecurrence(
        frequency=frequency,
        clock_time=clock_time,
        interval=interval,
        weekly_days=weekly_days,
        monthly_mode=monthly_mode,
        day_of_month=day_of_month,
        nth=nth,
        weekday=weekday,
        anchor_date=anchor_date,
        once_date=once_date,
    )


def _month_diff(a: date, b: date) -> int:
    return (a.year - b.year) * 12 + (a.month - b.month)


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _nth_weekday_of_month(year: int, month: int, weekday: int, nth: int) -> date:
    if nth == -1:
        last = date(year, month, _last_day(year, month))
        delta = (last.weekday() - weekday) % 7
        return last - timedelta(days=delta)

    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    first_hit = first + timedelta(days=offset)
    candidate = first_hit + timedelta(weeks=nth - 1)
    if candidate.month != month:
        candidate = first_hit
    return candidate


def matches(recurrence: TimerRecurrence, day: date) -> bool:
    anchor = recurrence.anchor_date or day

    if recurrence.frequency == "daily":
        days = (day - anchor).days
        return days >= 0 and days % recurrence.interval == 0

    if recurrence.frequency == "weekly":
        if recurrence.weekly_days is None or day.weekday() not in recurrence.weekly_days:
            return False
        weeks = (day - anchor).days // 7
        return weeks >= 0 and weeks % recurrence.interval == 0

    if recurrence.frequency == "once":
        return recurrence.once_date == day

    if recurrence.frequency == "monthly":
        months = _month_diff(day, anchor)
        if months < 0 or months % recurrence.interval != 0:
            return False

        if recurrence.monthly_mode == "day_of_month":
            target = recurrence.day_of_month or 1
            day_in_month = min(target, _last_day(day.year, day.month))
            return day.day == day_in_month

        if recurrence.monthly_mode == "nth_weekday":
            expected = _nth_weekday_of_month(day.year, day.month, recurrence.weekday or 0, recurrence.nth or 1)
            return day == expected

    return False


def interval_window_occurrences(recurrence: TimerRecurrence, day: date) -> List[datetime]:
    """Compute all fire times for a windowed interval timer on a given day.

    Returns fire times anchored to active_hours_start, stepping by every_seconds,
    while <= active_hours_end.  E.g. 302m from 07:00 to 22:30 → [07:00, 12:02, 17:04, 22:06].
    """
    if not recurrence.active_hours_start or not recurrence.active_hours_end:
        return []
    every = recurrence.every_seconds or 0
    if every <= 0:
        return []
    start_dt = datetime.combine(day, recurrence.active_hours_start)
    end_dt = datetime.combine(day, recurrence.active_hours_end)
    fires: List[datetime] = []
    cursor = start_dt
    while cursor <= end_dt:
        fires.append(cursor)
        cursor += timedelta(seconds=every)
    return fires


def next_window_occurrence(recurrence: TimerRecurrence, now: datetime) -> Optional[datetime]:
    """Find the next fire time for a windowed interval timer."""
    # Check today's remaining slots
    for occ in interval_window_occurrences(recurrence, now.date()):
        if occ > now:
            return occ
    # Fall through to tomorrow
    tomorrow = now.date() + timedelta(days=1)
    occs = interval_window_occurrences(recurrence, tomorrow)
    return occs[0] if occs else None


def occurrences_between(timer: Dict, start: datetime, end: datetime) -> List[datetime]:
    if end <= start:
        return []

    recurrence = parse_recurrence(timer)
    if recurrence.frequency == "interval":
        if not recurrence.active_hours_start:
            return []  # non-windowed interval timers use a separate scheduling path
        # Windowed interval timers have predictable daily schedules
        results: List[datetime] = []
        cursor = start.date()
        end_date = end.date()
        while cursor <= end_date:
            for occ in interval_window_occurrences(recurrence, cursor):
                if start <= occ <= end:
                    results.append(occ)
            cursor += timedelta(days=1)
        results.sort()
        return results
    results: List[datetime] = []

    # Include one day back to catch an occurrence exactly at start when seconds mismatch.
    cursor = (start - timedelta(days=1)).date()
    end_date = end.date()

    while cursor <= end_date:
        if matches(recurrence, cursor):
            occ = datetime.combine(cursor, recurrence.clock_time)
            if start <= occ <= end:
                results.append(occ)
        cursor += timedelta(days=1)

    results.sort()
    return results


def next_occurrence(timer: Dict, now: datetime, horizon_days: int = 370) -> Optional[datetime]:
    end = now + timedelta(days=horizon_days)
    occurrences = occurrences_between(timer, now, end)
    return occurrences[0] if occurrences else None


def upcoming_occurrences(timer: Dict, now: datetime, horizon_days: int) -> Iterable[datetime]:
    end = now + timedelta(days=horizon_days)
    return occurrences_between(timer, now, end)
