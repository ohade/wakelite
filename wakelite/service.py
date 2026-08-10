from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Optional, Tuple

from concurrent.futures import ThreadPoolExecutor

from .config import (
    AMQ_BINARY_PATH,
    AMQ_CALLBACK_SEND_TIMEOUT_SECONDS,
    AMQ_KEEPALIVE_REGISTRY_FILE,
    DEFAULT_HORIZON_DAYS,
    DEFAULT_RETENTION_DAYS,
    LOG_DIR,
    MAX_WORKERS,
    OWNER,
    RUN_LOG_RETENTION_DAYS,
    RUNNER_LOG,
    WAKE_INTENTS_FILE,
    amq_callback_enabled,
    ensure_dirs,
)
from . import capacity
from .notifier import Notifier
from .recurrence import interval_is_due, interval_window_occurrences, next_occurrence, next_window_occurrence, occurrences_between, parse_interval, parse_recurrence, upcoming_occurrences
from .state import StateStore
from .templates import _deep_merge, get_template, list_templates, resolve_template
from .timer_store import TimerStore
from .utils import atomic_write_json


logger = logging.getLogger(__name__)


class IdempotencyConflictError(ValueError):
    pass


class CapacityExceededError(ValueError):
    pass


class WakeLiteService:
    def __init__(self, tick_seconds: int = 15, max_workers: int = MAX_WORKERS) -> None:
        ensure_dirs()
        logging.basicConfig(
            filename=str(RUNNER_LOG),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        self.tick_seconds = tick_seconds
        self.max_workers = max_workers
        self._started_monotonic = time.monotonic()
        self.timer_store = TimerStore()
        self.state = StateStore()
        self.notifier = Notifier()
        self.notifier.muted = self.state.get_meta("notifications.muted", "false") == "true"
        self._stop = threading.Event()
        self._wake_event = threading.Event()
        self._last_slow_tick_at: Optional[datetime] = None
        self._scheduler_thread: Optional[threading.Thread] = None
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="wl-run")
        self._run_lock = threading.RLock()
        self._active_processes: Dict[str, subprocess.Popen[Any]] = {}
        self._abort_requests: set[str] = set()

    _MAX_SLEEP = 15.0  # seconds; caps idle sleep for heartbeat liveness

    def _signal_wake(self) -> None:
        """Signal the scheduler thread to re-evaluate timer deadlines immediately."""
        self._wake_event.set()

    def _fingerprint(self, payload: Dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _idempotent(self, scope: str, idem_key: str, payload: Dict[str, Any], fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        existing = self.state.get_idempotent(scope, idem_key)
        fingerprint = self._fingerprint(payload)
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise IdempotencyConflictError(
                    f"idempotency key '{idem_key}' already used with different payload"
                )
            return existing["response"]

        response = fn()
        self.state.store_idempotent(scope, idem_key, fingerprint, response)
        return response

    def start(self) -> None:
        recovered = self.state.recover_uncertain_runs()
        recovered_timer_ids: set[str] = set()
        for row in recovered:
            recovered_timer_ids.add(row["timer_id"])
            self.state.set_runtime_idle(row["timer_id"])
            self.state.add_incident(
                "warn",
                "crash_recovery",
                f"Recovered uncertain run {row['run_id']} for timer {row['timer_id']}; retry pending",
                row["timer_id"],
            )

        repaired_timer_ids = self._repair_stale_runtime_locks()
        replay_timer_ids = recovered_timer_ids | repaired_timer_ids

        for row in recovered:
            timer = self.timer_store.get_timer(row["timer_id"])
            if not timer or not timer.get("enabled", True):
                continue
            self._enqueue_or_spawn(
                timer=timer,
                scheduled_at=row["scheduled_at"],
                is_catchup=True,
                queued_reason="crash_recovery",
                retry_of_run_id=row["run_id"],
            )

        for timer_id in replay_timer_ids:
            self._replay_queued_once(timer_id)

        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, name="wakelite-scheduler", daemon=True)
        self._scheduler_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake_event.set()  # break long sleep for fast shutdown
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)
        # Terminate all active processes (daemon and regular)
        with self._run_lock:
            for run_id, proc in list(self._active_processes.items()):
                self._terminate_process(proc)
        self._executor.shutdown(wait=True, cancel_futures=True)

    def health(self) -> Dict[str, Any]:
        timers = self.timer_store.list_timers()
        enabled = [t for t in timers if t.get("enabled", True)]
        heartbeat = self.state.get_meta("runner.heartbeat")
        active_runs = self.state.count_active_runs()
        unacked_incidents = self.state.count_unacked_incidents()
        daemon_count = sum(1 for t in enabled if t.get("timer_type") == "daemon")
        interval_count = sum(1 for t in enabled if t.get("recurrence", {}).get("frequency") == "interval" and t.get("timer_type") != "daemon")

        mute_changes = self.state.list_notification_mute_changes(limit=1)
        return {
            "version": "2.0.0",
            "status": "ok",
            "owner": OWNER,
            "timers_total": len(timers),
            "timers_enabled": len(enabled),
            "daemon_count": daemon_count,
            "interval_count": interval_count,
            "active_runs": active_runs,
            "max_workers": self.max_workers,
            "uptime_seconds": max(0.0, time.monotonic() - self._started_monotonic),
            "notifications_muted": self.notifier.muted,
            "notifications_mute_last_change": mute_changes[0] if mute_changes else None,
            "unacked_incidents": unacked_incidents,
            "runner_heartbeat": heartbeat,
            "now": datetime.now(timezone.utc).isoformat(),
        }

    def get_notifications_muted(self) -> bool:
        return self.notifier.muted

    def get_notification_settings(self) -> Dict[str, Any]:
        return {
            "notifications_muted": self.notifier.muted,
            "audit": self.state.list_notification_mute_changes(),
        }

    def set_notifications_muted(
        self, muted: bool, *, source: str = "service"
    ) -> Dict[str, Any]:
        if not isinstance(source, str):
            raise ValueError("notification mute source must be a string")
        normalized_source = source.strip()
        if not normalized_source:
            raise ValueError("notification mute source must not be empty")
        if len(normalized_source) > 128:
            raise ValueError("notification mute source must be at most 128 characters")

        changed = self.notifier.muted != muted
        audit_entry = self.state.set_notification_mute(
            muted, normalized_source, changed=changed
        )
        self.notifier.muted = muted
        return {
            "notifications_muted": muted,
            "changed": changed,
            "audit_entry": audit_entry,
        }

    @staticmethod
    def _format_daemon_status(ds: "DaemonState", now: datetime) -> str:
        """Format a human-readable status string for a daemon timer."""
        if ds.status == "running":
            elapsed = ""
            if ds.last_started_at:
                try:
                    started = datetime.fromisoformat(ds.last_started_at.replace("Z", "+00:00")).replace(tzinfo=None)
                    secs = int((now - started).total_seconds())
                    if secs < 60:
                        elapsed = f", started {secs}s ago"
                    elif secs < 3600:
                        elapsed = f", started {secs // 60}m ago"
                    else:
                        elapsed = f", started {secs // 3600}h ago"
                except (ValueError, TypeError):
                    pass
            return f"running{elapsed}"
        # stopped or restarting
        detail = ""
        if ds.last_exit_code is not None:
            detail = f", exit={ds.last_exit_code}"
        if ds.current_backoff_seconds > 0:
            detail += f", restart in {ds.current_backoff_seconds}s"
        return f"stopped{detail}"

    def _compute_next_run(self, timer: Dict[str, Any], now: datetime) -> Optional[str]:
        if not timer.get("enabled", True):
            return None
        # Daemon timers: derive status from daemon_state, not interval.last_fired
        if timer.get("timer_type") == "daemon":
            ds = self.state.get_daemon_state(timer["id"])
            return self._format_daemon_status(ds, now)
        rec = timer.get("recurrence", {})
        if rec.get("frequency") == "interval":
            every_sec = parse_interval(rec.get("every", "0s"))
            if every_sec == 0:
                return "daemon (continuous)"
            recurrence = parse_recurrence(timer)
            # Windowed interval timers have predictable daily schedules
            if recurrence.active_hours_start:
                nxt = next_window_occurrence(recurrence, now)
                return nxt.strftime("%Y-%m-%dT%H:%M:%S") if nxt else None
            meta_key = f"interval.last_fired.{timer['id']}"
            last_raw = self.state.get_meta(meta_key)
            if last_raw is None:
                return "now (never fired)"
            try:
                last = datetime.fromisoformat(last_raw)
            except ValueError:
                return "now (parse error)"
            nxt = last + timedelta(seconds=every_sec)
            return nxt.strftime("%Y-%m-%dT%H:%M:%S")
        nxt = next_occurrence(timer, now)
        return nxt.isoformat() if nxt else None

    def list_timers(self) -> List[Dict[str, Any]]:
        now = datetime.now()
        timers = self.timer_store.list_timers()
        for timer in timers:
            timer["next_run"] = self._compute_next_run(timer, now)
            if timer.get("max_runs") is not None:
                timer["completed_runs"] = self.state.count_completed_runs(timer["id"])
        return timers

    def get_timer(self, timer_id: str) -> Optional[Dict[str, Any]]:
        timer = self.timer_store.get_timer(timer_id)
        if not timer:
            return None
        timer["next_run"] = self._compute_next_run(timer, datetime.now())
        if timer.get("max_runs") is not None:
            timer["completed_runs"] = self.state.count_completed_runs(timer["id"])
        return timer

    def _enrich_with_last_fired(self, timer: Dict[str, Any]) -> Dict[str, Any]:
        """Stamp `_last_fired_at` on interval timers so capacity.py can phase
        their projection correctly. Non-interval and brand-new timers are
        unchanged. Returns a shallow copy; the store's dict is not mutated."""
        rec = timer.get("recurrence") or {}
        if rec.get("frequency") != "interval":
            return timer
        tid = timer.get("id")
        if not tid:
            return timer
        last_fired_raw = self.state.get_meta(f"interval.last_fired.{tid}")
        if not last_fired_raw:
            return timer
        enriched = dict(timer)
        enriched["_last_fired_at"] = last_fired_raw
        return enriched

    def check_capacity(self, new_timer: Dict[str, Any], exclude_timer_id: Optional[str] = None) -> tuple:
        """Check if adding/updating a timer would violate per-resource capacity
        anywhere across the projection horizon.

        Returns (can_proceed: bool, warnings: list[str], error_msg: str | None).
        """
        warnings: List[str] = list(
            self.timer_store.check_resource_conflicts(new_timer, exclude_timer_id)
        )
        existing = [
            self._enrich_with_last_fired(t) for t in self.timer_store.list_timers()
        ]
        # On update, attach the updating timer's real phase too so raising
        # estimated_usage is evaluated against the existing fire schedule.
        evaluated_new = new_timer
        if exclude_timer_id:
            evaluated_new = self._enrich_with_last_fired(
                {**new_timer, "id": new_timer.get("id") or exclude_timer_id}
            )
        can_proceed, error_msg = capacity.check_capacity(
            evaluated_new,
            existing,
            self.max_workers,
            exclude_timer_id=exclude_timer_id,
        )
        return (can_proceed, warnings, error_msg)

    def create_timer(self, payload: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        # Pre-check capacity before idempotent wrapper
        can_proceed, warnings, error_msg = self.check_capacity(payload)
        if not can_proceed:
            raise CapacityExceededError(error_msg)

        def _create() -> Dict[str, Any]:
            timer = self.timer_store.create_timer(payload)
            result: Dict[str, Any] = {"timer": timer}
            if warnings:
                result["warnings"] = warnings
            return result

        result = self._idempotent(
            scope="timer.create",
            idem_key=idempotency_key,
            payload=payload,
            fn=_create,
        )
        self._signal_wake()
        return result

    def clone_timer(self, source_id: str, overrides: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        source = self.timer_store.get_timer(source_id)
        if not source:
            raise KeyError(source_id)

        # Build clone payload: strip metadata, apply overrides
        payload = copy.deepcopy(source)
        for key in ("id", "created_at", "updated_at", "next_run"):
            payload.pop(key, None)
        if "name" not in overrides:
            payload["name"] = f"{source['name']} (copy)"
        payload = _deep_merge(payload, overrides)

        can_proceed, warnings, error_msg = self.check_capacity(payload)
        if not can_proceed:
            raise CapacityExceededError(error_msg)

        def _clone() -> Dict[str, Any]:
            timer = self.timer_store.create_timer(payload)
            result: Dict[str, Any] = {"timer": timer}
            if warnings:
                result["warnings"] = warnings
            return result

        result = self._idempotent(
            scope=f"timer.clone:{source_id}",
            idem_key=idempotency_key,
            payload=payload,
            fn=_clone,
        )
        self._signal_wake()
        return result

    def list_templates(self) -> List[Dict[str, Any]]:
        return list_templates()

    def get_template(self, name: str) -> Dict[str, Any]:
        tpl = get_template(name)
        if tpl is None:
            raise KeyError(name)
        return tpl

    def create_from_template(self, template_name: str, overrides: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        payload = resolve_template(template_name, overrides)
        return self.create_timer(payload, idempotency_key)

    def update_timer(self, timer_id: str, patch: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        # Build the projected timer for capacity check
        existing = self.timer_store.get_timer(timer_id)
        if existing:
            projected = dict(existing)
            projected.update(patch)
            can_proceed, warnings, error_msg = self.check_capacity(projected, exclude_timer_id=timer_id)
            if not can_proceed:
                raise CapacityExceededError(error_msg)
        else:
            warnings = []

        def _update() -> Dict[str, Any]:
            timer = self.timer_store.update_timer(timer_id, patch)
            result: Dict[str, Any] = {"timer": timer}
            if warnings:
                result["warnings"] = warnings
            return result

        result = self._idempotent(
            scope=f"timer.update:{timer_id}",
            idem_key=idempotency_key,
            payload=patch,
            fn=_update,
        )
        self._signal_wake()
        return result

    def delete_timer(self, timer_id: str, idempotency_key: str) -> Dict[str, Any]:
        result = self._idempotent(
            scope=f"timer.delete:{timer_id}",
            idem_key=idempotency_key,
            payload={"timer_id": timer_id},
            fn=lambda: {"deleted": self.timer_store.delete_timer(timer_id)},
        )
        self._signal_wake()
        return result

    def set_timer_enabled(self, timer_id: str, enabled: bool, idempotency_key: str) -> Dict[str, Any]:
        result = self._idempotent(
            scope=f"timer.enabled:{timer_id}",
            idem_key=idempotency_key,
            payload={"enabled": enabled},
            fn=lambda: {"timer": self.timer_store.set_enabled(timer_id, enabled)},
        )
        # Reset daemon backoff on re-enable so it restarts immediately
        if enabled:
            timer = self.timer_store.get_timer(timer_id)
            if timer and timer.get("timer_type") == "daemon":
                self.state.set_daemon_state(timer_id, current_backoff_seconds=0, status="stopped")
        self._signal_wake()
        return result

    def run_timer_now(self, timer_id: str, idempotency_key: str) -> Dict[str, Any]:
        timer = self.timer_store.get_timer(timer_id)
        if not timer:
            raise KeyError(timer_id)

        scheduled_at = datetime.now().isoformat(timespec="seconds")

        def _run_now() -> Dict[str, Any]:
            queued = self._schedule_occurrence(timer, scheduled_at, is_catchup=False, queued_reason="run_now")
            return {"queued": queued, "timer_id": timer_id, "scheduled_at": scheduled_at}

        result = self._idempotent(
            scope=f"timer.run_now:{timer_id}",
            idem_key=idempotency_key,
            payload={"timer_id": timer_id, "scheduled_at": scheduled_at},
            fn=_run_now,
        )
        self._signal_wake()
        return result

    def abort_run(self, run_id: str, idempotency_key: str) -> Dict[str, Any]:
        def _abort() -> Dict[str, Any]:
            run = self.state.get_run(run_id)
            if not run:
                raise KeyError(run_id)

            status = run.get("status")
            if status != "started":
                return {
                    "run_id": run_id,
                    "aborted": False,
                    "status": status,
                    "message": "run is not active",
                }

            with self._run_lock:
                self._abort_requests.add(run_id)
                proc = self._active_processes.get(run_id)

            if proc is None:
                # Abort requested before process handle is visible; runner thread will
                # honor this flag before command start.
                return {
                    "run_id": run_id,
                    "aborted": True,
                    "status": "aborting",
                    "message": "abort requested",
                }

            forced_kill = self._terminate_process(proc)
            return {
                "run_id": run_id,
                "aborted": True,
                "status": "aborting",
                "forced_kill": forced_kill,
                "message": "abort signal sent",
            }

        return self._idempotent(
            scope=f"run.abort:{run_id}",
            idem_key=idempotency_key,
            payload={"run_id": run_id},
            fn=_abort,
        )

    def list_runs(self, limit: int = 100, timer_id: Optional[str] = None) -> List[Dict[str, Any]]:
        runs = self.state.list_runs(limit=limit, timer_id=timer_id)
        timer_name_by_id = {t["id"]: t.get("name", t["id"]) for t in self.timer_store.list_timers()}
        for run in runs:
            if not run.get("timer_name"):
                run["timer_name"] = timer_name_by_id.get(run["timer_id"])
            if not run.get("timer_name"):
                run["timer_name"] = "Deleted timer"
            run_id = run.get("run_id")
            run["logs_url"] = f"/v1/runs/{run_id}/logs" if run_id else None
            run["has_logs"] = self._path_exists(run.get("stdout_path")) or self._path_exists(run.get("stderr_path"))
        return runs

    def get_run_logs(self, run_id: str) -> Dict[str, Any]:
        run = self.state.get_run(run_id)
        if not run:
            raise KeyError(run_id)

        def _read(path: Optional[str]) -> str:
            if not path:
                return ""
            p = Path(path)
            if not p.exists():
                return ""
            try:
                return p.read_text(encoding="utf-8", errors="replace")[-20000:]
            except Exception:
                return ""

        stdout_available = self._path_exists(run.get("stdout_path"))
        stderr_available = self._path_exists(run.get("stderr_path"))
        logs_expired = False
        if not stdout_available and not stderr_available:
            created = run.get("created_at")
            if created:
                try:
                    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if created_dt <= (datetime.now(timezone.utc) - timedelta(days=RUN_LOG_RETENTION_DAYS)):
                        logs_expired = True
                except ValueError:
                    pass

        return {
            "run": run,
            "stdout": _read(run.get("stdout_path")),
            "stderr": _read(run.get("stderr_path")),
            "stdout_available": stdout_available,
            "stderr_available": stderr_available,
            "logs_expired": logs_expired,
        }

    def ack_incident(self, incident_id: int, idempotency_key: str) -> Dict[str, Any]:
        return self._idempotent(
            scope=f"incident.ack:{incident_id}",
            idem_key=idempotency_key,
            payload={"incident_id": incident_id},
            fn=lambda: {"acknowledged": self.state.ack_incident(incident_id)},
        )

    def _compute_next_wake(self, now: datetime) -> float:
        """Compute seconds until the next event the scheduler needs to handle.

        Returns a float capped at _MAX_SLEEP (for heartbeat liveness) and
        floored at 0.1s (to prevent spin-loops on edge cases).
        """
        soonest = self._MAX_SLEEP

        # Factor in slow-tick deadline
        if self._last_slow_tick_at is not None:
            slow_remaining = self.tick_seconds - (now - self._last_slow_tick_at).total_seconds()
            soonest = min(soonest, max(0.0, slow_remaining))
        else:
            return 0.1  # first run, force immediate

        for timer in self.timer_store.list_timers():
            if not timer.get("enabled", True):
                continue

            # Daemon timers: check backoff deadline
            if timer.get("timer_type") == "daemon":
                timer_id = timer["id"]
                runtime = self.state.get_runtime(timer_id)
                if runtime.is_running:
                    continue  # running fine, nothing to schedule
                ds = self.state.get_daemon_state(timer_id)
                if ds.status == "stopped" and ds.last_exited_at:
                    execution = timer.get("execution", {})
                    restart_on_failure = execution.get("restart_on_failure", True)
                    if ds.last_exit_code != 0 and not restart_on_failure:
                        continue  # won't restart
                    backoff = ds.current_backoff_seconds or execution.get("restart_delay_seconds", 5)
                    try:
                        exited_at = datetime.fromisoformat(ds.last_exited_at.replace("Z", "+00:00"))
                        elapsed = (datetime.now(timezone.utc) - exited_at).total_seconds()
                        soonest = min(soonest, max(0.0, backoff - elapsed))
                    except (ValueError, AttributeError):
                        soonest = 0.0
                elif not runtime.is_running:
                    soonest = 0.0  # needs immediate start
                continue

            rec = timer.get("recurrence", {})
            freq = rec.get("frequency")

            # Interval timers: time until next fire
            if freq == "interval":
                recurrence = parse_recurrence(timer)
                # Windowed interval: sleep until next window slot
                if recurrence.active_hours_start:
                    nxt = next_window_occurrence(recurrence, now)
                    if nxt:
                        soonest = min(soonest, max(0.0, (nxt - now).total_seconds()))
                    continue
                every_seconds = recurrence.every_seconds or 0
                meta_key = f"interval.last_fired.{timer['id']}"
                last_fired_raw = self.state.get_meta(meta_key)
                if last_fired_raw is None:
                    soonest = 0.0  # never fired, fire now
                else:
                    try:
                        last_fired = datetime.fromisoformat(last_fired_raw)
                        elapsed = (now - last_fired).total_seconds()
                        soonest = min(soonest, max(0.0, every_seconds - elapsed))
                    except ValueError:
                        soonest = 0.0
                continue

            # Calendar timers: time until next occurrence
            nxt = next_occurrence(timer, now)
            if nxt:
                soonest = min(soonest, max(0.0, (nxt - now).total_seconds()))

        return max(0.1, min(soonest, self._MAX_SLEEP))

    def _scheduler_loop(self) -> None:
        last_tick_iso = self.state.get_meta("runner.last_tick")
        if last_tick_iso:
            try:
                last_tick = datetime.fromisoformat(last_tick_iso)
            except ValueError:
                last_tick = datetime.now() - timedelta(seconds=self.tick_seconds)
        else:
            last_tick = datetime.now() - timedelta(seconds=self.tick_seconds)

        self._last_slow_tick_at = datetime.now() - timedelta(seconds=self.tick_seconds)
        prune_counter = 0
        consecutive_errors = 0

        while not self._stop.is_set():
            try:
                self._wake_event.clear()
                now = datetime.now()
                self.state.set_meta("runner.heartbeat", datetime.now(timezone.utc).isoformat())

                # Fast tick: interval timers + daemon health (every wake)
                self._process_interval_due(now)
                self._process_daemons(now)

                # Slow tick: calendar timers + housekeeping (time-based)
                if (now - self._last_slow_tick_at).total_seconds() >= self.tick_seconds:
                    self._process_due(last_tick, now)
                    self._export_wake_intents(now)
                    self._maybe_emit_morning_digest(now)

                    self.state.set_meta("runner.last_tick", now.isoformat())
                    last_tick = now
                    self._last_slow_tick_at = now

                    prune_counter += 1
                    if prune_counter >= max(1, int(3600 / self.tick_seconds)):
                        self._prune()
                        prune_counter = 0

                if consecutive_errors > 0:
                    logger.info("Scheduler recovered after %d consecutive errors", consecutive_errors)
                consecutive_errors = 0

            except sqlite3.OperationalError as e:
                consecutive_errors += 1
                backoff = min(30, 2 ** consecutive_errors)
                self.state._reset_connection()
                logger.error(
                    "Scheduler tick failed (attempt %d, backoff %ds): %s — db_path=%s exists=%s",
                    consecutive_errors, backoff, e,
                    self.state.db_path,
                    self.state.db_path.exists() if hasattr(self.state, 'db_path') else 'N/A',
                )
                if consecutive_errors >= 60:
                    logger.critical(
                        "Scheduler giving up after %d consecutive SQLite errors. "
                        "Last error: %s. Runner will exit.",
                        consecutive_errors, e,
                    )
                    os._exit(78)  # EX_CONFIG — launchd will restart us
                self._wake_event.wait(timeout=backoff)
                continue
            except Exception as e:
                consecutive_errors += 1
                logger.exception("Unexpected scheduler error (attempt %d): %s", consecutive_errors, e)
                if consecutive_errors >= 10:
                    logger.critical("Scheduler giving up after %d unexpected errors", consecutive_errors)
                    os._exit(78)
                self._wake_event.wait(timeout=5)
                continue

            # Sleep until next event or wake signal
            sleep_seconds = self._compute_next_wake(now)
            self._wake_event.wait(timeout=sleep_seconds)

    def _process_due(self, start: datetime, end: datetime) -> None:
        for timer in self.timer_store.list_timers():
            # Auto-delete expired one-time timers — they'll never fire again.
            rec = timer.get("recurrence", {})
            if rec.get("frequency") == "once" and rec.get("date"):
                from datetime import date as _date
                try:
                    once_date = _date.fromisoformat(str(rec["date"]))
                except ValueError:
                    pass
                else:
                    if once_date < end.date():
                        logger.info("Auto-deleting expired once-timer %s (%s)", timer["id"], timer.get("name"))
                        try:
                            self.timer_store.delete_timer(timer["id"])
                        except Exception:
                            logger.warning("Failed to auto-delete timer %s", timer["id"], exc_info=True)
                        continue

            if not timer.get("enabled", True):
                continue

            max_runs = timer.get("max_runs")
            if max_runs is not None:
                completed = self.state.count_completed_runs(timer["id"])
                if completed >= max_runs:
                    logger.info("Timer %s reached max_runs=%d (%d completed), auto-disabling", timer.get("name"), max_runs, completed)
                    try:
                        self.timer_store.set_enabled(timer["id"], False)
                    except Exception:
                        logger.warning("Failed to auto-disable timer %s", timer["id"], exc_info=True)
                    continue

            due = occurrences_between(timer, start, end)
            if not due:
                continue

            if len(due) > 1:
                selected = due[-1]
                self.state.add_incident(
                    "warn",
                    "catchup_coalesce",
                    f"Coalesced {len(due)} missed occurrences into one run at {selected.isoformat()}",
                    timer["id"],
                )
                self._schedule_occurrence(
                    timer,
                    selected.isoformat(timespec="seconds"),
                    is_catchup=True,
                    queued_reason="catchup",
                )
                continue

            selected = due[0]
            lag = (end - selected).total_seconds()
            is_catchup = lag > (self.tick_seconds * 2)
            self._schedule_occurrence(
                timer,
                selected.isoformat(timespec="seconds"),
                is_catchup=is_catchup,
                queued_reason="catchup" if is_catchup else None,
            )

    def _schedule_occurrence(self, timer: Dict[str, Any], scheduled_at: str, is_catchup: bool, queued_reason: Optional[str]) -> bool:
        timer_id = timer["id"]
        reserved = self.state.reserve_occurrence(timer_id, scheduled_at, is_catchup)
        if not reserved:
            return False

        return self._enqueue_or_spawn(
            timer=timer,
            scheduled_at=scheduled_at,
            is_catchup=is_catchup,
            queued_reason=queued_reason,
            retry_of_run_id=None,
        )

    def _enqueue_or_spawn(
        self,
        timer: Dict[str, Any],
        scheduled_at: str,
        is_catchup: bool,
        queued_reason: Optional[str],
        retry_of_run_id: Optional[str],
    ) -> bool:
        timer_id = timer["id"]
        execution = timer.get("execution", {})
        overlap = execution.get("overlap", "queue")
        max_concurrent = execution.get("max_concurrent", 1)
        runtime = self.state.get_runtime(timer_id)

        if runtime.is_running:
            if overlap == "allow" and runtime.running_count < max_concurrent:
                pass  # fall through to spawn
            elif overlap == "queue":
                queued = self.state.set_queue_once(timer_id, scheduled_at)
                if queued:
                    self.state.add_incident(
                        "info",
                        "overlap_queued",
                        f"Timer {timer_id} was running; queued occurrence at {scheduled_at}",
                        timer_id,
                    )
                return queued
            elif overlap == "skip":
                logger.info("Timer %s: overlap=skip, skipping occurrence at %s", timer_id, scheduled_at)
                return False
            else:
                # default to queue for backward compat
                queued = self.state.set_queue_once(timer_id, scheduled_at)
                return queued

        self._spawn_run(timer, scheduled_at, is_catchup, queued_reason, retry_of_run_id=retry_of_run_id)
        return True

    def _spawn_run(
        self,
        timer: Dict[str, Any],
        scheduled_at: str,
        is_catchup: bool,
        queued_reason: Optional[str],
        retry_of_run_id: Optional[str],
    ) -> None:
        self._executor.submit(self._run_occurrence, timer, scheduled_at, is_catchup, queued_reason, retry_of_run_id)

    def _check_until_condition(self, timer: Dict[str, Any], status: str) -> bool:
        """Check if timer's until condition is met. Returns True if timer should be deleted.

        The until object has two fields:
          on_success: "delete" | "continue"
          on_failure: "delete" | "continue"

        Aborted runs do NOT trigger either condition — they are user-initiated
        cancellations, not a meaningful success/failure signal.
        """
        until = timer.get("until")
        if not until:
            return False
        if status == "success" and until.get("on_success") == "delete":
            return True
        if status == "failed" and until.get("on_failure") == "delete":
            return True
        return False

    def _execute_callback(
        self,
        timer: Dict[str, Any],
        status: str,
        exit_code: Optional[int],
        run_id: str,
        stdout_path: str,
        duration_seconds: float,
        slack_thread_ts: Optional[str] = None,
    ) -> None:
        """Fire callback if configured. Only on success/failed, never waiting/aborted."""
        callback = timer.get("callback")
        if not callback:
            return
        if status not in ("success", "failed"):
            return

        try:
            timer_name = timer.get("name") or timer.get("id", "unknown")
            duration_str = self._format_duration(duration_seconds)

            # Read last 50 lines of stdout
            stdout_tail = ""
            try:
                p = Path(stdout_path)
                if p.exists():
                    lines = p.read_text(errors="replace").splitlines()
                    tail = lines[-50:] if len(lines) > 50 else lines
                    stdout_tail = "\n".join(tail)
            except Exception:
                stdout_tail = "(unable to read stdout)"

            message = (
                f'[WakeLite callback] Timer "{timer_name}" completed\n'
                f"Status: {status} | Exit code: {exit_code} | Duration: {duration_str}\n"
                f"Stdout (last 50 lines):\n"
                f"{'─' * 25}\n"
                f"{stdout_tail}\n"
                f"{'─' * 25}\n"
                f"This timer was created during your session. Act on the results above."
            )

            cb_type = callback.get("type")
            if cb_type in ("wezterm", "ghostty", "cmux"):
                # Write signal file for UserPromptSubmit hook to pick up
                session_id = callback.get("session_id")
                if session_id and cb_type != "cmux":
                    self._write_callback_signal(session_id, run_id, timer_name, status, exit_code, duration_str, stdout_tail)
                if cb_type == "cmux":
                    self._cmux_callback(timer, run_id, status, exit_code, duration_str, stdout_tail, callback)
                elif cb_type == "ghostty":
                    self._ghostty_callback(callback, message, timer, timer_name, status, slack_thread_ts=slack_thread_ts)
                else:
                    self._wezterm_callback(callback, message, timer, timer_name, status, slack_thread_ts=slack_thread_ts)
        except Exception:
            logger.warning("Callback failed for timer %s", timer.get("id"), exc_info=True)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}h {m}m {s}s"
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"

    def _write_callback_signal(
        self,
        session_id: Optional[str],
        run_id: str,
        timer_name: str,
        status: str,
        exit_code: Optional[int],
        duration_str: str,
        stdout_tail: str,
        timer_id: Optional[str] = None,
    ) -> Path:
        """Write callback data to signal file for Claude Code hook to pick up.

        Uses run_id in filename to prevent overlapping callbacks from
        overwriting each other (bug found during /consult 2026-04-07).

        CC-95 HIGH#4: when ``session_id`` is None (e.g., a cmux-callback timer
        that was never bound to a Claude session), the file lands under a
        synthetic ``_no-session.<timer_id>.<run_id>.wakelite-callback.json``
        name so the payload remains durable on disk for human / tooling
        recovery. The session-keyed inject hook globs ``<session>.*.json`` and
        will not pick the synthetic name up automatically — that is by design,
        because there is no live session to inject into.
        """
        import datetime as _dt

        signal_dir = Path.home() / ".claude" / "session-signals"
        signal_dir.mkdir(parents=True, exist_ok=True)
        if session_id:
            signal_file = signal_dir / f"{session_id}.{run_id}.wakelite-callback.json"
        else:
            # Task #6 polish: re-rstrip after the [:80] truncation. The earlier
            # `.strip("._")` removed leading/trailing separators on the
            # already-substituted string, but slicing to 80 chars can leave a
            # `.` or `_` at position 79 — e.g. `"x"*79 + "_x"` slices to
            # `"x"*79 + "_"`, which produces a filename with a trailing
            # underscore that some downstream tooling interprets as an
            # incomplete name. The post-slice rstrip + `or "unknown"` re-applied
            # at the end together guarantee the slug is stable AND non-empty.
            slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(timer_id or "unknown")).strip("._")
            slug = slug[:80].rstrip("._") or "unknown"
            signal_file = signal_dir / f"_no-session.{slug}.{run_id}.wakelite-callback.json"

        signal_data = {
            "timer_name": timer_name,
            "run_id": run_id,
            "status": status,
            "exit_code": exit_code,
            "duration": duration_str,
            "stdout_tail": stdout_tail,
            "timestamp": _dt.datetime.now().isoformat(),
        }
        signal_file.write_text(json.dumps(signal_data, indent=2))
        logger.info("Wrote callback signal file: %s", signal_file)
        return signal_file

    # ── cmux callback ─────────────────────────────────────────────────

    def _cmux_callback(
        self,
        timer: Dict[str, Any],
        run_id: str,
        status: str,
        exit_code: Optional[int],
        duration: str,
        stdout_tail: str,
        callback: Dict[str, Any],
    ) -> str:
        """Route a cmux callback through AMQ when opted in, else inject it.

        The full callback payload lives in the terminal-neutral signal file.
        AMQ delivery is accepted when the send exits successfully with a
        non-empty message ID, proving that the full payload was stored in the
        mailbox. If AMQ is unavailable, rejects the send, or the target is
        known dead, the existing cmux injection path remains the fallback. The
        signal file stays in place unless AMQ accepts the message. Returns one
        of ``amq-sent``, ``fallback-delivered``, or ``delivery-failed``.
        ``delivery-failed`` includes both no terminal delivery and a ``partial``
        injection whose text is present but still awaits manual submission.
        The caller discards this return value; tests and the structured route
        log consume it as callback evidence, not as run-completion state.
        """
        timer_id = timer.get("id", "unknown")
        timer_name = timer.get("name", timer_id)
        session_id = callback.get("session_id")

        message = (
            f'[WakeLite callback] Timer "{timer_name}" completed\n'
            f"Status: {status} | Exit code: {exit_code} | Duration: {duration}\n"
            f"Stdout (last 50 lines):\n"
            f"{'─' * 25}\n"
            f"{stdout_tail}\n"
            f"{'─' * 25}\n"
            f"This timer was created during your session. Act on the results above."
        )

        # CC-95 HIGH#4: persist the recovery signal file BEFORE any early-return
        # path. Pre-fix, the write only happened inside the `if session_id:`
        # branch, so a cmux-callback timer with no session binding silently lost
        # its payload whenever cmux delivery failed (missing CLI, missing
        # workspace_id/surface_id, or stale target with no session-store
        # fallback). Writing first guarantees the data is recoverable on disk
        # regardless of which downstream branch we take.
        signal_file: Optional[Path] = None
        try:
            signal_file = self._write_callback_signal(
                session_id,
                run_id,
                timer_name,
                status,
                exit_code,
                duration,
                stdout_tail,
                timer_id=timer_id,
            )
        except (OSError, ValueError) as exc:
            logger.error(
                "Recovery signal write failed for timer %s run %s: %s",
                timer_id,
                run_id,
                exc,
            )
            # Task #6 polish: surface the recovery-write failure as an
            # incident so it appears in the dashboard / oncall view, not
            # just in the runner log. add_incident is best-effort — if
            # state.db is itself unwritable (typical reason for a recovery
            # write to fail too), the secondary failure is silently
            # swallowed so we don't shadow the original error.
            try:
                self.state.add_incident(
                    "warn",
                    "callback_recovery_write_failed",
                    f"cmux callback recovery signal write failed for run {run_id}: {exc}",
                    timer_id=timer_id,
                )
            except Exception:  # noqa: BLE001 — best-effort; original error already logged above
                logger.exception(
                    "Failed to record incident for recovery-write failure on timer %s run %s",
                    timer_id,
                    run_id,
                )

        amq_requested = bool(callback.get("amq"))
        amq_message_id: Optional[str] = None
        outcome = "delivery-failed"
        route = "cmux_fallback"
        if amq_requested:
            delivered_via_amq = False
            if amq_callback_enabled():
                try:
                    delivered_via_amq, amq_message_id = self._deliver_cmux_callback_via_amq(
                        callback,
                        session_id,
                        signal_file,
                    )
                except Exception:
                    logger.warning(
                        "AMQ callback route failed for timer %s run %s; falling back to cmux",
                        timer_id,
                        run_id,
                        exc_info=True,
                    )

            if delivered_via_amq:
                if signal_file is not None:
                    try:
                        signal_file.unlink(missing_ok=True)
                    except OSError:
                        logger.warning(
                            "AMQ accepted callback but signal cleanup failed for timer %s run %s: %s",
                            timer_id,
                            run_id,
                            signal_file,
                            exc_info=True,
                        )
                outcome = "amq-sent"
                route = "amq"

        if outcome != "amq-sent":
            trigger = (
                f"[WakeLite: {timer_name} completed ({status})]"
                if session_id
                else message
            )
            outcome = self._deliver_cmux_callback_via_injection(
                callback,
                timer,
                session_id,
                trigger,
                timer_id,
            )

        if amq_requested:
            logger.info(
                "callback_route timer_id=%s run_id=%s route=%s "
                "outcome=%s amq_message_id=%s",
                timer_id,
                run_id,
                route,
                outcome,
                amq_message_id or "none",
            )
        return outcome

    def _deliver_cmux_callback_via_injection(
        self,
        callback: Dict[str, Any],
        timer: Dict[str, Any],
        session_id: Optional[str],
        trigger: str,
        timer_id: str,
    ) -> str:
        """Deliver through the existing cmux path and return its terminal outcome."""
        cli_path = self._resolve_cmux_cli_path(callback)
        workspace_id = callback.get("workspace_id")
        surface_id = callback.get("surface_id")

        if not cli_path:
            logger.error(
                "cmux callback failed for timer %s: cmux CLI not found or not executable",
                timer_id,
            )
            return "delivery-failed"
        if not workspace_id or not surface_id:
            logger.error(
                "cmux callback failed for timer %s: missing workspace_id or surface_id",
                timer_id,
            )
            return "delivery-failed"

        env = self._cmux_env(callback)
        delivery = self._cmux_deliver_trigger(
            cli_path, env, workspace_id, surface_id, trigger, timer_id
        )
        if delivery == "delivered":
            return "fallback-delivered"
        if delivery != "stale":
            return "delivery-failed"
        if not session_id:
            logger.error(
                "cmux callback target is stale for timer %s and no "
                "session_id is available for fallback",
                timer_id,
            )
            return "delivery-failed"

        resolved = self._resolve_cmux_target_via_session_store(session_id)
        if resolved:
            resolved_workspace, resolved_surface = resolved
            delivery = self._cmux_deliver_trigger(
                cli_path,
                env,
                resolved_workspace,
                resolved_surface,
                trigger,
                timer_id,
            )
            if delivery == "delivered":
                return "fallback-delivered"
            if delivery != "stale":
                return "delivery-failed"

        if self._cmux_new_workspace_fallback(cli_path, env, timer, session_id):
            return "fallback-delivered"
        return "delivery-failed"

    @staticmethod
    def _cmux_env(callback: Dict[str, Any]) -> Dict[str, str]:
        env = os.environ.copy()
        socket_path = callback.get("socket_path")
        if socket_path:
            env["CMUX_SOCKET_PATH"] = socket_path
        return env

    @staticmethod
    def _resolve_cmux_cli_path(callback: Dict[str, Any]) -> Optional[str]:
        explicit = callback.get("cli_path")
        if explicit:
            if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
                return explicit
            return None

        for candidate in (
            os.environ.get("CMUX_BUNDLED_CLI_PATH"),
            "/opt/homebrew/bin/cmux",
            "/usr/local/bin/cmux",
            "/Applications/cmux.app/Contents/Resources/bin/cmux",
        ):
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    @staticmethod
    def _cmux_result_text(result: subprocess.CompletedProcess[Any]) -> str:
        return f"{result.stdout or ''}\n{result.stderr or ''}".strip()

    # CC-95: canonical stale-target phrases from cmux's own
    # `shouldIgnoreClaudeHookTeardownError` allowlist (cmux.swift:12755-12772).
    # These are the exact lowercased substrings cmux emits on stderr when a
    # workspace/surface/panel id no longer resolves. Subset chosen for
    # WakeLite's "did the callback target go stale?" question — socket-level
    # errors ("failed to write to socket", "socket read error", "not connected")
    # are excluded because they're infrastructure faults, not stale handles,
    # and WakeLite should retry/log-error on those rather than fall back to
    # new-workspace.
    _CMUX_STALE_TARGET_PHRASES: ClassVar[Tuple[str, ...]] = (
        "workspace not found",
        "workspace ref not found",
        "workspace index not found",
        "workspace target not found",
        "previous workspace not found",
        "surface not found",
        "surface ref not found",
        "surface index not found",
        "surface target not found",
        "unable to resolve surface id",
        "panel not found",
        "tab not found",
        "no workspace selected",
        "tabmanager not available",
    )

    @classmethod
    def _cmux_surface_not_found(cls, result: subprocess.CompletedProcess[Any]) -> bool:
        """Return True iff cmux's stderr indicates a stale workspace/surface
        handle. CC-95: replaced the prior fragile substring match
        (`"surface" in text AND any-of("not found"|"missing"|"unknown"|...)`)
        with an anchored allowlist of cmux's canonical phrases. The previous
        check produced false positives on phrasings like "Network unknown
        error on surface init" and false negatives on cmux's actual messages
        like "Workspace target not found" (no "surface" token). When stderr
        is non-zero but no canonical phrase matches, we log a WARNING so an
        unrecognized stale-target wording surfaces in the runner log instead
        of being silently classified as a generic failure."""
        if result.returncode == 0:
            return False
        text = cls._cmux_result_text(result).lower()
        for phrase in cls._CMUX_STALE_TARGET_PHRASES:
            if phrase in text:
                return True
        # Non-zero exit, but stderr doesn't match any known stale-target
        # phrase. Could be a real send/transport failure OR a stale-target
        # phrasing we haven't seen yet. Log so the operator can extend the
        # allowlist if it's the latter.
        if text:
            logger.warning(
                "cmux non-zero exit with unrecognized stderr (not classified as stale-target): %s",
                text,
            )
        return False

    @staticmethod
    def _cmux_run(cli_path: str, args: List[str], env: Dict[str, str], timeout: int = 5) -> Optional[subprocess.CompletedProcess[Any]]:
        try:
            return subprocess.run(
                [cli_path, *args],
                timeout=timeout,
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
        except Exception as exc:
            logger.error("cmux command failed (%s %s): %s", cli_path, " ".join(args[:2]), exc)
            return None

    def _cmux_probe_liveness(
        self,
        cli_path: str,
        env: Dict[str, str],
        surface_id: str,
    ) -> str:
        """Return alive, dead, or unknown for the current cmux surface."""
        params = json.dumps({"lines": 1, "surface_id": surface_id}, separators=(",", ":"))
        result = self._cmux_run(cli_path, ["rpc", "surface.read_text", params], env)
        if result is None:
            return "unknown"
        if result.returncode == 0:
            return "alive"
        if self._cmux_surface_not_found(result):
            return "dead"
        return "unknown"

    @staticmethod
    def _amq_wake_is_live(root: str, recipient: str) -> bool:
        """Use AMQ's public wake-check contract to verify the mailbox doorbell."""
        args = [
            AMQ_BINARY_PATH,
            "wake",
            "check",
            "--root",
            root,
            "--me",
            recipient,
            "--json",
            "--json-schema=2",
        ]
        try:
            result = subprocess.run(
                args,
                timeout=AMQ_CALLBACK_SEND_TIMEOUT_SECONDS,
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            logger.warning(
                "AMQ wake check failed for recipient %s at %s: %s; falling back to cmux",
                recipient,
                root,
                exc,
            )
            return False

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Unable to parse AMQ wake-check JSON for recipient %s at %s: %s; "
                "falling back to cmux",
                recipient,
                root,
                exc,
            )
            return False

        if not isinstance(payload, dict):
            logger.warning(
                "AMQ wake-check JSON was not an object for recipient %s at %s; "
                "falling back to cmux",
                recipient,
                root,
            )
            return False

        wake = payload.get("wake")
        accepted = (
            result.returncode == 0
            and payload.get("schema") == 2
            and payload.get("root") == root
            and payload.get("agent") == recipient
            and isinstance(wake, dict)
            and wake.get("status") == "valid"
            and wake.get("live") is True
        )
        if not accepted:
            logger.warning(
                "AMQ wake is not live and valid for recipient %s at %s "
                "(returncode=%s status=%s live=%s); falling back to cmux",
                recipient,
                root,
                result.returncode,
                wake.get("status") if isinstance(wake, dict) else None,
                wake.get("live") if isinstance(wake, dict) else None,
            )
        return accepted

    @classmethod
    def _resolve_amq_mailbox_for_surface(cls, surface_id: str) -> Optional[Tuple[str, str]]:
        """Resolve one registered mailbox whose official AMQ wake is live."""
        try:
            payload = json.loads(AMQ_KEEPALIVE_REGISTRY_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.info("AMQ keepalive registry unavailable; falling back to cmux", exc_info=True)
            return None

        entries = payload.get("entries", []) if isinstance(payload, dict) else []
        target = f"cmux:surface:{surface_id}"
        identities: set[Tuple[str, str]] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("adapter") != "cmux" or entry.get("target") != target:
                continue
            # Keepalive can report "attached" when its supervisor cannot
            # manage an owner-bound wake even though that wake is still live.
            # The registry selects the mailbox; AMQ's public wake-check result
            # below, rather than the supervisor state, proves the doorbell.
            if entry.get("state") not in ("active", "attached"):
                continue
            root = entry.get("root")
            recipient = entry.get("agent")
            if not isinstance(root, str) or not isinstance(recipient, str):
                continue
            root_path = Path(root)
            if not root_path.is_absolute() or not (root_path / "agents" / recipient).is_dir():
                continue
            identities.add((str(root_path), recipient))

        if len(identities) != 1:
            logger.warning(
                "AMQ identity resolution for surface %s found %d registered mailboxes; falling back to cmux",
                surface_id,
                len(identities),
            )
            return None
        root, recipient = next(iter(identities))
        if not cls._amq_wake_is_live(root, recipient):
            return None
        return root, recipient

    def _deliver_cmux_callback_via_amq(
        self,
        callback: Dict[str, Any],
        session_id: Optional[str],
        signal_file: Optional[Path],
    ) -> Tuple[bool, Optional[str]]:
        """Attempt AMQ delivery and require an accepted message ID."""
        if not session_id or signal_file is None:
            return False, None

        resolved = self._resolve_cmux_target_via_session_store(session_id)
        cli_path = self._resolve_cmux_cli_path(callback)
        if resolved:
            _, surface_id = resolved
        else:
            # Codex does not run Claude's cmux session-store hook, so a valid
            # Codex callback has no entry in claude-hook-sessions.json. In that
            # case the timer's captured target is usable only after a fresh,
            # affirmative liveness probe. Unknown is deliberately insufficient:
            # without either the session store or a live surface, routing would
            # suppress the direct cmux fallback and could strand the callback.
            workspace_id = callback.get("workspace_id")
            surface_id = callback.get("surface_id")
            if (
                not isinstance(workspace_id, str)
                or not workspace_id
                or not isinstance(surface_id, str)
                or not surface_id
                or not cli_path
            ):
                return False, None
            liveness = self._cmux_probe_liveness(
                cli_path,
                self._cmux_env(callback),
                surface_id,
            )
            if liveness != "alive":
                logger.info(
                    "AMQ callback session-store miss for session %s and captured "
                    "surface %s is %s; falling back to cmux",
                    session_id,
                    surface_id,
                    liveness,
                )
                return False, None
            logger.info(
                "AMQ callback session-store miss for session %s; using live "
                "captured workspace=%s surface=%s",
                session_id,
                workspace_id,
                surface_id,
            )

        if resolved and cli_path:
            liveness = self._cmux_probe_liveness(cli_path, self._cmux_env(callback), surface_id)
            if liveness == "dead":
                logger.info("AMQ callback target surface %s is dead; falling back to cmux", surface_id)
                return False, None

        mailbox = self._resolve_amq_mailbox_for_surface(surface_id)
        if not mailbox:
            return False, None
        root, recipient = mailbox

        result = subprocess.run(
            [
                AMQ_BINARY_PATH,
                "send",
                "--root",
                root,
                "--ignore-session-pin",
                "--me",
                recipient,
                "--to",
                recipient,
                "--allow-self",
                "--body",
                f"@{signal_file}",
                "--strict",
                "--json",
            ],
            timeout=AMQ_CALLBACK_SEND_TIMEOUT_SECONDS,
            check=False,
            capture_output=True,
            text=True,
        )

        parsed: Dict[str, Any] = {}
        try:
            candidate = json.loads(result.stdout)
            if isinstance(candidate, dict):
                parsed = candidate
        except json.JSONDecodeError as exc:
            logger.warning("Unable to parse AMQ callback JSON response: %s", exc)
        raw_message_id = parsed.get("id")
        message_id = (
            raw_message_id.strip()
            if isinstance(raw_message_id, str) and raw_message_id.strip()
            else None
        )
        accepted = result.returncode == 0 and message_id is not None
        if not accepted:
            logger.warning(
                "AMQ callback send was not accepted (id=%s returncode=%s); falling back to cmux",
                message_id or "none",
                result.returncode,
            )
        return accepted, message_id

    def _cmux_deliver_trigger(
        self,
        cli_path: str,
        env: Dict[str, str],
        workspace_id: str,
        surface_id: str,
        trigger: str,
        timer_id: str,
    ) -> str:
        send_result = self._cmux_run(
            cli_path,
            ["send", "--workspace", workspace_id, "--surface", surface_id, "--", trigger],
            env,
        )
        if send_result is None:
            return "failed"
        if send_result.returncode != 0:
            if self._cmux_surface_not_found(send_result):
                logger.warning(
                    "cmux callback target is stale for timer %s: %s",
                    timer_id,
                    self._cmux_result_text(send_result),
                )
                return "stale"
            logger.error(
                "cmux send failed for timer %s: %s",
                timer_id,
                self._cmux_result_text(send_result),
            )
            return "failed"

        enter_result: Optional[subprocess.CompletedProcess[Any]] = None
        for attempt in range(3):
            enter_result = self._cmux_run(
                cli_path,
                ["send-key", "--workspace", workspace_id, "--surface", surface_id, "Enter"],
                env,
            )
            if enter_result is not None and enter_result.returncode == 0:
                logger.info(
                    "cmux callback delivered to workspace %s surface %s for timer %s",
                    workspace_id,
                    surface_id,
                    timer_id,
                )
                return "delivered"
            time.sleep(1.0 * (attempt + 1))

        detail = self._cmux_result_text(enter_result) if enter_result is not None else "send-key did not run"
        logger.warning(
            "cmux partial injection for timer %s: cmux send succeeded but send-key Enter failed: %s",
            timer_id,
            detail,
        )
        return "partial"

    def _resolve_cmux_target_via_session_store(self, session_id: str) -> Optional[Tuple[str, str]]:
        store_override = os.environ.get("WAKELITE_CMUX_SESSION_STORE_PATH")
        if store_override:
            store_path = Path(store_override).expanduser()
            lock_path = Path(
                os.environ.get(
                    "WAKELITE_CMUX_SESSION_STORE_LOCK_PATH",
                    f"{store_path}.lock",
                )
            ).expanduser()
        else:
            cmux_dir = Path.home() / ".cmuxterm"
            store_path = cmux_dir / "claude-hook-sessions.json"
            lock_path = Path(f"{store_path}.lock")

        if not store_path.exists() or not lock_path.exists():
            return None

        try:
            import fcntl

            with lock_path.open("r", encoding="utf-8") as lock_file:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    logger.info("cmux session store is locked; treating as cache miss")
                    return None
                try:
                    raw = store_path.read_text(encoding="utf-8")
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            logger.info("Unable to read cmux session store; treating as cache miss", exc_info=True)
            return None

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.info("Unable to parse cmux session store; treating as cache miss")
            return None

        if isinstance(parsed, dict):
            stored_sessions = parsed.get("sessions", [])
            if isinstance(stored_sessions, dict):
                sessions = stored_sessions.values()
            elif isinstance(stored_sessions, list):
                sessions = stored_sessions
            else:
                sessions = []
        elif isinstance(parsed, list):
            sessions = parsed
        else:
            sessions = []

        matches = [
            entry for entry in sessions
            if isinstance(entry, dict)
            and entry.get("sessionId") == session_id
            and isinstance(entry.get("workspaceId"), str)
            and isinstance(entry.get("surfaceId"), str)
        ]
        if not matches:
            return None

        # CC-95: normalize numeric-epoch or ISO `updatedAt` values to datetime
        # instead of lex-sorting strings. Lex-sort breaks on mixed timezone
        # formats — the same instant
        # serialized as `...+00:00` lex-orders earlier than `...Z` because
        # `+` (0x2B) < `Z` (0x5A). Datetime parsing collapses these to the
        # same instant. Also enforce a 24h freshness cutoff so a long-stale
        # entry that happens to be the lex-greatest doesn't win — past 24h
        # we'd rather fall through to new-workspace than route to a session
        # the user has almost certainly closed. cmux's own session store
        # auto-prunes after 7 days (cmux.swift:339), so 24h is well inside
        # the data lifecycle.
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        dated: List[Tuple[datetime, Dict[str, Any]]] = []
        for entry in matches:
            updated_at = self._parse_session_updated_at(entry.get("updatedAt"))
            if updated_at is None:
                continue
            if updated_at < cutoff:
                continue
            dated.append((updated_at, entry))

        if not dated:
            logger.info(
                "cmux session store has no fresh entry for session %s within 24h cutoff",
                session_id,
            )
            return None

        dated.sort(key=lambda x: x[0])
        updated_at, freshest = dated[-1]
        age_seconds = max(0, int((now - updated_at).total_seconds()))
        logger.info(
            "cmux session store routed session %s to workspace=%s surface=%s (age=%ds)",
            session_id,
            freshest["workspaceId"],
            freshest["surfaceId"],
            age_seconds,
        )
        return freshest["workspaceId"], freshest["surfaceId"]

    @staticmethod
    def _parse_session_updated_at(value: Any) -> Optional[datetime]:
        """Parse a cmux session-store `updatedAt` field into an aware UTC
        datetime. Live cmux stores a numeric Unix epoch; ISO 8601 strings,
        including offsets and naive timestamps, remain accepted for
        compatibility. Returns None on unparseable input — caller treats it
        as "no usable timestamp"."""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                return datetime.fromtimestamp(value, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        if not isinstance(value, str) or not value:
            return None
        try:
            # Python 3.11+ accepts `Z` suffix natively in fromisoformat.
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            # cmux emits UTC; treat naive timestamps as UTC rather than
            # silently assuming local time.
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _cmux_workspace_id_from_new_workspace(result: subprocess.CompletedProcess[Any]) -> Optional[str]:
        output = (result.stdout or "").strip()
        if not output:
            return None
        last_line = output.splitlines()[-1].strip()
        parts = last_line.split()
        if len(parts) >= 2 and parts[0] == "OK":
            return parts[1]
        return parts[-1] if parts else None

    @staticmethod
    def _cmux_screen_ready(text: str) -> bool:
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        if not lines:
            return False
        last = lines[-1]
        prompt_markers = ("$", "%", "#", ">", "❯", "➜")
        return any(last.endswith(marker) for marker in prompt_markers)

    def _cmux_new_workspace_fallback(
        self,
        cli_path: str,
        env: Dict[str, str],
        timer: Dict[str, Any],
        session_id: str,
    ) -> bool:
        timer_id = timer.get("id", "unknown")
        timer_name = timer.get("name", timer_id)
        cwd = timer.get("command", {}).get("workingDirectory") or str(Path.home())
        result = self._cmux_run(
            cli_path,
            ["new-workspace", "--name", f"WakeLite: {timer_name}", "--cwd", cwd],
            env,
            timeout=10,
        )
        if result is None:
            return False
        if result.returncode != 0:
            logger.error("cmux new-workspace fallback failed for timer %s: %s", timer_id, self._cmux_result_text(result))
            return False

        workspace_id = self._cmux_workspace_id_from_new_workspace(result)
        if not workspace_id:
            logger.error("cmux new-workspace fallback returned no workspace id for timer %s", timer_id)
            return False

        ready = False
        for attempt in range(10):
            screen = self._cmux_run(
                cli_path,
                ["read-screen", "--workspace", workspace_id, "--lines", "5"],
                env,
            )
            if screen is not None and screen.returncode == 0 and self._cmux_screen_ready(screen.stdout or ""):
                ready = True
                break
            time.sleep(1.0)

        if not ready:
            logger.warning(
                "cmux new-workspace fallback did not reach a prompt for timer %s; signal file preserved",
                timer_id,
            )
            return False

        # CC-95: type the resume command via argv (no shell, so shlex.quote is unnecessary
        # and would inject literal quote chars). Submit it with an explicit `send-key Enter`
        # — `cmux send` does not interpret \n / \r as Enter, so a trailing escape leaves the
        # command sitting at the prompt unsubmitted. Match the 3x backoff in
        # _cmux_deliver_trigger so transient send-key failures don't strand the resume.
        resume = f"claude --resume {session_id}"
        send_resume = self._cmux_run(
            cli_path,
            ["send", "--workspace", workspace_id, "--", resume],
            env,
        )
        if send_resume is None or send_resume.returncode != 0:
            detail = self._cmux_result_text(send_resume) if send_resume is not None else "send did not run"
            logger.error("cmux resume send failed for timer %s: %s", timer_id, detail)
            return False

        enter_result: Optional[subprocess.CompletedProcess[Any]] = None
        for attempt in range(3):
            enter_result = self._cmux_run(
                cli_path,
                ["send-key", "--workspace", workspace_id, "Enter"],
                env,
            )
            if enter_result is not None and enter_result.returncode == 0:
                logger.info(
                    "cmux new-workspace fallback delivered claude --resume to workspace %s for timer %s",
                    workspace_id,
                    timer_id,
                )
                return True
            time.sleep(1.0 * (attempt + 1))

        detail = self._cmux_result_text(enter_result) if enter_result is not None else "send-key did not run"
        logger.warning(
            "cmux new-workspace fallback partial-injection for timer %s: send succeeded but send-key Enter failed: %s",
            timer_id,
            detail,
        )
        return False

    def _wezterm_callback(self, callback: Dict[str, Any], message: str, timer: Dict[str, Any],
                          timer_name: str = "", status: str = "",
                          slack_thread_ts: Optional[str] = None) -> None:
        """Send short trigger to WezTerm pane, with Slack + resume fallback.

        If a signal file was written (session_id present), sends only a short
        trigger message. The UserPromptSubmit hook reads the full data from the
        signal file. Falls back to full message if no session_id.
        """
        pane_id = callback.get("pane_id")
        session_id = callback.get("session_id")

        # Use short trigger if signal file was written, else full message
        if session_id and timer_name:
            trigger = f"[WakeLite: {timer_name} completed ({status})]"
        else:
            trigger = message

        if pane_id is not None:
            # Check if pane still exists
            try:
                result = subprocess.run(
                    ["wezterm", "cli", "list", "--format", "json"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    panes = json.loads(result.stdout)
                    pane_exists = any(p.get("pane_id") == pane_id for p in panes)
                    if pane_exists:
                        # Happy path: activate and send short trigger
                        subprocess.run(
                            ["wezterm", "cli", "activate-pane", "--pane-id", str(pane_id)],
                            capture_output=True, timeout=5,
                        )
                        subprocess.run(
                            ["wezterm", "cli", "send-text", "--pane-id", str(pane_id), trigger],
                            capture_output=True, timeout=5,
                        )
                        time.sleep(2.0)  # Claude Code TUI needs time to settle after paste
                        # --no-paste so the newline is a raw Enter keypress
                        # Retry up to 3 times with increasing delay
                        for attempt in range(3):
                            enter_result = subprocess.run(
                                ["wezterm", "cli", "send-text", "--no-paste", "--pane-id", str(pane_id), "\r"],
                                capture_output=True, timeout=5,
                            )
                            if enter_result.returncode == 0:
                                break
                            time.sleep(1.0 * (attempt + 1))
                        logger.info("Callback delivered to pane %d for timer %s (enter rc=%d)", pane_id, timer.get("id"), enter_result.returncode)
                        return
            except Exception:
                logger.warning("WezTerm pane check failed for pane %d", pane_id, exc_info=True)

        # Fallback: pane gone or no pane_id
        self._wezterm_fallback(callback, message, timer, slack_thread_ts=slack_thread_ts)

    def _wezterm_fallback(self, callback: Dict[str, Any], message: str, timer: Dict[str, Any],
                          slack_thread_ts: Optional[str] = None) -> None:
        """Fallback: Slack DM + optionally spawn new WezTerm tab with claude --resume."""
        timer_name = timer.get("name", timer.get("id", "unknown"))
        self.notifier.notify_slack(
            f":warning: *Callback fallback* — pane gone\n"
            f"Timer: *{timer_name}*\n"
            f"Results delivered to new tab (or Slack only if no session_id).\n\n"
            f"```\n{message[:1500]}\n```",
            thread_ts=slack_thread_ts,
        )

        session_id = callback.get("session_id")
        if not session_id:
            logger.info("No session_id for callback fallback, Slack only for timer %s", timer.get("id"))
            return

        try:
            cwd = timer.get("command", {}).get("workingDirectory") or str(Path.home())
            result = subprocess.run(
                ["wezterm", "cli", "spawn", "--cwd", cwd, "--", "claude", "--resume", session_id],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                new_pane = result.stdout.strip()
                logger.info("Spawned resume tab (pane %s) for session %s", new_pane, session_id)
                time.sleep(3)  # Wait for Claude Code to initialize
                subprocess.run(
                    ["wezterm", "cli", "send-text", "--pane-id", new_pane, message],
                    capture_output=True, timeout=5,
                )
                time.sleep(1.0)  # Claude Code TUI needs time to process pasted text
                for attempt in range(3):
                    enter_result = subprocess.run(
                        ["wezterm", "cli", "send-text", "--no-paste", "--pane-id", new_pane, "\r"],
                        capture_output=True, timeout=5,
                    )
                    if enter_result.returncode == 0:
                        break
                    time.sleep(0.5 * (attempt + 1))
            else:
                logger.warning("Failed to spawn resume tab: %s", result.stderr)
        except Exception:
            logger.warning("Resume spawn failed for session %s", session_id, exc_info=True)

    # ── Ghostty callback ──────────────────────────────────────────────

    @staticmethod
    def _resolve_terminal_id(callback: Dict[str, Any]) -> Optional[str]:
        """Resolve the freshest Ghostty terminal ID for this session.

        Checks the terminal-registry.jsonl (written by SessionStart hook)
        for the most recent entry matching this session_id. Falls back to
        the terminal_id stored at timer creation time.
        """
        session_id = callback.get("session_id")
        stored_id = callback.get("terminal_id")

        if not session_id:
            return stored_id

        registry = Path.home() / ".claude" / "session-signals" / "terminal-registry.jsonl"
        if not registry.exists():
            return stored_id

        # Read registry, find freshest entry for this session
        freshest_id = None
        try:
            for line in registry.read_text().strip().splitlines():
                try:
                    entry = json.loads(line)
                    if entry.get("session_id") == session_id:
                        freshest_id = entry.get("terminal_id")
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

        return freshest_id or stored_id

    def _ghostty_callback(self, callback: Dict[str, Any], message: str, timer: Dict[str, Any],
                           timer_name: str = "", status: str = "",
                           slack_thread_ts: Optional[str] = None) -> None:
        """Send short trigger to Ghostty terminal via AppleScript, with Slack + resume fallback."""
        terminal_id = self._resolve_terminal_id(callback)
        session_id = callback.get("session_id")

        if session_id and timer_name:
            trigger = f"[WakeLite: {timer_name} completed ({status})]"
        else:
            trigger = message

        if terminal_id:
            try:
                # Check terminal exists
                result = subprocess.run(
                    ["osascript", "-e",
                     f'tell application "Ghostty" to exists terminal id "{terminal_id}"'],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip() == "true":
                    # Focus terminal
                    subprocess.run(
                        ["osascript", "-e",
                         f'tell application "Ghostty" to focus terminal id "{terminal_id}"'],
                        capture_output=True, timeout=5,
                    )
                    # Inject text (paste mode)
                    escaped = trigger.replace('\\', '\\\\').replace('"', '\\"')
                    subprocess.run(
                        ["osascript", "-e",
                         f'tell application "Ghostty" to input text "{escaped}" to terminal id "{terminal_id}"'],
                        capture_output=True, timeout=5,
                    )
                    time.sleep(2.0)  # Claude Code TUI needs time to settle
                    # Press Enter
                    for attempt in range(3):
                        enter_result = subprocess.run(
                            ["osascript", "-e",
                             f'tell application "Ghostty" to send key "enter" to terminal id "{terminal_id}"'],
                            capture_output=True, timeout=5,
                        )
                        if enter_result.returncode == 0:
                            break
                        time.sleep(1.0 * (attempt + 1))
                    logger.info("Ghostty callback delivered to terminal %s for timer %s",
                                terminal_id, timer.get("id"))
                    return
            except Exception:
                logger.warning("Ghostty terminal check failed for %s", terminal_id, exc_info=True)

        # Fallback: terminal gone or no terminal_id
        self._ghostty_fallback(callback, message, timer, slack_thread_ts=slack_thread_ts)

    def _ghostty_fallback(self, callback: Dict[str, Any], message: str, timer: Dict[str, Any],
                           slack_thread_ts: Optional[str] = None) -> None:
        """Fallback: Slack DM + optionally spawn new Ghostty tab with claude --resume."""
        timer_name = timer.get("name", timer.get("id", "unknown"))
        self.notifier.notify_slack(
            f":warning: *Callback fallback* — terminal gone\n"
            f"Timer: *{timer_name}*\n"
            f"Results delivered to new tab (or Slack only if no session_id).\n\n"
            f"```\n{message[:1500]}\n```",
            thread_ts=slack_thread_ts,
        )

        session_id = callback.get("session_id")
        if not session_id:
            logger.info("No session_id for ghostty callback fallback, Slack only for timer %s", timer.get("id"))
            return

        try:
            cwd = timer.get("command", {}).get("workingDirectory") or str(Path.home())
            escaped_cwd = cwd.replace('\\', '\\\\').replace('"', '\\"')
            result = subprocess.run(
                ["osascript", "-e", f'''tell application "Ghostty"
    set cfg to new surface configuration
    set command of cfg to "claude --resume {session_id}"
    set initial working directory of cfg to "{escaped_cwd}"
    new tab with configuration cfg
end tell'''],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                logger.info("Spawned Ghostty resume tab for session %s", session_id)
                time.sleep(3)  # Wait for Claude Code to initialize
                # No need to inject text — claude --resume picks up the signal file
            else:
                logger.warning("Failed to spawn Ghostty resume tab: %s", result.stderr)
        except Exception:
            logger.warning("Ghostty resume spawn failed for session %s", session_id, exc_info=True)

    def _run_occurrence(
        self,
        timer: Dict[str, Any],
        scheduled_at: str,
        is_catchup: bool,
        queued_reason: Optional[str],
        retry_of_run_id: Optional[str],
    ) -> None:
        timer_id = timer["id"]

        run_ctx = self.state.create_run(
            timer_id=timer_id,
            timer_name=timer.get("name", timer_id),
            scheduled_at=scheduled_at,
            is_catchup=is_catchup,
            queued_reason=queued_reason,
            retry_of_run_id=retry_of_run_id,
            timer_snapshot=json.dumps(timer),
        )
        run_id = run_ctx["run_id"]
        self.state.set_runtime_running(timer_id, run_id, scheduled_at)

        notifications = timer.get("notifications") or {}
        slack_activity_enabled = notifications.get("slackActivity", True)

        # For timers not bound to a specific session, create a Slack thread
        # so all notifications for this run are grouped under one parent.
        # Existing timers omit slackActivity, so True is the compatibility
        # default; explicit False makes this entire lifecycle Slack-silent.
        slack_thread_ts: Optional[str] = None
        callback = timer.get("callback") or {}
        session_bound = callback.get("type") in ("wezterm", "ghostty", "cmux") and bool(callback.get("session_id"))
        if slack_activity_enabled and not session_bound:
            try:
                timer_name = timer.get("name", timer_id)
                slack_thread_ts = self.notifier.get_daily_thread_ts()
                if slack_thread_ts:
                    self.notifier.notify_slack(
                        f":hourglass_flowing_sand: Timer *{timer_name}* started\n"
                        f"Run: `{run_id}`\n"
                        f"Scheduled: {scheduled_at}",
                        thread_ts=slack_thread_ts,
                    )
            except Exception:
                slack_thread_ts = None
                logger.warning("Slack thread setup failed for timer %s; continuing", timer_id, exc_info=True)

        date_dir = datetime.now().strftime("%Y-%m-%d")
        run_dir = LOG_DIR / timer_id / date_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = run_dir / f"{run_id}.out.log"
        stderr_path = run_dir / f"{run_id}.err.log"

        command = timer.get("command", {})
        env = os.environ.copy()
        env.update({k: str(v) for k, v in (command.get("env") or {}).items()})
        cwd = command.get("workingDirectory") or str(Path.home())

        status = "failed"
        exit_code = None
        message = ""
        notify_on_success = bool(notifications.get("onSuccess", False))
        notify_on_failure = bool(notifications.get("onFailure", True))
        run_start_mono = time.monotonic()

        try:
            with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
                with self._run_lock:
                    pre_start_abort = run_id in self._abort_requests

                if pre_start_abort:
                    status = "aborted"
                    exit_code = -15
                    message = "aborted by user"
                else:
                    if command.get("mode") == "shell":
                        cmd = ["/bin/zsh", "-lc", command.get("shell", "")]
                    else:
                        cmd = [command.get("executable", "")] + list(command.get("args") or [])

                    proc = subprocess.Popen(
                        cmd,
                        stdout=out,
                        stderr=err,
                        cwd=cwd,
                        env=env,
                        start_new_session=True,
                    )
                    with self._run_lock:
                        self._active_processes[run_id] = proc
                    self.state.update_active_run_pid(run_id, proc.pid)

                    exit_code = proc.wait()

                    with self._run_lock:
                        abort_requested = run_id in self._abort_requests

                    if abort_requested:
                        status = "aborted"
                        message = "aborted by user"
                        if exit_code is None:
                            exit_code = -15
                    elif exit_code == 0:
                        status = "success"
                        message = "completed"
                    elif exit_code == 75:
                        status = "waiting"
                        message = "not ready yet (EX_TEMPFAIL)"
                    else:
                        status = "failed"
                        message = f"exit code {exit_code}"
        except Exception as exc:
            status = "failed"
            message = str(exc)
            exit_code = -1
        finally:
            with self._run_lock:
                self._active_processes.pop(run_id, None)
                self._abort_requests.discard(run_id)

        self.state.finish_run(
            run_id=run_id,
            timer_id=timer_id,
            scheduled_at=scheduled_at,
            status=status,
            exit_code=exit_code,
            message=message,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )

        # Fire callback if configured (success/failed only)
        duration_seconds = time.monotonic() - run_start_mono
        self._execute_callback(timer, status, exit_code, run_id, str(stdout_path), duration_seconds, slack_thread_ts=slack_thread_ts)

        runtime = self.state.set_runtime_idle(timer_id, run_id=run_id)

        # Update daemon state if this is a daemon timer
        if timer.get("timer_type") == "daemon":
            self.state.set_daemon_state(
                timer_id,
                status="stopped",
                last_exited_at=self.state._now(),
                last_exit_code=exit_code,
                current_run_id=None,
            )

        # Check until condition — auto-delete timer if condition met
        if self._check_until_condition(timer, status):
            trigger = "on_success" if status == "success" else "on_failure"
            timer_name = timer.get("name", timer_id)
            logger.info("Timer %s until condition met (%s=delete), deleting", timer_id, trigger)
            deleted = False
            try:
                self.timer_store.delete_timer(timer_id)
                deleted = True
            except Exception:
                logger.warning("Failed to auto-delete timer %s after until condition", timer_id, exc_info=True)
            if deleted:
                # Drain orphaned queue entry before returning
                self.state.pop_queue_once(timer_id)
                self.notifier.notify(
                    "WakeLite: timer completed",
                    f"{timer_name} — {trigger}=delete triggered. Timer auto-deleted.",
                )
                if slack_activity_enabled:
                    self.notifier.notify_slack(
                        f":wastebasket: Timer auto-deleted: *{timer_name}*\n"
                        f"Condition: `{trigger}=delete` triggered\n"
                        f"Exit code: {exit_code}",
                        thread_ts=slack_thread_ts,
                    )
                self._signal_wake()
                return
            # Delete failed — fall through to normal post-run processing

        if status not in ("success", "aborted", "waiting"):
            self.state.add_incident(
                "error",
                "run_failed",
                f"Timer {timer_id} failed at {scheduled_at}: {message}",
                timer_id,
            )
            if notify_on_failure:
                self.notifier.notify("WakeLite failure", f"{timer.get('name', timer_id)} failed: {message}")
        elif notify_on_success:
            self.notifier.notify(
                "WakeLite success",
                f"{timer.get('name', timer_id)} completed at {scheduled_at}",
            )

        # Always close the Slack thread with a status reply (if we opened one).
        # This is independent of macOS notification preferences — the thread parent
        # already created the Slack "noise", so leaving it without a reply is worse.
        if slack_thread_ts:
            if status == "aborted":
                emoji, label = ":stop_sign:", "Aborted"
            elif status == "waiting":
                emoji, label = ":large_blue_circle:", "Waiting"
            elif status == "success":
                emoji, label = ":white_check_mark:", "Success"
            else:
                emoji, label = ":x:", "Failed"
            duration_str = self._format_duration(time.monotonic() - run_start_mono)
            self.notifier.notify_slack(
                f"{emoji} *{label}* — {timer.get('name', timer_id)}\n"
                f"Exit code: {exit_code} | Duration: {duration_str}\n"
                f"{message}",
                thread_ts=slack_thread_ts,
            )

        queued_scheduled = self.state.pop_queue_once(timer_id)
        if queued_scheduled:
            fresh_timer = self.timer_store.get_timer(timer_id)
            if fresh_timer and fresh_timer.get("enabled", True):
                self._spawn_run(
                    fresh_timer,
                    queued_scheduled,
                    is_catchup=True,
                    queued_reason="queued_once",
                    retry_of_run_id=run_id,
                )

        # Signal scheduler to re-evaluate (daemon restart, queued timer, interval reset)
        self._signal_wake()

    def _process_interval_due(self, now: datetime) -> None:
        """Fast-tick: check interval timers and fire if due."""
        for timer in self.timer_store.list_timers():
            if not timer.get("enabled", True):
                continue
            if timer.get("timer_type") == "daemon":
                continue  # daemons handled by _process_daemons
            rec = timer.get("recurrence", {})
            if rec.get("frequency") != "interval":
                continue

            timer_id = timer["id"]

            max_runs = timer.get("max_runs")
            if max_runs is not None:
                completed = self.state.count_completed_runs(timer_id)
                if completed >= max_runs:
                    logger.info("Timer %s reached max_runs=%d (%d completed), auto-disabling", timer.get("name"), max_runs, completed)
                    try:
                        self.timer_store.set_enabled(timer_id, False)
                    except Exception:
                        logger.warning("Failed to auto-disable timer %s", timer_id, exc_info=True)
                    continue

            recurrence = parse_recurrence(timer)
            every_seconds = recurrence.every_seconds or 0

            meta_key = f"interval.last_fired.{timer_id}"
            last_fired_raw = self.state.get_meta(meta_key)
            last_fired = None
            if last_fired_raw:
                try:
                    last_fired = datetime.fromisoformat(last_fired_raw)
                except ValueError:
                    pass

            # Windowed interval: fire based on fixed daily schedule
            if recurrence.active_hours_start:
                today_fires = interval_window_occurrences(recurrence, now.date())
                should_fire = False
                for fire_time in today_fires:
                    if fire_time <= now and (last_fired is None or last_fired < fire_time):
                        should_fire = True
                        break
                if not should_fire:
                    continue
            elif not interval_is_due(last_fired, now, every_seconds):
                continue

            scheduled_at = now.isoformat(timespec="seconds")
            reserved = self.state.reserve_occurrence(timer_id, scheduled_at, False)
            if not reserved:
                continue

            self.state.set_meta(meta_key, now.isoformat())
            self._enqueue_or_spawn(
                timer=timer,
                scheduled_at=scheduled_at,
                is_catchup=False,
                queued_reason=None,
                retry_of_run_id=None,
            )

    # Tolerance window between _spawn_run submitting to the executor and the
    # worker thread actually registering a PID. Without this, a busy runner can
    # race its own daemon spawn and clear the runtime row before Popen happens.
    DAEMON_SPAWN_GRACE_SECONDS = 30

    def _is_daemon_process_alive(self, timer_id: str, run_id: Optional[str]) -> bool:
        """Check if the daemon process is actually alive via in-memory dict or OS PID check."""
        if run_id and run_id in self._active_processes:
            proc = self._active_processes[run_id]
            return proc.poll() is None  # None = still running
        # Process not in memory (runner restarted?) — check DB PID
        if run_id:
            pid = self.state.get_active_run_pid(run_id)
            if pid is not None:
                try:
                    os.kill(pid, 0)  # signal 0 = existence check
                    return True
                except ProcessLookupError:
                    return False
                except PermissionError:
                    return True  # process exists but we can't signal it
            run = self.state.get_run(run_id)
            if run:
                started_iso = run.get("started_at") or run.get("created_at")
                if started_iso:
                    try:
                        started_at = datetime.fromisoformat(str(started_iso).replace("Z", "+00:00"))
                        if started_at.tzinfo is None:
                            started_at = started_at.replace(tzinfo=timezone.utc)
                        age = (datetime.now(timezone.utc) - started_at).total_seconds()
                        if age < self.DAEMON_SPAWN_GRACE_SECONDS:
                            return True
                    except (TypeError, ValueError, AttributeError):
                        pass
        return False  # no run_id, no PID, or grace window exceeded — ghost

    def _process_daemons(self, now: datetime) -> None:
        """Fast-tick: ensure daemon timers are running; restart if crashed."""
        for timer in self.timer_store.list_timers():
            if not timer.get("enabled", True):
                continue
            if timer.get("timer_type") != "daemon":
                continue

            timer_id = timer["id"]
            ds = self.state.get_daemon_state(timer_id)
            runtime = self.state.get_runtime(timer_id)

            if runtime.is_running:
                # Verify process is actually alive — guard against ghost runs
                if not self._is_daemon_process_alive(timer_id, runtime.running_run_id):
                    logger.warning("Daemon %s has ghost run %s — clearing stale state", timer_id, runtime.running_run_id)
                    if runtime.running_run_id:
                        run = self.state.get_run(runtime.running_run_id) or {}
                        self.state.finish_run(
                            run_id=runtime.running_run_id,
                            timer_id=timer_id,
                            scheduled_at=run.get("scheduled_at") or self.state._now(),
                            status="failed",
                            exit_code=-1,
                            message="ghost run: process not alive",
                            stdout_path=None,
                            stderr_path=None,
                        )
                    self.state.set_runtime_idle(timer_id, runtime.running_run_id)
                    runtime = self.state.get_runtime(timer_id)
                    # Fall through to restart logic below
                else:
                    continue  # actually running, nothing to do

            if ds.status == "running":
                # DB says running but runtime says idle => process exited
                self.state.set_daemon_state(
                    timer_id,
                    status="stopped",
                    last_exited_at=self.state._now(),
                )
                ds = self.state.get_daemon_state(timer_id)

            execution = timer.get("execution", {})
            restart_on_failure = execution.get("restart_on_failure", True)
            restart_delay = execution.get("restart_delay_seconds", 5)
            max_backoff = execution.get("restart_max_backoff_seconds", 300)

            if ds.status == "stopped" and ds.last_exited_at:
                # Previously ran and exited — always restart daemon timers
                # (daemon contract: run forever while enabled; use disable to stop)
                if ds.last_exit_code != 0 and not restart_on_failure:
                    continue

                # Check backoff
                backoff = ds.current_backoff_seconds or restart_delay
                try:
                    exited_at = datetime.fromisoformat(ds.last_exited_at.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - exited_at).total_seconds()
                except (ValueError, AttributeError):
                    elapsed = backoff + 1  # force restart if timestamp parse fails

                if elapsed < backoff:
                    continue  # still in backoff period

                # Increase backoff for next failure
                next_backoff = min(backoff * 2, max_backoff)
                self.state.set_daemon_state(timer_id, current_backoff_seconds=next_backoff)

            # Start the daemon
            scheduled_at = now.isoformat(timespec="seconds")
            self.state.reserve_occurrence(timer_id, scheduled_at, False)
            self.state.set_daemon_state(
                timer_id,
                status="running",
                last_started_at=self.state._now(),
                restart_count=ds.restart_count + (1 if ds.last_exited_at else 0),
            )
            self._spawn_run(
                timer=timer,
                scheduled_at=scheduled_at,
                is_catchup=False,
                queued_reason="daemon_start",
                retry_of_run_id=None,
            )
            logger.info("Started daemon timer %s (restart_count=%d)", timer_id, ds.restart_count)

    def _export_wake_intents(self, now: datetime) -> None:
        events: List[Dict[str, Any]] = []
        for timer in self.timer_store.list_timers():
            if not timer.get("enabled", True):
                continue
            wake = timer.get("wake", {})
            if not wake.get("enabled", False):
                continue

            action = wake.get("action", "wake")
            lead_minutes = int(wake.get("leadMinutes", 0))
            for occ in upcoming_occurrences(timer, now, DEFAULT_HORIZON_DAYS):
                wake_time = occ - timedelta(minutes=lead_minutes)
                if wake_time <= now:
                    continue
                events.append(
                    {
                        "timer_id": timer["id"],
                        "timer_name": timer.get("name", timer["id"]),
                        "command_time": occ.isoformat(timespec="seconds"),
                        "wake_time": wake_time.isoformat(timespec="seconds"),
                        "action": action,
                        "owner": OWNER,
                    }
                )

        events.sort(key=lambda e: e["wake_time"])
        payload = {
            "version": "v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "owner": OWNER,
            "events": events,
        }
        atomic_write_json(WAKE_INTENTS_FILE, payload)

    def _maybe_emit_morning_digest(self, now: datetime) -> None:
        if not self.notifier.should_emit_morning_digest(now):
            return

        day_key = now.strftime("%Y-%m-%d")
        emitted = self.state.get_meta("digest.last_day")
        if emitted == day_key:
            return

        runs = self.state.list_runs(limit=200)
        since = datetime.now(timezone.utc) - timedelta(hours=12)

        ok = 0
        failed = 0
        skipped = 0
        for run in runs:
            created = run.get("created_at")
            if not created:
                continue
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                continue
            if created_dt < since:
                continue
            status = run.get("status")
            if status == "success":
                ok += 1
            elif status in ("failed", "uncertain_crash"):
                failed += 1
            elif status == "skipped":
                skipped += 1

        if ok == 0 and failed == 0 and skipped == 0:
            return

        self.notifier.notify(
            "WakeLite overnight summary",
            f"success={ok}, failed={failed}, skipped={skipped}",
        )
        self.state.set_meta("digest.last_day", day_key)

    def _prune(self) -> None:
        summary = self.state.prune_old_data(DEFAULT_RETENTION_DAYS)
        log_summary = self._prune_run_log_files(RUN_LOG_RETENTION_DAYS)
        registry_pruned = self._prune_terminal_registry(max_age_hours=24)
        signals_pruned = self._prune_signal_files(max_age_hours=24)
        logger.info("Pruned old data: db=%s run_logs=%s registry=%d signals=%d",
                     summary, log_summary, registry_pruned, signals_pruned)

    def _repair_stale_runtime_locks(self) -> set[str]:
        repaired: set[str] = set()
        for row in self.state.list_runtime():
            if not row.get("is_running"):
                continue
            timer_id = row["timer_id"]
            running_run_id = row.get("running_run_id")
            run = self.state.get_run(running_run_id) if running_run_id else None
            if run and run.get("status") == "started":
                continue
            self.state.set_runtime_idle(timer_id)
            repaired.add(timer_id)
            self.state.add_incident(
                "warn",
                "runtime_lock_repaired",
                f"Cleared stale runtime lock for timer {timer_id} (run_id={running_run_id or 'none'})",
                timer_id,
            )
        return repaired

    def _replay_queued_once(self, timer_id: str) -> None:
        runtime = self.state.get_runtime(timer_id)
        if not runtime.queued_once or not runtime.queued_scheduled_at:
            return
        queued_scheduled = self.state.pop_queue_once(timer_id)
        if not queued_scheduled:
            return
        timer = self.timer_store.get_timer(timer_id)
        if not timer or not timer.get("enabled", True):
            return
        self._enqueue_or_spawn(
            timer=timer,
            scheduled_at=queued_scheduled,
            is_catchup=True,
            queued_reason="recovered_queue",
            retry_of_run_id=None,
        )

    @staticmethod
    def _path_exists(path: Optional[str]) -> bool:
        if not path:
            return False
        try:
            return Path(path).exists()
        except Exception:
            return False

    @staticmethod
    def _ghostty_terminal_alive(terminal_id: str) -> Optional[bool]:
        """Check if a Ghostty terminal still exists. Returns None if Ghostty is unreachable."""
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 f'tell application "Ghostty" to exists terminal id "{terminal_id}"'],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                return result.stdout.strip() == "true"
            return None  # Ghostty not running or errored
        except Exception:
            return None

    @staticmethod
    def _prune_terminal_registry(max_age_hours: int = 24) -> int:
        """Prune stale entries from terminal-registry.jsonl.

        Smart eviction: asks Ghostty if each terminal still exists.
        Falls back to TTL if Ghostty is unreachable.
        """
        registry = Path.home() / ".claude" / "session-signals" / "terminal-registry.jsonl"
        if not registry.exists():
            return 0
        cutoff = time.time() - (max_age_hours * 3600)

        # Probe Ghostty once to see if it's reachable
        ghostty_available = False
        try:
            probe = subprocess.run(
                ["osascript", "-e", 'tell application "Ghostty" to return name of front window'],
                capture_output=True, text=True, timeout=3,
            )
            ghostty_available = probe.returncode == 0
        except Exception:
            pass

        kept = []
        pruned = 0
        try:
            for line in registry.read_text().strip().splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    pruned += 1
                    continue

                tid = entry.get("terminal_id")
                ts = entry.get("ts", 0)

                if ghostty_available and tid:
                    # Smart: ask Ghostty if terminal is alive
                    alive = WakeLiteService._ghostty_terminal_alive(tid)
                    if alive is False:
                        pruned += 1
                        continue
                    # alive is True or None (error) — keep it
                    kept.append(line)
                else:
                    # Fallback: TTL-based
                    if ts >= cutoff:
                        kept.append(line)
                    else:
                        pruned += 1

            if pruned > 0:
                registry.write_text("\n".join(kept) + "\n" if kept else "")
        except Exception:
            logger.warning("Failed to prune terminal registry", exc_info=True)
        return pruned

    @staticmethod
    def _prune_signal_files(max_age_hours: int = 24) -> int:
        """Remove stale wakelite-callback signal files older than max_age_hours."""
        signal_dir = Path.home() / ".claude" / "session-signals"
        if not signal_dir.exists():
            return 0
        pruned = 0
        cutoff = time.time() - (max_age_hours * 3600)
        try:
            for f in signal_dir.glob("*.wakelite-callback.json"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    pruned += 1
        except Exception:
            logger.warning("Failed to prune signal files", exc_info=True)
        return pruned

    def _prune_run_log_files(self, retention_days: int) -> Dict[str, int]:
        files_deleted = 0
        dirs_deleted = 0
        errors = 0

        cutoff_ts = time.time() - (retention_days * 86400)
        run_log_name_pattern = re.compile(r"^[0-9a-fA-F-]{8,}\.(out|err)\.log$")
        day_dir_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")

        if LOG_DIR.exists():
            for file_path in LOG_DIR.rglob("*.log"):
                # Prune only per-run stdout/stderr logs under:
                # ~/.wakelite/logs/<timer_id>/<YYYY-MM-DD>/<run_id>.out|err.log
                if not file_path.name.endswith((".out.log", ".err.log")):
                    continue
                if not run_log_name_pattern.match(file_path.name):
                    continue
                try:
                    rel = file_path.relative_to(LOG_DIR)
                except ValueError:
                    continue
                if len(rel.parts) != 3:
                    continue
                _, date_part, _ = rel.parts
                if not day_dir_pattern.match(date_part):
                    continue
                try:
                    if not file_path.is_file():
                        continue
                    if file_path.stat().st_mtime < cutoff_ts:
                        file_path.unlink(missing_ok=True)
                        files_deleted += 1
                except Exception:
                    errors += 1

            # Remove empty timer/date folders once old files are deleted.
            dirs = sorted((p for p in LOG_DIR.rglob("*") if p.is_dir()), key=lambda p: len(str(p)), reverse=True)
            for dir_path in dirs:
                if dir_path == LOG_DIR:
                    continue
                try:
                    if any(dir_path.iterdir()):
                        continue
                    dir_path.rmdir()
                    dirs_deleted += 1
                except Exception:
                    errors += 1

        return {
            "files_deleted": files_deleted,
            "dirs_deleted": dirs_deleted,
            "errors": errors,
            "retention_days": retention_days,
        }

    @staticmethod
    def _terminate_process(proc: subprocess.Popen[Any], graceful_timeout: float = 5.0) -> bool:
        forced_kill = False

        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return forced_kill
        except Exception:
            try:
                proc.terminate()
            except Exception:
                return forced_kill

        deadline = time.time() + max(0.1, graceful_timeout)
        while time.time() < deadline:
            if proc.poll() is not None:
                return forced_kill
            time.sleep(0.1)

        forced_kill = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return forced_kill
        except Exception:
            try:
                proc.kill()
            except Exception:
                return forced_kill

        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass

        return forced_kill
