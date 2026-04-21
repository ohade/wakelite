"""Time-axis per-resource capacity projection.

Admission control for timer creation/update: project each timer's resource
consumption onto a time axis, gate when any declared resource's peak concurrent
use would exceed its capacity.

The executor thread pool is modeled as a well-known resource named
`_executor.slot` with capacity equal to `max_workers`. User-declared resources
on timers (`resources[]`) participate in the same engine when their
`capacity` / `estimated_usage` strings are parseable; unparseable values fall
back to the advisory-warning path in `timer_store.check_resource_conflicts`.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .recurrence import RecurrenceError, occurrences_between, parse_interval


DEFAULT_HORIZON_DAYS = 7
DEFAULT_BUCKET_SECONDS = 60
DEFAULT_RUN_DURATION_SECONDS = 60
EXECUTOR_RESOURCE = "_executor.slot"


@dataclass(frozen=True)
class ResourceUsage:
    name: str
    amount: int
    kind: str = "concurrent"  # v1 only; "rate" reserved for sliding-window semantics


_RATE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(?:([A-Za-z%]+)\s*(?:/\s*(s|sec|second|m|min|minute|h|hr|hour))?)?\s*$"
)


def parse_rate(value: Any) -> Optional[int]:
    """Parse a capacity or estimated_usage value into units-per-minute.

    Accepted:
      - int / float: interpreted as "per minute bucket"
      - "50", "50 req/min", "12 req/sec" (720/min), "2 req/h" (~1/min),
        "10 gb" (unitless units, treated as concurrent slot of 10).

    Returns None when unparseable or negative — caller treats None as
    "advisory-only, do not gate".
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    if not isinstance(value, str):
        return None

    m = _RATE_RE.match(value.strip())
    if not m:
        return None
    amount_str, _unit_name, time_unit = m.group(1), m.group(2), m.group(3)
    try:
        amount = float(amount_str)
    except ValueError:
        return None
    if amount < 0:
        return None
    amount_int = int(math.ceil(amount))

    if time_unit is None:
        return amount_int
    tu = time_unit.lower()
    if tu in ("s", "sec", "second"):
        return amount_int * 60
    if tu in ("m", "min", "minute"):
        return amount_int
    if tu in ("h", "hr", "hour"):
        return max(1, amount_int // 60) if amount_int > 0 else 0
    return amount_int


def collect_timer_usage(timer: Dict[str, Any]) -> List[ResourceUsage]:
    """Resources consumed per active run of this timer."""
    if not timer.get("enabled", True):
        return []

    usages: List[ResourceUsage] = []

    execution = timer.get("execution") or {}
    overlap = execution.get("overlap", "queue")
    if overlap == "allow":
        slot_amount = execution.get("max_concurrent", 1)
        if not isinstance(slot_amount, int) or slot_amount < 1:
            slot_amount = 1
    else:
        slot_amount = 1
    usages.append(ResourceUsage(EXECUTOR_RESOURCE, slot_amount))

    for res in timer.get("resources") or []:
        name = res.get("name")
        if not name:
            continue
        est = parse_rate(res.get("estimated_usage"))
        if est is None or est <= 0:
            continue
        usages.append(ResourceUsage(name, est))

    return usages


def resource_capacity(
    name: str, timers: List[Dict[str, Any]], max_workers: int
) -> Optional[int]:
    """Ceiling for `name` — executor slot uses `max_workers`; user resources
    look up the first parseable `capacity:` among declaring timers.
    """
    if name == EXECUTOR_RESOURCE:
        return max_workers
    for t in timers:
        for res in t.get("resources") or []:
            if res.get("name") != name:
                continue
            cap = parse_rate(res.get("capacity"))
            if cap is not None and cap > 0:
                return cap
    return None


def _is_daemon_or_zero_interval(timer: Dict[str, Any]) -> bool:
    if timer.get("timer_type") == "daemon":
        return True
    rec = timer.get("recurrence") or {}
    if rec.get("frequency") != "interval":
        return False
    try:
        return parse_interval(rec.get("every", "0s"), allow_zero=True) == 0
    except RecurrenceError:
        return False


def _interval_seconds(timer: Dict[str, Any]) -> Optional[int]:
    rec = timer.get("recurrence") or {}
    if rec.get("frequency") != "interval":
        return None
    try:
        every = parse_interval(rec.get("every", "0s"), allow_zero=True)
    except RecurrenceError:
        return None
    return every if every > 0 else None


def _timer_run_windows(
    timer: Dict[str, Any],
    now: datetime,
    horizon_end: datetime,
    run_duration_seconds: int,
) -> Iterable[Tuple[datetime, datetime]]:
    """Yield (start, end) windows where this timer occupies a slot."""
    if not timer.get("enabled", True):
        return

    if _is_daemon_or_zero_interval(timer):
        yield (now, horizon_end)
        return

    # Non-windowed interval timers: timestamp-based fires. occurrences_between
    # returns [] for these (active_hours-less interval), so we project ourselves.
    every = _interval_seconds(timer)
    rec = timer.get("recurrence") or {}
    if every is not None and not (rec.get("active_hours")):
        t = now
        step = timedelta(seconds=every)
        duration = timedelta(seconds=run_duration_seconds)
        while t < horizon_end:
            yield (t, min(t + duration, horizon_end))
            t += step
        return

    # Calendar timers + windowed-interval timers.
    try:
        occs = occurrences_between(timer, now, horizon_end)
    except (RecurrenceError, ValueError):
        return
    duration = timedelta(seconds=run_duration_seconds)
    for occ in occs:
        yield (occ, min(occ + duration, horizon_end))


def project_concurrent_usage(
    timers: List[Dict[str, Any]],
    now: datetime,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
    run_duration_seconds: int = DEFAULT_RUN_DURATION_SECONDS,
) -> Dict[str, List[int]]:
    """resource_name → list[bucket_count] with cumulative concurrent use."""
    if horizon_days <= 0 or bucket_seconds <= 0:
        return {}
    horizon_end = now + timedelta(days=horizon_days)
    n_buckets = (horizon_days * 86400) // bucket_seconds
    if n_buckets <= 0:
        return {}

    projection: Dict[str, List[int]] = {}

    for timer in timers:
        usages = collect_timer_usage(timer)
        if not usages:
            continue
        for start, end in _timer_run_windows(timer, now, horizon_end, run_duration_seconds):
            if end <= start:
                continue
            start_off = max(0.0, (start - now).total_seconds())
            end_off = max(0.0, (end - now).total_seconds())
            start_idx = int(start_off // bucket_seconds)
            end_idx = int(math.ceil(end_off / bucket_seconds))
            start_idx = max(0, start_idx)
            end_idx = min(n_buckets, end_idx)
            if end_idx <= start_idx:
                continue
            for usage in usages:
                arr = projection.setdefault(usage.name, [0] * n_buckets)
                for i in range(start_idx, end_idx):
                    arr[i] += usage.amount

    return projection


def _find_peak(arr: List[int]) -> Tuple[int, int]:
    if not arr:
        return (0, 0)
    peak = arr[0]
    peak_idx = 0
    for i, v in enumerate(arr):
        if v > peak:
            peak = v
            peak_idx = i
    return (peak, peak_idx)


def _peak_contributors(
    timers: List[Dict[str, Any]],
    resource_name: str,
    peak_idx: int,
    now: datetime,
    bucket_seconds: int,
    run_duration_seconds: int,
    horizon_days: int,
) -> List[str]:
    horizon_end = now + timedelta(days=horizon_days)
    bucket_start = now + timedelta(seconds=peak_idx * bucket_seconds)
    bucket_end = bucket_start + timedelta(seconds=bucket_seconds)
    contributors: List[str] = []
    for timer in timers:
        if not any(u.name == resource_name for u in collect_timer_usage(timer)):
            continue
        for start, end in _timer_run_windows(timer, now, horizon_end, run_duration_seconds):
            if start < bucket_end and end > bucket_start:
                contributors.append(timer.get("name") or timer.get("id", "<unknown>"))
                break
    return contributors


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if v > 0 else default


def check_capacity(
    new_timer: Dict[str, Any],
    existing_timers: List[Dict[str, Any]],
    max_workers: int,
    now: Optional[datetime] = None,
    exclude_timer_id: Optional[str] = None,
    horizon_days: Optional[int] = None,
    bucket_seconds: Optional[int] = None,
    run_duration_seconds: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    """Gate admission of `new_timer`. Returns (can_proceed, error_msg)."""
    now = now or datetime.now()
    horizon_days = (
        horizon_days
        if horizon_days is not None
        else _env_positive_int("WAKELITE_CAPACITY_HORIZON_DAYS", DEFAULT_HORIZON_DAYS)
    )
    bucket_seconds = (
        bucket_seconds
        if bucket_seconds is not None
        else _env_positive_int("WAKELITE_CAPACITY_BUCKET_SECONDS", DEFAULT_BUCKET_SECONDS)
    )
    run_duration_seconds = (
        run_duration_seconds
        if run_duration_seconds is not None
        else _env_positive_int(
            "WAKELITE_CAPACITY_RUN_DURATION_SECONDS", DEFAULT_RUN_DURATION_SECONDS
        )
    )

    new_usages = collect_timer_usage(new_timer)
    if not new_usages:
        return (True, None)

    relevant = [
        t for t in existing_timers
        if not (exclude_timer_id and t.get("id") == exclude_timer_id)
    ]
    all_timers = relevant + [new_timer]

    projection = project_concurrent_usage(
        all_timers,
        now,
        horizon_days=horizon_days,
        bucket_seconds=bucket_seconds,
        run_duration_seconds=run_duration_seconds,
    )

    for usage in new_usages:
        cap = resource_capacity(usage.name, all_timers, max_workers)
        if cap is None:
            continue
        arr = projection.get(usage.name)
        if not arr:
            continue
        peak, peak_idx = _find_peak(arr)
        if peak <= cap:
            continue
        peak_time = now + timedelta(seconds=peak_idx * bucket_seconds)
        contributors = _peak_contributors(
            all_timers,
            usage.name,
            peak_idx,
            now,
            bucket_seconds,
            run_duration_seconds,
            horizon_days,
        )
        contrib_str = ", ".join(contributors[:5]) or "<unknown>"
        if len(contributors) > 5:
            contrib_str += f" … +{len(contributors) - 5} more"
        msg = (
            f'Adding this timer would exceed capacity for resource "{usage.name}". '
            f"Peak use {peak} near {peak_time.isoformat(timespec='minutes')} "
            f"exceeds capacity {cap}. Contributors: {contrib_str}"
        )
        return (False, msg)

    return (True, None)
