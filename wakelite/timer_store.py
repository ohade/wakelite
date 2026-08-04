from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .config import TIMER_FILE
from .recurrence import parse_recurrence, parse_time, RecurrenceError
from .utils import atomic_write_json, read_json


logger = logging.getLogger(__name__)


ALLOWED_PAYLOAD_KEYS = {"name", "enabled", "timezone", "recurrence", "command", "wake", "notifications", "comment", "id", "timer_type", "execution", "resources", "max_runs", "until", "callback"}
ALLOWED_WAKE_KEYS = {"enabled", "action", "leadMinutes"}
ALLOWED_NOTIFICATION_KEYS = {"onSuccess", "onFailure", "slackActivity"}
ALLOWED_EXECUTION_KEYS = {"overlap", "max_concurrent", "restart_on_failure", "restart_delay_seconds", "restart_max_backoff_seconds"}
ALLOWED_OVERLAP_VALUES = {"skip", "queue", "allow"}
ALLOWED_RESOURCE_KEYS = {"name", "description", "capacity", "estimated_usage"}
ALLOWED_CALLBACK_TYPES = ("wezterm", "ghostty", "cmux")
ALLOWED_CALLBACK_KEYS = {
    "wezterm": {"type", "pane_id", "session_id"},
    "ghostty": {"type", "terminal_id", "session_id"},
    "cmux": {
        "type", "workspace_id", "surface_id", "panel_id", "socket_path",
        "cli_path", "session_id", "amq",
    },
}


class TimerStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> Dict:
        raw = read_json(TIMER_FILE, default={"version": "v1", "timers": []})
        if "timers" not in raw:
            raw = {"version": "v1", "timers": []}
        return raw

    def _save(self) -> None:
        atomic_write_json(TIMER_FILE, self._data)

    def list_timers(self) -> List[Dict]:
        with self._lock:
            return list(self._data["timers"])

    def get_timer(self, timer_id: str) -> Optional[Dict]:
        with self._lock:
            for timer in self._data["timers"]:
                if timer["id"] == timer_id:
                    return dict(timer)
        return None

    def _validate_timer(self, timer: Dict) -> None:
        if not timer.get("name"):
            raise ValueError("name is required")
        if not timer.get("comment"):
            raise ValueError(
                "comment is required — describe what this timer does "
                "(e.g. \"Nightly log cleanup script\")"
            )
        recurrence = timer.get("recurrence", {})
        if not recurrence or not recurrence.get("frequency"):
            raise ValueError(
                "recurrence.frequency is required (daily, weekly, monthly, once). "
                "Hint: the top-level key must be \"recurrence\", not \"schedule\"."
            )
        wake = timer.get("wake", {})
        if wake:
            unknown_wake = set(wake) - ALLOWED_WAKE_KEYS
            if unknown_wake:
                raise ValueError(
                    f"Unknown keys in wake: {unknown_wake}. "
                    f"Allowed: {', '.join(sorted(ALLOWED_WAKE_KEYS))}. "
                    "Hint: use \"leadMinutes\" (not \"minutes_before\")."
                )
        if timer.get("command", {}).get("mode") not in ("shell", "exec"):
            raise ValueError("command.mode must be shell or exec")
        if timer["command"]["mode"] == "shell" and not timer["command"].get("shell"):
            raise ValueError("command.shell is required in shell mode")
        if timer["command"]["mode"] == "exec" and not timer["command"].get("executable"):
            raise ValueError("command.executable is required in exec mode")
        notifications = timer.get("notifications", {})
        if not isinstance(notifications, dict):
            raise ValueError("notifications must be an object")
        unknown_notifications = set(notifications) - ALLOWED_NOTIFICATION_KEYS
        if unknown_notifications:
            raise ValueError(
                f"Unknown keys in notifications: {unknown_notifications}. "
                f"Allowed: {', '.join(sorted(ALLOWED_NOTIFICATION_KEYS))}"
            )
        for field in ALLOWED_NOTIFICATION_KEYS:
            if field in notifications and not isinstance(notifications[field], bool):
                raise ValueError(f"notifications.{field} must be boolean")

        timer_type = timer.get("timer_type", "scheduled")
        if timer_type not in ("scheduled", "daemon"):
            raise ValueError("timer_type must be 'scheduled' or 'daemon'")

        execution = timer.get("execution", {})
        if execution:
            unknown_exec = set(execution) - ALLOWED_EXECUTION_KEYS
            if unknown_exec:
                raise ValueError(f"Unknown keys in execution: {unknown_exec}. Allowed: {', '.join(sorted(ALLOWED_EXECUTION_KEYS))}")
            overlap = execution.get("overlap", "skip")
            if overlap not in ALLOWED_OVERLAP_VALUES:
                raise ValueError(f"execution.overlap must be one of: {', '.join(sorted(ALLOWED_OVERLAP_VALUES))}")
            max_conc = execution.get("max_concurrent", 1)
            if not isinstance(max_conc, int) or max_conc < 1:
                raise ValueError("execution.max_concurrent must be a positive integer")

        resources = timer.get("resources", [])
        if resources:
            if not isinstance(resources, list):
                raise ValueError("resources must be a list")
            for i, res in enumerate(resources):
                if not isinstance(res, dict):
                    raise ValueError(f"resources[{i}] must be an object")
                if not res.get("name"):
                    raise ValueError(f"resources[{i}].name is required")
                unknown_res = set(res) - ALLOWED_RESOURCE_KEYS
                if unknown_res:
                    raise ValueError(f"Unknown keys in resources[{i}]: {unknown_res}. Allowed: {', '.join(sorted(ALLOWED_RESOURCE_KEYS))}")

        max_runs = timer.get("max_runs")
        if max_runs is not None:
            if not isinstance(max_runs, int) or max_runs < 1:
                raise ValueError("max_runs must be a positive integer or null")

        until = timer.get("until")
        if until is not None:
            if not isinstance(until, dict):
                raise ValueError(
                    "'until' must be an object with 'on_success' and 'on_failure' fields, "
                    "each set to 'delete' or 'continue'"
                )
            allowed_actions = ("delete", "continue")
            for field in ("on_success", "on_failure"):
                val = until.get(field)
                if val not in allowed_actions:
                    raise ValueError(
                        f"'until.{field}' is required and must be 'delete' or 'continue'"
                    )

        callback = timer.get("callback")
        if callback is not None:
            if not isinstance(callback, dict):
                raise ValueError("callback must be an object")
            cb_type = callback.get("type")
            if cb_type not in ALLOWED_CALLBACK_TYPES:
                # CC-95: rolling-deploy compat. A timer persisted by a newer
                # WakeLite version may carry a callback type the running version
                # doesn't recognize (e.g., "kitty" lands in vN+1, vN gets rolled
                # back). Hard-rejecting here breaks update_timer / replace_all
                # on existing timers and bricks the rollback. Neutralize the
                # callback instead — the timer keeps running, just without
                # terminal injection — and warn loudly so the operator sees it.
                logger.warning(
                    "Unknown callback.type=%r on timer %r — neutralizing callback "
                    "for rolling-deploy compatibility. Allowed types: %s. "
                    "The timer will run without terminal callback.",
                    cb_type,
                    timer.get("name", "<unnamed>"),
                    ", ".join(ALLOWED_CALLBACK_TYPES),
                )
                timer["callback"] = None
            elif "amq" in callback and cb_type != "cmux":
                raise ValueError("callback.amq is only valid for cmux callbacks")
            elif unknown_callback := set(callback) - ALLOWED_CALLBACK_KEYS[cb_type]:
                raise ValueError(
                    f"Unknown keys in callback: {unknown_callback}. "
                    f"Allowed for {cb_type}: "
                    f"{', '.join(sorted(ALLOWED_CALLBACK_KEYS[cb_type]))}"
                )
            elif cb_type == "cmux":
                if "amq" in callback and not isinstance(callback["amq"], bool):
                    raise ValueError("callback.amq must be a boolean")
                workspace_id = callback.get("workspace_id")
                surface_id = callback.get("surface_id")
                panel_id = callback.get("panel_id")
                if not isinstance(workspace_id, str) or not workspace_id:
                    raise ValueError("callback.workspace_id must be a non-empty string")
                if surface_id is not None and not isinstance(surface_id, str):
                    raise ValueError("callback.surface_id must be a string")
                if panel_id is not None and not isinstance(panel_id, str):
                    raise ValueError("callback.panel_id must be a string")
                if not surface_id and panel_id:
                    callback["surface_id"] = panel_id
                callback.pop("panel_id", None)
                if not isinstance(callback.get("surface_id"), str) or not callback.get("surface_id"):
                    raise ValueError("callback.surface_id or callback.panel_id must be a non-empty string")
                for field in ("socket_path", "cli_path", "session_id"):
                    value = callback.get(field)
                    if value is not None and not isinstance(value, str):
                        raise ValueError(f"callback.{field} must be a string")
            else:
                pane_id = callback.get("pane_id")
                if pane_id is not None and not isinstance(pane_id, int):
                    raise ValueError("callback.pane_id must be an integer")
                terminal_id = callback.get("terminal_id")
                if terminal_id is not None and not isinstance(terminal_id, str):
                    raise ValueError("callback.terminal_id must be a string")
                session_id = callback.get("session_id")
                if session_id is not None and not isinstance(session_id, str):
                    raise ValueError("callback.session_id must be a string")

        active_hours = timer.get("recurrence", {}).get("active_hours")
        if active_hours:
            freq = timer.get("recurrence", {}).get("frequency")
            if freq != "interval":
                raise ValueError("active_hours is only valid for interval frequency")
            if "start" not in active_hours or "end" not in active_hours:
                raise ValueError("active_hours requires both 'start' and 'end' (HH:MM)")
            parse_time(active_hours["start"])
            parse_time(active_hours["end"])

        recurrence = parse_recurrence(timer)
        if recurrence.frequency == "once" and recurrence.once_date is not None and timer.get("enabled", True):
            from datetime import date
            today = date.today()
            if recurrence.once_date < today:
                raise ValueError(
                    f"Cannot create/enable a one-time timer for a past date "
                    f"({recurrence.once_date.isoformat()}). Today is {today.isoformat()}."
                )

    def create_timer(self, payload: Dict) -> Dict:
        unknown = set(payload) - ALLOWED_PAYLOAD_KEYS
        if unknown:
            raise ValueError(
                f"Unknown keys in timer payload: {unknown}. "
                f"Allowed top-level keys: {', '.join(sorted(ALLOWED_PAYLOAD_KEYS))}"
            )
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            timer = {
                "id": payload.get("id") or str(uuid.uuid4()),
                "name": payload.get("name", "timer"),
                "comment": payload.get("comment", ""),
                "enabled": bool(payload.get("enabled", True)),
                "timer_type": payload.get("timer_type", "scheduled"),
                "timezone": payload.get("timezone", "local"),
                "recurrence": payload.get("recurrence", {}),
                "command": payload.get("command", {}),
                "execution": payload.get("execution", {}),
                "resources": payload.get("resources", []),
                "max_runs": payload.get("max_runs"),
                "until": payload.get("until"),
                "callback": payload.get("callback"),
                "wake": payload.get(
                    "wake",
                    {"enabled": False, "action": "wake", "leadMinutes": 0},
                ),
                "notifications": payload.get(
                    "notifications",
                    {"onSuccess": False, "onFailure": True, "slackActivity": True},
                ),
                "created_at": payload.get("created_at", now),
                "updated_at": now,
            }
            self._validate_timer(timer)
            self._data["timers"].append(timer)
            self._save()
            return dict(timer)

    def update_timer(self, timer_id: str, patch: Dict) -> Dict:
        with self._lock:
            for idx, timer in enumerate(self._data["timers"]):
                if timer["id"] != timer_id:
                    continue
                updated = dict(timer)
                for key in ("name", "comment", "enabled", "timezone", "recurrence", "command", "wake", "notifications", "timer_type", "execution", "resources", "max_runs", "until", "callback"):
                    if key in patch:
                        updated[key] = patch[key]
                updated["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._validate_timer(updated)
                self._data["timers"][idx] = updated
                self._save()
                return dict(updated)
        raise KeyError(timer_id)

    def delete_timer(self, timer_id: str) -> bool:
        with self._lock:
            before = len(self._data["timers"])
            self._data["timers"] = [t for t in self._data["timers"] if t["id"] != timer_id]
            changed = len(self._data["timers"]) != before
            if changed:
                self._save()
            return changed

    def set_enabled(self, timer_id: str, enabled: bool) -> Dict:
        return self.update_timer(timer_id, {"enabled": enabled})

    def check_resource_conflicts(self, timer_payload: Dict, exclude_timer_id: Optional[str] = None) -> List[str]:
        """Check if a timer's declared resources conflict with other timers.

        Returns a list of warning strings (empty if no conflicts).
        """
        resources = timer_payload.get("resources", [])
        if not resources:
            return []

        new_resource_names = {r["name"] for r in resources if r.get("name")}
        if not new_resource_names:
            return []

        warnings: List[str] = []
        with self._lock:
            for other_timer in self._data["timers"]:
                if exclude_timer_id and other_timer["id"] == exclude_timer_id:
                    continue
                if not other_timer.get("enabled", True):
                    continue
                other_resources = other_timer.get("resources", [])
                for other_res in other_resources:
                    other_name = other_res.get("name", "")
                    if other_name in new_resource_names:
                        warnings.append(
                            f'Resource "{other_name}" is also used by '
                            f'timer "{other_timer.get("name", other_timer["id"])}" '
                            f'(estimated: {other_res.get("estimated_usage", "unknown")}, '
                            f'capacity: {other_res.get("capacity", "unknown")})'
                        )
        return warnings

    def replace_all(self, timers: List[Dict]) -> Tuple[int, int]:
        with self._lock:
            old_count = len(self._data["timers"])
            self._data["timers"] = timers
            for timer in self._data["timers"]:
                self._validate_timer(timer)
            self._save()
            return old_count, len(self._data["timers"])
