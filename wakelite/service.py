from __future__ import annotations

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
from typing import Any, Callable, Dict, List, Optional

from concurrent.futures import ThreadPoolExecutor

from .config import (
    DEFAULT_HORIZON_DAYS,
    DEFAULT_RETENTION_DAYS,
    LOG_DIR,
    MAX_WORKERS,
    OWNER,
    RUN_LOG_RETENTION_DAYS,
    RUNNER_LOG,
    WAKE_INTENTS_FILE,
    ensure_dirs,
)
from .notifier import Notifier
from .recurrence import interval_is_due, interval_window_occurrences, next_occurrence, next_window_occurrence, occurrences_between, parse_interval, parse_recurrence, upcoming_occurrences
from .state import StateStore
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
        incidents = self.state.list_incidents(limit=10, include_acked=False)
        heartbeat = self.state.get_meta("runner.heartbeat")
        active_runs = self.state.count_active_runs()
        daemon_count = sum(1 for t in enabled if t.get("timer_type") == "daemon")
        interval_count = sum(1 for t in enabled if t.get("recurrence", {}).get("frequency") == "interval" and t.get("timer_type") != "daemon")

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
            "notifications_muted": self.notifier.muted,
            "unacked_incidents": len(incidents),
            "runner_heartbeat": heartbeat,
            "now": datetime.now(timezone.utc).isoformat(),
        }

    def get_notifications_muted(self) -> bool:
        return self.notifier.muted

    def set_notifications_muted(self, muted: bool) -> Dict[str, Any]:
        self.notifier.muted = muted
        self.state.set_meta("notifications.muted", "true" if muted else "false")
        return {"notifications_muted": muted}

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

    def _estimate_slots(self, timer: Dict[str, Any]) -> int:
        """Estimate how many executor slots this timer would consume."""
        if not timer.get("enabled", True):
            return 0
        execution = timer.get("execution", {})
        overlap = execution.get("overlap", "queue")
        if overlap == "allow":
            return execution.get("max_concurrent", 1)
        return 1  # daemon, interval, or calendar timer = 1 slot

    def check_capacity(self, new_timer: Dict[str, Any], exclude_timer_id: Optional[str] = None) -> tuple:
        """Check if adding/updating a timer would exceed max_workers.

        Returns (can_proceed: bool, warnings: list[str], error_msg: str | None).
        """
        warnings: List[str] = []
        error_msg: Optional[str] = None

        # Resource conflict warnings
        warnings.extend(self.timer_store.check_resource_conflicts(new_timer, exclude_timer_id))

        # Capacity check
        total_slots = 0
        slot_details: List[str] = []
        for t in self.timer_store.list_timers():
            if exclude_timer_id and t["id"] == exclude_timer_id:
                continue
            slots = self._estimate_slots(t)
            if slots > 0:
                total_slots += slots
                slot_details.append(f'{t.get("name", t["id"])} ({t.get("timer_type", "scheduled")}, {slots} slot{"s" if slots > 1 else ""})')

        new_slots = self._estimate_slots(new_timer)
        projected = total_slots + new_slots

        if projected > self.max_workers:
            error_msg = (
                f"Adding this timer would exceed max concurrent capacity ({self.max_workers}). "
                f"Current utilization: {total_slots}/{self.max_workers} active slots. "
                f"Active timers: {', '.join(slot_details[:5])}"
            )
            if len(slot_details) > 5:
                error_msg += f" ... and {len(slot_details) - 5} more"

        return (error_msg is None, warnings, error_msg)

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
    ) -> None:
        """Fire callback if configured. Only on success/failed, never waiting/aborted."""
        callback = timer.get("callback")
        if not callback:
            return
        if status not in ("success", "failed"):
            return

        try:
            timer_name = timer.get("name", timer["id"])
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

            if callback.get("type") == "wezterm":
                # Write signal file for UserPromptSubmit hook to pick up
                session_id = callback.get("session_id")
                if session_id:
                    self._write_callback_signal(session_id, timer_name, status, exit_code, duration_str, stdout_tail)
                self._wezterm_callback(callback, message, timer, timer_name, status)
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
        session_id: str,
        timer_name: str,
        status: str,
        exit_code: Optional[int],
        duration_str: str,
        stdout_tail: str,
    ) -> None:
        """Write callback data to signal file for Claude Code hook to pick up."""
        import datetime as _dt

        signal_dir = Path.home() / ".claude" / "session-signals"
        signal_dir.mkdir(parents=True, exist_ok=True)
        signal_file = signal_dir / f"{session_id}.wakelite-callback.json"

        signal_data = {
            "timer_name": timer_name,
            "status": status,
            "exit_code": exit_code,
            "duration": duration_str,
            "stdout_tail": stdout_tail,
            "timestamp": _dt.datetime.now().isoformat(),
        }
        signal_file.write_text(json.dumps(signal_data, indent=2))
        logger.info("Wrote callback signal file: %s", signal_file)

    def _wezterm_callback(self, callback: Dict[str, Any], message: str, timer: Dict[str, Any],
                          timer_name: str = "", status: str = "") -> None:
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
        self._wezterm_fallback(callback, message, timer)

    def _wezterm_fallback(self, callback: Dict[str, Any], message: str, timer: Dict[str, Any]) -> None:
        """Fallback: Slack DM + optionally spawn new WezTerm tab with claude --resume."""
        timer_name = timer.get("name", timer.get("id", "unknown"))
        self.notifier.notify_slack(
            f":warning: *Callback fallback* — pane gone\n"
            f"Timer: *{timer_name}*\n"
            f"Results delivered to new tab (or Slack only if no session_id).\n\n"
            f"```\n{message[:1500]}\n```"
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
        notifications = timer.get("notifications", {})
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
        self._execute_callback(timer, status, exit_code, run_id, str(stdout_path), duration_seconds)

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
                self.notifier.notify_slack(
                    f":wastebasket: Timer auto-deleted: *{timer_name}*\n"
                    f"Condition: `{trigger}=delete` triggered\n"
                    f"Exit code: {exit_code}"
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
        return False  # no run_id or no PID — ghost

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
                        self.state.finish_run(runtime.running_run_id, "failed", -1, "ghost run: process not alive")
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
        logger.info("Pruned old data: db=%s run_logs=%s", summary, log_summary)

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
