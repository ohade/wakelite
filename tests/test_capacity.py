"""Unit tests for wakelite.capacity — pure functions, no I/O bootstrap needed."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from wakelite.capacity import (
    DEFAULT_BUCKET_SECONDS,
    DEFAULT_HORIZON_DAYS,
    EXECUTOR_RESOURCE,
    ResourceUsage,
    check_capacity,
    collect_timer_usage,
    parse_rate,
    project_concurrent_usage,
    resource_capacity,
)


def _daemon(name, max_concurrent=1, overlap="queue", enabled=True, resources=None):
    t = {
        "id": name,
        "name": name,
        "enabled": enabled,
        "timer_type": "daemon",
        "recurrence": {"frequency": "interval", "every": "0s"},
        "command": {"mode": "shell", "shell": "sleep 999"},
    }
    if overlap == "allow":
        t["execution"] = {"overlap": "allow", "max_concurrent": max_concurrent}
    if resources:
        t["resources"] = resources
    return t


def _once(name, date_str, time_str="10:00", enabled=True, resources=None):
    t = {
        "id": name,
        "name": name,
        "enabled": enabled,
        "recurrence": {"frequency": "once", "date": date_str, "time": time_str},
        "command": {"mode": "shell", "shell": "echo ok"},
    }
    if resources:
        t["resources"] = resources
    return t


def _daily(name, time_str="06:00", enabled=True, resources=None):
    t = {
        "id": name,
        "name": name,
        "enabled": enabled,
        "recurrence": {"frequency": "daily", "time": time_str},
        "command": {"mode": "shell", "shell": "echo ok"},
    }
    if resources:
        t["resources"] = resources
    return t


class ParseRateTests(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(parse_rate(None))

    def test_int(self):
        self.assertEqual(parse_rate(50), 50)

    def test_float_uses_ceil_consistent_with_string_path(self):
        # 12.7 numeric must round the same way "12.7 req/min" does (ceil),
        # otherwise admission depends on payload encoding.
        self.assertEqual(parse_rate(12.7), 13)
        self.assertEqual(parse_rate("12.7 req/min"), 13)

    def test_hourly_sub_minute_rate_uses_ceil(self):
        # Regression: "61 req/h" must round up to 2/min, not down to 1.
        self.assertEqual(parse_rate("61 req/h"), 2)
        self.assertEqual(parse_rate("1 req/h"), 1)
        self.assertEqual(parse_rate("0 req/h"), 0)

    def test_negative(self):
        self.assertIsNone(parse_rate(-1))
        self.assertIsNone(parse_rate("-5 req/min"))

    def test_bare_digit_string(self):
        self.assertEqual(parse_rate("50"), 50)

    def test_per_minute(self):
        self.assertEqual(parse_rate("50 req/min"), 50)
        self.assertEqual(parse_rate("50 req/minute"), 50)
        self.assertEqual(parse_rate("50req/m"), 50)

    def test_per_second(self):
        self.assertEqual(parse_rate("12 req/sec"), 12 * 60)

    def test_per_hour(self):
        self.assertEqual(parse_rate("60 req/h"), 1)
        self.assertEqual(parse_rate("30 req/h"), 1)  # floor to 1

    def test_unitless_units(self):
        self.assertEqual(parse_rate("10 gb"), 10)

    def test_unparseable(self):
        self.assertIsNone(parse_rate("lots"))
        self.assertIsNone(parse_rate(""))
        self.assertIsNone(parse_rate(True))

    def test_float_ceiling(self):
        self.assertEqual(parse_rate("1.1 req/min"), 2)


class CollectTimerUsageTests(unittest.TestCase):
    def test_disabled_yields_nothing(self):
        self.assertEqual(collect_timer_usage(_daemon("d", enabled=False)), [])

    def test_daemon_consumes_one_slot(self):
        usages = collect_timer_usage(_daemon("d"))
        self.assertEqual(usages, [ResourceUsage(EXECUTOR_RESOURCE, 1)])

    def test_daemon_always_one_slot_regardless_of_overlap_config(self):
        # Daemons keep exactly one live process regardless of overlap=allow /
        # max_concurrent; the gate must reflect runtime behavior, not payload.
        usages = collect_timer_usage(_daemon("d", overlap="allow", max_concurrent=3))
        self.assertEqual(usages[0], ResourceUsage(EXECUTOR_RESOURCE, 1))

    def test_non_daemon_overlap_allow_reports_max_concurrent(self):
        timer = _daily("sched", "06:00")
        timer["execution"] = {"overlap": "allow", "max_concurrent": 3}
        usages = collect_timer_usage(timer)
        self.assertEqual(usages[0], ResourceUsage(EXECUTOR_RESOURCE, 3))

    def test_user_resource_with_estimated_usage_added(self):
        usages = collect_timer_usage(
            _daemon(
                "d",
                resources=[
                    {"name": "slack", "capacity": "50 req/min", "estimated_usage": "12 req/min"}
                ],
            )
        )
        names = {u.name: u.amount for u in usages}
        self.assertEqual(names[EXECUTOR_RESOURCE], 1)
        self.assertEqual(names["slack"], 12)

    def test_user_resource_with_unparseable_estimate_skipped(self):
        usages = collect_timer_usage(
            _daemon("d", resources=[{"name": "opaque", "estimated_usage": "many"}])
        )
        self.assertEqual([u.name for u in usages], [EXECUTOR_RESOURCE])


class ResourceCapacityTests(unittest.TestCase):
    def test_executor_returns_max_workers(self):
        self.assertEqual(resource_capacity(EXECUTOR_RESOURCE, [], 16), 16)

    def test_user_resource_first_parseable_capacity(self):
        timers = [
            _daemon("a", resources=[{"name": "slack", "capacity": "50 req/min"}]),
            _daemon("b", resources=[{"name": "slack", "capacity": "nope"}]),
        ]
        self.assertEqual(resource_capacity("slack", timers, 16), 50)

    def test_unknown_resource_returns_none(self):
        self.assertIsNone(resource_capacity("ghost", [], 16))


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 4, 22, 0, 0, 0)

    def _future_date(self, days):
        return (self.now + timedelta(days=days)).date().isoformat()

    def test_daemon_covers_every_bucket(self):
        proj = project_concurrent_usage([_daemon("d")], self.now, horizon_days=1, bucket_seconds=60)
        arr = proj[EXECUTOR_RESOURCE]
        self.assertEqual(arr[0], 1)
        self.assertEqual(arr[-1], 1)
        self.assertEqual(sum(arr), len(arr))

    def test_two_once_timers_same_minute_sum(self):
        d1 = self._future_date(1)
        proj = project_concurrent_usage(
            [_once("a", d1, "10:00"), _once("b", d1, "10:00")],
            self.now,
            horizon_days=7,
            bucket_seconds=60,
        )
        self.assertEqual(max(proj[EXECUTOR_RESOURCE]), 2)

    def test_two_once_timers_different_days_do_not_overlap(self):
        proj = project_concurrent_usage(
            [_once("a", self._future_date(1), "10:00"), _once("b", self._future_date(2), "10:00")],
            self.now,
            horizon_days=7,
            bucket_seconds=60,
        )
        self.assertEqual(max(proj[EXECUTOR_RESOURCE]), 1)

    def test_disabled_contributes_nothing(self):
        proj = project_concurrent_usage(
            [_daemon("on"), _daemon("off", enabled=False)],
            self.now,
            horizon_days=1,
            bucket_seconds=60,
        )
        self.assertEqual(max(proj[EXECUTOR_RESOURCE]), 1)


class CheckCapacityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 4, 22, 0, 0, 0)

    def _future_date(self, days):
        return (self.now + timedelta(days=days)).date().isoformat()

    def test_twenty_once_timers_spread_all_succeed_at_max_workers_2(self):
        existing: list = []
        for i in range(20):
            new = _once(f"once-{i}", self._future_date(1 + (i % 7)), f"{10 + (i % 8):02d}:{(i * 7) % 60:02d}")
            ok, err = check_capacity(new, existing, max_workers=2, now=self.now)
            self.assertTrue(ok, f"once-{i} blocked: {err}")
            existing.append(new)

    def test_three_daemons_exceed_max_workers_2(self):
        existing = [_daemon("a"), _daemon("b")]
        ok, err = check_capacity(_daemon("c"), existing, max_workers=2, now=self.now)
        self.assertFalse(ok)
        self.assertIn(EXECUTOR_RESOURCE, err)
        self.assertIn("capacity 2", err)

    def test_daemon_at_max_workers_allowed(self):
        existing = [_daemon("a")]
        ok, err = check_capacity(_daemon("b"), existing, max_workers=2, now=self.now)
        self.assertTrue(ok, err)

    def test_user_resource_under_capacity_allowed(self):
        existing = [
            _daemon(
                "poller",
                resources=[{"name": "slack", "capacity": "50 req/min", "estimated_usage": "20 req/min"}],
            )
        ]
        new = _daemon(
            "notifier",
            resources=[{"name": "slack", "capacity": "50 req/min", "estimated_usage": "20 req/min"}],
        )
        ok, err = check_capacity(new, existing, max_workers=16, now=self.now)
        self.assertTrue(ok, err)

    def test_user_resource_over_capacity_blocked(self):
        existing = [
            _daemon(
                "poller",
                resources=[{"name": "slack", "capacity": "50 req/min", "estimated_usage": "30 req/min"}],
            )
        ]
        new = _daemon(
            "notifier",
            resources=[{"name": "slack", "capacity": "50 req/min", "estimated_usage": "30 req/min"}],
        )
        ok, err = check_capacity(new, existing, max_workers=16, now=self.now)
        self.assertFalse(ok)
        self.assertIn("slack", err)

    def test_unparseable_user_resource_is_advisory(self):
        existing = [_daemon("a", resources=[{"name": "opaque", "estimated_usage": "lots"}])]
        new = _daemon("b", resources=[{"name": "opaque", "estimated_usage": "lots"}])
        ok, err = check_capacity(new, existing, max_workers=16, now=self.now)
        self.assertTrue(ok, err)

    def test_exclude_self_on_update(self):
        existing = [_daemon("a"), _daemon("b")]
        # Update existing 'a' without adding load — should be allowed even if cap is tight
        updated = dict(existing[0])
        ok, err = check_capacity(updated, existing, max_workers=2, now=self.now, exclude_timer_id="a")
        self.assertTrue(ok, err)

    def test_two_dailies_same_hour_blocked_at_executor_1(self):
        existing = [_daily("a", "06:00")]
        ok, err = check_capacity(_daily("b", "06:00"), existing, max_workers=1, now=self.now)
        self.assertFalse(ok)

    def test_two_dailies_different_hour_allowed_at_executor_1(self):
        existing = [_daily("a", "06:00")]
        ok, err = check_capacity(_daily("b", "09:00"), existing, max_workers=1, now=self.now)
        self.assertTrue(ok, err)

    def test_update_raising_usage_is_gated(self):
        # Raising estimated_usage on one of two shared-resource timers must
        # trigger the gate even though total timer count is unchanged.
        existing = [
            _daemon("a", resources=[{"name": "slack", "capacity": "50 req/min", "estimated_usage": "20 req/min"}]),
            _daemon("b", resources=[{"name": "slack", "capacity": "50 req/min", "estimated_usage": "20 req/min"}]),
        ]
        updated = dict(existing[0])
        updated["resources"] = [
            {"name": "slack", "capacity": "50 req/min", "estimated_usage": "40 req/min"}
        ]
        ok, err = check_capacity(
            updated, existing, max_workers=16, now=self.now, exclude_timer_id="a"
        )
        self.assertFalse(ok)
        self.assertIn("slack", err)


class InventoryIntervalPhasingTests(unittest.TestCase):
    """WL-12 follow-up: existing interval timers must be projected from
    their real phase (`_last_fired_at`) rather than all collapsing to `now`."""

    def setUp(self):
        self.now = datetime(2026, 4, 22, 12, 0, 0)

    def test_existing_1h_interval_with_recent_fire_does_not_block_new_1h_at_max_workers_1(self):
        # Existing 1h interval fired 30 min ago → next fire in ~30 min.
        # New 1h interval fires at `now`. They must not overlap in bucket 0.
        existing = [{
            "id": "existing",
            "name": "existing",
            "enabled": True,
            "recurrence": {"frequency": "interval", "every": "60m"},
            "command": {"mode": "shell", "shell": "echo ok"},
            "_last_fired_at": (self.now - timedelta(minutes=30)).isoformat(),
        }]
        new = {
            "id": "new",
            "name": "new",
            "enabled": True,
            "recurrence": {"frequency": "interval", "every": "60m"},
            "command": {"mode": "shell", "shell": "echo ok"},
        }
        ok, err = check_capacity(new, existing, max_workers=1, now=self.now)
        self.assertTrue(ok, f"phased interval should not block new 1h timer: {err}")

    def test_brand_new_interval_without_phase_falls_back_to_now(self):
        # No `_last_fired_at`: we still seed at `now`. Two interval timers
        # with no history WILL collide in bucket 0 at max_workers=1 — this
        # is conservative and matches "we don't know when they'll run yet".
        existing = [{
            "id": "existing",
            "name": "existing",
            "enabled": True,
            "recurrence": {"frequency": "interval", "every": "60m"},
            "command": {"mode": "shell", "shell": "echo ok"},
        }]
        new = dict(existing[0])
        new["id"] = "new"
        new["name"] = "new"
        ok, _ = check_capacity(new, existing, max_workers=1, now=self.now)
        self.assertFalse(ok)


class SharedResourceCapacityMinTests(unittest.TestCase):
    def test_min_across_declarations_is_used_not_first(self):
        timers = [
            _daemon("a", resources=[{"name": "slack", "capacity": "100 req/min"}]),
            _daemon("b", resources=[{"name": "slack", "capacity": "10 req/min"}]),
        ]
        self.assertEqual(resource_capacity("slack", timers, 16), 10)

    def test_min_is_order_independent(self):
        t1 = _daemon("low", resources=[{"name": "slack", "capacity": "10 req/min"}])
        t2 = _daemon("high", resources=[{"name": "slack", "capacity": "100 req/min"}])
        self.assertEqual(resource_capacity("slack", [t1, t2], 16), 10)
        self.assertEqual(resource_capacity("slack", [t2, t1], 16), 10)


class KillSwitchTests(unittest.TestCase):
    def setUp(self):
        import os
        self._prev = os.environ.pop("WAKELITE_CAPACITY_ENABLED", None)
        self.now = datetime(2026, 4, 22, 12, 0, 0)

    def tearDown(self):
        import os
        if self._prev is None:
            os.environ.pop("WAKELITE_CAPACITY_ENABLED", None)
        else:
            os.environ["WAKELITE_CAPACITY_ENABLED"] = self._prev

    def test_disabled_env_admits_everything(self):
        import os
        os.environ["WAKELITE_CAPACITY_ENABLED"] = "false"
        existing = [_daemon("a"), _daemon("b")]
        ok, err = check_capacity(_daemon("c"), existing, max_workers=1, now=self.now)
        self.assertTrue(ok, err)

    def test_enabled_by_default(self):
        existing = [_daemon("a"), _daemon("b")]
        ok, _ = check_capacity(_daemon("c"), existing, max_workers=2, now=self.now)
        self.assertFalse(ok)

    def test_values_that_do_not_disable(self):
        import os
        for truthy in ("true", "1", "yes", "", "enabled"):
            os.environ["WAKELITE_CAPACITY_ENABLED"] = truthy
            existing = [_daemon("a"), _daemon("b")]
            ok, _ = check_capacity(_daemon("c"), existing, max_workers=2, now=self.now)
            self.assertFalse(ok, f"value {truthy!r} should leave gate active")


class EnvVarOverrideTests(unittest.TestCase):
    def setUp(self):
        import os
        self._saved = {
            k: os.environ.pop(k, None)
            for k in (
                "WAKELITE_CAPACITY_HORIZON_DAYS",
                "WAKELITE_CAPACITY_BUCKET_SECONDS",
                "WAKELITE_CAPACITY_RUN_DURATION_SECONDS",
            )
        }
        self.now = datetime(2026, 4, 22, 12, 0, 0)

    def tearDown(self):
        import os
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_invalid_horizon_falls_back_to_default(self):
        import os
        for bad in ("0", "-5", "abc", ""):
            os.environ["WAKELITE_CAPACITY_HORIZON_DAYS"] = bad
            existing = [_daily("a", "06:00")]
            ok, _ = check_capacity(_daily("b", "06:00"), existing, max_workers=1, now=self.now)
            self.assertFalse(ok, f"default 7-day horizon should catch collision at 06:00 even with bad env {bad!r}")

    def test_horizon_honored_when_positive(self):
        import os
        os.environ["WAKELITE_CAPACITY_HORIZON_DAYS"] = "1"
        # With 1-day horizon, a once-timer scheduled 5 days out is outside
        # projection and admission is trivially ok.
        far_future = _once("far", (self.now + timedelta(days=5)).date().isoformat(), "06:00")
        ok, _ = check_capacity(far_future, [_daily("a", "06:00")], max_workers=1, now=self.now)
        self.assertTrue(ok)


class DSTSafetyTests(unittest.TestCase):
    """Bucket arithmetic uses .timestamp() so DST transitions don't shift
    the axis. Exercises both spring-forward and fall-back."""

    def test_horizon_crosses_spring_forward(self):
        # Mar 8 2026 02:00 EST → 03:00 EDT. A daily 06:00 timer fires on both
        # sides of the transition; peak should still be 1, not 2 or 0.
        now = datetime(2026, 3, 7, 12, 0, 0)
        existing = [_daily("dawn", "06:00")]
        new = _daily("dawn-other", "09:00")
        ok, err = check_capacity(new, existing, max_workers=1, now=now, horizon_days=3)
        self.assertTrue(ok, err)

    def test_horizon_crosses_fall_back(self):
        now = datetime(2026, 11, 1, 12, 0, 0)
        existing = [_daily("dawn", "06:00")]
        new = _daily("dawn-other", "09:00")
        ok, err = check_capacity(new, existing, max_workers=1, now=now, horizon_days=3)
        self.assertTrue(ok, err)


class PeakHorizonBoundaryTests(unittest.TestCase):
    def test_fire_just_before_horizon_end_is_counted(self):
        now = datetime(2026, 4, 22, 0, 0, 0)
        far = (now + timedelta(days=6, hours=23, minutes=0)).date().isoformat()
        existing = [_once("a", far, "23:30")]
        ok, _ = check_capacity(_once("b", far, "23:30"), existing, max_workers=1, now=now, horizon_days=7)
        self.assertFalse(ok)

    def test_fire_past_horizon_end_is_not_counted(self):
        now = datetime(2026, 4, 22, 0, 0, 0)
        beyond = (now + timedelta(days=10)).date().isoformat()
        existing = [_once("a", beyond, "12:00")]
        ok, _ = check_capacity(_once("b", beyond, "12:00"), existing, max_workers=1, now=now, horizon_days=7)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
