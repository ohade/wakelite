"""`wakelitectl doctor` — one command that says what is wrong.

Every other CLI subcommand reaches the runner over REST. That is exactly the
wrong dependency for the failure this command exists to diagnose: when the
scheduler loop hangs, the REST server hangs with it and the CLI reports
nothing but "service unavailable". So doctor tries REST first and falls back
to reading state.db directly. It runs as the owning user, so the direct read
needs no privileges.

The report is read-only. `--fix` performs exactly two actions:

  1. the orphan reclaim (owned by the R1 change; see `_resolve_orphan_reclaim`)
  2. `launchctl kickstart -k` when the heartbeat is stale, rate-limited and
     escalation-bounded so a runner that refuses to come back raises one
     critical incident instead of being kicked forever

Both actions record an incident naming what they did.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import config
from .state import StateStore
from .utils import read_json

RUNNER_LABEL = "com.wakelite.runner"

# The scheduler writes runner.heartbeat every wake and caps its idle sleep at
# 15s (WakeLiteService._MAX_SLEEP), so 120s is eight missed writes — well past
# anything a slow tick explains.
STALE_HEARTBEAT_SECONDS = 120

KICK_COOLDOWN_SECONDS = 30 * 60
KICK_FAILURE_WINDOW_SECONDS = 2 * 60 * 60
KICK_FAILURE_LIMIT = 2

# A timer is "failing" once it has missed this many runs in a row. Exit code 75
# means "not ready yet" and is neutral, per the polling-script convention.
FAILURE_STREAK_THRESHOLD = 3
FAILURE_STREAK_LOOKBACK = 50

META_HEARTBEAT = "runner.heartbeat"
META_RUNNER_PID = "runner.pid"
META_WATCHDOG_LAST_RUN = "watchdog.last_run"
META_KICK_ATTEMPTS = "doctor.kick.attempts"
META_KICK_ESCALATED_AT = "doctor.kick.escalated_at"

# R1 stamps this into every child's environment. Two processes carrying the
# same value is the definition of a duplicate daemon.
TIMER_ID_ENV_MARKER = "WAKELITE_TIMER_ID"

_NEUTRAL_RUN_STATUSES = {"waiting", "aborted", "shutdown", "started", "queued"}
_RUNNER_ARG_MARKERS = (
    "wakelite-runner",
    "wakelite.runner_main",
    "wakelite.cli serve",
    "wakelitectl serve",
)

_PORT_PATTERNS = (
    re.compile(r"--port[=\s]+(\d{1,5})"),
    re.compile(r"\bPORT=(\d{1,5})"),
    re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})"),
)
_RESOURCE_PORT_PATTERN = re.compile(r"^(?:tcp|udp|port)[:/-](\d{1,5})$", re.IGNORECASE)
# Bare number in a port resource's free text. The lookarounds reject octets of
# a dotted quad, so "127.0.0.1:17382" yields 17382 and not 127.
_LOOSE_NUMBER_PATTERN = re.compile(r"(?<![\d.])(\d{2,5})(?![\d.])")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _api_get(path: str) -> Optional[Dict[str, Any]]:
    """GET from the local REST API, or None when it does not answer.

    The timeout is deliberately short: a hung runner accepts the connection
    and then never replies, and doctor must not hang with it.
    """
    from urllib import request

    url = f"http://{config.API_HOST}:{config.API_PORT}{path}"
    req = request.Request(url=url, method="GET", headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=2.0) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def default_process_snapshot() -> List[Dict[str, Any]]:
    """One `ps` pass: pid, ppid, and args with the environment appended.

    `-E` is what exposes WAKELITE_TIMER_ID. If it is unavailable the plain
    form still gives liveness and parentage; duplicate detection is the only
    thing that degrades, and the report says so rather than guessing.
    """
    for argv in (
        ["ps", "-A", "-E", "-o", "pid=,ppid=,args="],
        ["ps", "-A", "-o", "pid=,ppid=,args="],
    ):
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        rows: List[Dict[str, Any]] = []
        for line in completed.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 2:
                continue
            try:
                pid, ppid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            rows.append({"pid": pid, "ppid": ppid, "args": parts[2] if len(parts) > 2 else ""})
        if rows:
            return rows
    return []


def default_port_holders(port: int) -> List[int]:
    argv = ["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    holders: List[int] = []
    for line in completed.stdout.split():
        try:
            holders.append(int(line))
        except ValueError:
            continue
    return sorted(set(holders))


def default_launchctl(argv: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=30)


def _resolve_orphan_reclaim() -> Optional[Callable[[Any], List[Dict[str, Any]]]]:
    """Find the R1 reclaim entrypoint, or None while R1 has not landed.

    Contract: `wakelite.service.reclaim_orphans(state)` returns a list of
    dicts describing what it reclaimed. Looked up by name at call time so
    doctor works standalone and picks up R1 the moment it merges.
    """
    from . import service

    hook = getattr(service, "reclaim_orphans", None)
    return hook if callable(hook) else None


def _last_stderr_line(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        target = Path(path)
        size = target.stat().st_size
        with target.open("rb") as handle:
            if size > 8192:
                handle.seek(size - 8192)
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if line.strip():
            return line.strip()
    return None


def declared_ports(timer: Dict[str, Any]) -> List[int]:
    """Ports this timer claims.

    Three accepted forms, in the order they are looked for: a resource named
    `port:NNNN` / `tcp-NNNN`, any number in a resource whose name mentions a
    port (so `{"name": "cmux-focus-port", "description": "TCP 17382"}` works
    without a schema change), and `--port NNNN` / `PORT=NNNN` /
    `127.0.0.1:NNNN` in the command.
    """
    found: set[int] = set()
    for resource in timer.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        name = str(resource.get("name", ""))
        match = _RESOURCE_PORT_PATTERN.match(name)
        if match:
            found.add(int(match.group(1)))
        elif "port" in name.lower():
            haystack = f"{name} {resource.get('description') or ''}"
            for pattern in _PORT_PATTERNS:
                found.update(int(raw) for raw in pattern.findall(haystack))
            found.update(int(raw) for raw in _LOOSE_NUMBER_PATTERN.findall(haystack))

    command = timer.get("command") or {}
    haystack = " ".join(
        [str(command.get("shell") or ""), str(command.get("executable") or "")]
        + [str(arg) for arg in (command.get("args") or [])]
    )
    for pattern in _PORT_PATTERNS:
        for raw in pattern.findall(haystack):
            found.add(int(raw))

    return sorted(port for port in found if 1 <= port <= 65535)


def unparsed_port_resources(timer: Dict[str, Any]) -> List[str]:
    """Resources that mention a port but carry no number doctor can read.

    `cmux-focus-server` — the daemon from the 2026-09-07 incident — declares
    exactly this: a resource called `cmux-focus-port` with 17382 recorded
    nowhere. Naming it beats silently reporting no port at all.
    """
    if declared_ports(timer):
        return []
    return [
        str(resource.get("name"))
        for resource in timer.get("resources") or []
        if isinstance(resource, dict) and "port" in str(resource.get("name", "")).lower()
    ]


class Doctor:
    def __init__(
        self,
        *,
        db_path: Optional[Path] = None,
        timer_file: Optional[Path] = None,
        api_get: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        process_snapshot: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        port_holders: Optional[Callable[[int], List[int]]] = None,
        launchctl: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
        orphan_reclaim: Optional[Callable[[Any], List[Dict[str, Any]]]] = None,
        now: Optional[Callable[[], datetime]] = None,
        uid: Optional[int] = None,
        state: Optional[StateStore] = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path else config.STATE_DB_FILE
        self._timer_file = Path(timer_file) if timer_file else config.TIMER_FILE
        self._api_get = api_get or _api_get
        self._process_snapshot = process_snapshot or default_process_snapshot
        self._port_holders = port_holders or default_port_holders
        self._launchctl = launchctl or default_launchctl
        self._orphan_reclaim = orphan_reclaim
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._uid = uid if uid is not None else os.getuid()
        self._state = state

    @property
    def state(self) -> StateStore:
        if self._state is None:
            self._state = StateStore(self._db_path)
        return self._state

    # ----- report ------------------------------------------------------

    def _api(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            payload = self._api_get(path)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _load_timers(self, health: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if health is not None:
            payload = self._api("/v1/timers")
            if payload and isinstance(payload.get("timers"), list):
                return payload["timers"]
        raw = read_json(self._timer_file, default={"timers": []})
        timers = raw.get("timers") if isinstance(raw, dict) else None
        return timers if isinstance(timers, list) else []

    def _runner_section(
        self, heartbeat_iso: Optional[str], api_alive: bool, now: datetime
    ) -> Dict[str, Any]:
        heartbeat = _parse_iso(heartbeat_iso)
        age = (now - heartbeat).total_seconds() if heartbeat else None

        if heartbeat is None:
            status = "unreachable"
            detail = "no heartbeat has ever been written to state.db"
        elif age is not None and age > STALE_HEARTBEAT_SECONDS:
            status = "stale"
            detail = (
                f"heartbeat is {int(age)}s old; the scheduler loop writes one at "
                f"least every 15s, so it is not turning"
            )
        elif api_alive:
            status = "healthy"
            detail = f"heartbeat {int(age or 0)}s old, REST API answering"
        else:
            status = "unreachable"
            detail = (
                f"heartbeat is fresh ({int(age or 0)}s) but the REST API on port "
                f"{config.API_PORT} did not answer"
            )

        return {
            "status": status,
            "heartbeat": heartbeat_iso,
            "heartbeat_age_seconds": age,
            "detail": detail,
        }

    def _runner_pid(self, snapshot: List[Dict[str, Any]]) -> Optional[int]:
        recorded = self.state.get_meta(META_RUNNER_PID)
        live = {row["pid"] for row in snapshot}
        if recorded:
            try:
                pid = int(recorded)
            except ValueError:
                pid = None
            if pid is not None and (pid in live or not snapshot):
                return pid
        for row in snapshot:
            args = row.get("args", "")
            if any(marker in args for marker in _RUNNER_ARG_MARKERS):
                return int(row["pid"])
        return None

    def _daemon_sections(
        self,
        timers: List[Dict[str, Any]],
        snapshot: List[Dict[str, Any]],
        runner_pid: Optional[int],
        now: datetime,
    ) -> List[Dict[str, Any]]:
        by_pid = {row["pid"]: row for row in snapshot}
        runtime_rows = {row["timer_id"]: row for row in self.state.list_runtime()}

        sections: List[Dict[str, Any]] = []
        for timer in timers:
            if timer.get("timer_type") != "daemon":
                continue
            timer_id = timer["id"]
            runtime = runtime_rows.get(timer_id, {})
            expected_running = bool(runtime.get("is_running"))
            daemon_state = self.state.get_daemon_state(timer_id)

            tracked = [
                int(row["pid"])
                for row in self.state.get_active_runs_for_timer(timer_id)
                if row.get("pid") is not None
            ]
            live = [pid for pid in tracked if pid in by_pid]
            pid = live[0] if live else (tracked[0] if tracked else None)
            parent_pid = by_pid[pid]["ppid"] if pid in by_pid else None

            # A single ps snapshot backs liveness, parentage, and duplicate
            # detection, so the three can never contradict each other.
            marked = sorted(
                int(row["pid"])
                for row in snapshot
                if f"{TIMER_ID_ENV_MARKER}={timer_id}" in row.get("args", "")
            )
            duplicate = len(marked) > 1 if marked else None

            sections.append({
                "timer_id": timer_id,
                "name": timer.get("name", timer_id),
                "enabled": bool(timer.get("enabled", True)),
                "expected_running": expected_running,
                "daemon_status": daemon_state.status,
                "alive": bool(live),
                "pid": pid,
                "parent_pid": parent_pid,
                "parent_is_runner": (
                    None if parent_pid is None or runner_pid is None else parent_pid == runner_pid
                ),
                "marked_pids": marked,
                "duplicate": duplicate,
                "restart_count": daemon_state.restart_count,
            })
        return sections

    def _port_sections(
        self,
        timers: List[Dict[str, Any]],
        daemons: List[Dict[str, Any]],
        snapshot: List[Dict[str, Any]],
        runner_pid: Optional[int],
    ) -> tuple:
        by_pid = {row["pid"]: row for row in snapshot}
        daemon_by_id = {d["timer_id"]: d for d in daemons}

        sections: List[Dict[str, Any]] = []
        hints: List[Dict[str, Any]] = []
        for timer in timers:
            if timer.get("timer_type") != "daemon":
                continue
            timer_id = timer["id"]
            for port in declared_ports(timer):
                holders = list(self._port_holders(port))
                ours = self._holders_are_ours(holders, timer_id, by_pid, daemon_by_id, runner_pid)
                sections.append({
                    "timer_id": timer_id,
                    "name": timer.get("name", timer_id),
                    "port": port,
                    "holders": holders,
                    "ours": ours,
                })
            for resource in unparsed_port_resources(timer):
                hints.append({
                    "timer_id": timer_id,
                    "name": timer.get("name", timer_id),
                    "resource": resource,
                })
        return sections, hints

    def _holders_are_ours(
        self,
        holders: List[int],
        timer_id: str,
        by_pid: Dict[int, Dict[str, Any]],
        daemon_by_id: Dict[str, Dict[str, Any]],
        runner_pid: Optional[int],
    ) -> Optional[bool]:
        if not holders:
            return None
        tracked_pid = (daemon_by_id.get(timer_id) or {}).get("pid")
        for holder in holders:
            if holder == tracked_pid:
                continue
            row = by_pid.get(holder)
            if row and f"{TIMER_ID_ENV_MARKER}={timer_id}" in row.get("args", ""):
                continue
            if row and runner_pid is not None and row["ppid"] == runner_pid:
                continue
            return False
        return True

    def _streak_column(self) -> Optional[str]:
        """R2 adds a failure-streak column; use it when it exists."""
        try:
            conn = self.state._connect()
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(timer_runtime)")}
        except Exception:
            return None
        for candidate in ("failure_streak", "consecutive_failures", "fail_streak"):
            if candidate in columns:
                return candidate
        return None

    def _derived_streak(self, timer_id: str) -> tuple:
        streak = 0
        newest_failure: Optional[Dict[str, Any]] = None
        for run in self.state.list_runs(limit=FAILURE_STREAK_LOOKBACK, timer_id=timer_id):
            status = run.get("status")
            if status == "success":
                break
            if status in _NEUTRAL_RUN_STATUSES:
                continue
            streak += 1
            if newest_failure is None:
                newest_failure = run
        return streak, newest_failure

    def _failing_timers(self, timers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        column = self._streak_column()
        failing: List[Dict[str, Any]] = []
        for timer in timers:
            timer_id = timer["id"]
            derived, newest_failure = self._derived_streak(timer_id)
            streak = derived
            if column:
                try:
                    row = self.state._connect().execute(
                        f"SELECT {column} AS streak FROM timer_runtime WHERE timer_id = ?",
                        (timer_id,),
                    ).fetchone()
                except Exception:
                    row = None
                if row and row["streak"] is not None:
                    streak = int(row["streak"])
            if streak < FAILURE_STREAK_THRESHOLD:
                continue
            failing.append({
                "timer_id": timer_id,
                "name": timer.get("name", timer_id),
                "streak": streak,
                "streak_source": column or "run_history",
                "last_error": (newest_failure or {}).get("message"),
                "last_stderr_line": _last_stderr_line((newest_failure or {}).get("stderr_path")),
            })
        return failing

    def _incident_section(self) -> Dict[str, Any]:
        summary = self.state.incident_summary(days=30)
        by_type = {
            str(row["key"]): int(row["unacked"])
            for row in summary.get("by_type", [])
            if int(row["unacked"]) > 0
        }
        return {
            "unacked_total": self.state.count_unacked_incidents(),
            "by_type": by_type,
        }

    def _watchdog_section(self, now: datetime) -> Dict[str, Any]:
        last_run_iso = self.state.get_meta(META_WATCHDOG_LAST_RUN)
        last_run = _parse_iso(last_run_iso)
        return {
            "last_run": last_run_iso,
            "age_seconds": (now - last_run).total_seconds() if last_run else None,
        }

    def report(self) -> Dict[str, Any]:
        now = self._now()
        health = self._api("/v1/health")
        heartbeat_iso = (health or {}).get("runner_heartbeat") or self.state.get_meta(META_HEARTBEAT)

        runner = self._runner_section(heartbeat_iso, health is not None, now)
        timers = self._load_timers(health)
        snapshot = self._process_snapshot()
        runner_pid = self._runner_pid(snapshot)
        daemons = self._daemon_sections(timers, snapshot, runner_pid, now)
        ports, port_hints = self._port_sections(timers, daemons, snapshot, runner_pid)

        report = {
            "generated_at": now.isoformat(),
            "source": "api" if health is not None else "database",
            "db_path": str(self._db_path),
            "runner": runner,
            "runner_pid": runner_pid,
            "daemons": daemons,
            "ports": ports,
            "port_hints": port_hints,
            "failing_timers": self._failing_timers(timers),
            "incidents": self._incident_section(),
            "watchdog": self._watchdog_section(now),
            "fixes": [],
        }
        report["problems"] = self._problems(report)
        return report

    @staticmethod
    def _problems(report: Dict[str, Any]) -> List[str]:
        """Conditions worth waking someone for.

        Open incidents and watchdog age are reported but are not problems:
        there are 142 open incidents on this box today, and counting them as
        faults would mean `--quiet` never stays quiet again.
        """
        problems: List[str] = []
        runner = report["runner"]
        if runner["status"] != "healthy":
            problems.append(f"runner {runner['status']}: {runner['detail']}")

        for daemon in report["daemons"]:
            label = daemon["name"]
            if daemon["enabled"] and daemon["expected_running"] and not daemon["alive"]:
                problems.append(
                    f"daemon {label} is marked running but no tracked process is alive"
                )
            if daemon["duplicate"]:
                problems.append(
                    f"daemon {label} has {len(daemon['marked_pids'])} live processes "
                    f"(pids {', '.join(str(p) for p in daemon['marked_pids'])})"
                )
            if daemon["parent_is_runner"] is False:
                problems.append(
                    f"daemon {label} (pid {daemon['pid']}) is not a child of the current "
                    f"runner — parent is pid {daemon['parent_pid']}"
                )

        for entry in report["ports"]:
            if entry["ours"] is False:
                holders = ", ".join(str(p) for p in entry["holders"])
                problems.append(
                    f"port {entry['port']} declared by {entry['name']} is held by "
                    f"pid {holders}, which is not ours"
                )

        for timer in report["failing_timers"]:
            tail = f" — last stderr: {timer['last_stderr_line']}" if timer["last_stderr_line"] else ""
            problems.append(f"timer {timer['name']} has failed {timer['streak']} times in a row{tail}")

        return problems

    # ----- fix ---------------------------------------------------------

    def fix(self, report: Dict[str, Any]) -> List[Dict[str, Any]]:
        actions = [self._fix_orphans(), self._fix_stale_runner(report)]
        performed = [action for action in actions if action]
        report["fixes"] = performed
        return performed

    def _fix_orphans(self) -> Dict[str, Any]:
        hook = self._orphan_reclaim or _resolve_orphan_reclaim()
        if hook is None:
            return {
                "action": "orphan_reclaim",
                "status": "unavailable",
                "detail": "wakelite.service.reclaim_orphans is not present in this build",
            }
        try:
            reclaimed = list(hook(self.state) or [])
        except Exception as exc:
            return {"action": "orphan_reclaim", "status": "error", "detail": str(exc)}

        if reclaimed:
            self.state.add_incident(
                "warn",
                "doctor_orphan_reclaim",
                f"doctor --fix reclaimed {len(reclaimed)} orphaned process(es): "
                + ", ".join(str(item) for item in reclaimed),
            )
        return {
            "action": "orphan_reclaim",
            "status": "reclaimed" if reclaimed else "nothing_to_reclaim",
            "count": len(reclaimed),
            "detail": f"{len(reclaimed)} orphan(s) reclaimed",
        }

    def _kick_attempts(self, now: datetime) -> List[datetime]:
        raw = self.state.get_meta(META_KICK_ATTEMPTS)
        if not raw:
            return []
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            return []
        cutoff = now - timedelta(seconds=KICK_FAILURE_WINDOW_SECONDS)
        parsed = [_parse_iso(value) for value in values if isinstance(value, str)]
        return sorted(when for when in parsed if when and when >= cutoff)

    def _record_kick_attempt(self, attempts: List[datetime], now: datetime) -> None:
        self.state.set_meta(
            META_KICK_ATTEMPTS,
            json.dumps([when.isoformat() for when in attempts + [now]]),
        )

    def _clear_kick_state(self) -> None:
        if self.state.get_meta(META_KICK_ATTEMPTS):
            self.state.set_meta(META_KICK_ATTEMPTS, "[]")
        if self.state.get_meta(META_KICK_ESCALATED_AT):
            self.state.set_meta(META_KICK_ESCALATED_AT, "")

    def _fix_stale_runner(self, report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        now = self._now()
        if report["runner"]["status"] != "stale":
            self._clear_kick_state()
            return None

        attempts = self._kick_attempts(now)

        # Escalation is checked before the cooldown on purpose. A stale
        # heartbeat means every attempt still inside the window failed to
        # revive the runner, and kicking a corpse forever helps nobody.
        if len(attempts) >= KICK_FAILURE_LIMIT:
            escalated_at = _parse_iso(self.state.get_meta(META_KICK_ESCALATED_AT))
            already = escalated_at is not None and (
                (now - escalated_at).total_seconds() < KICK_FAILURE_WINDOW_SECONDS
            )
            if not already:
                self.state.add_incident(
                    "critical",
                    "doctor_runner_unrecoverable",
                    f"doctor --fix stopped kicking {RUNNER_LABEL}: {len(attempts)} kick(s) "
                    f"in the last {KICK_FAILURE_WINDOW_SECONDS // 3600}h did not restore the "
                    f"heartbeat (age {int(report['runner']['heartbeat_age_seconds'] or 0)}s)",
                )
                self.state.set_meta(META_KICK_ESCALATED_AT, now.isoformat())
            return {
                "action": "runner_kick",
                "status": "escalated",
                "attempts": len(attempts),
                "detail": f"{len(attempts)} kicks failed to restore the heartbeat; escalated",
            }

        if attempts and (now - attempts[-1]).total_seconds() < KICK_COOLDOWN_SECONDS:
            waited = int((now - attempts[-1]).total_seconds())
            return {
                "action": "runner_kick",
                "status": "skipped",
                "attempts": len(attempts),
                "detail": (
                    f"last kick was {waited}s ago; the cooldown is "
                    f"{KICK_COOLDOWN_SECONDS}s"
                ),
            }

        argv = ["launchctl", "kickstart", "-k", f"gui/{self._uid}/{RUNNER_LABEL}"]
        self._record_kick_attempt(attempts, now)
        try:
            completed = self._launchctl(argv)
            returncode = completed.returncode
            stderr = (completed.stderr or "").strip()
        except Exception as exc:
            returncode, stderr = -1, str(exc)

        self.state.add_incident(
            "warn" if returncode == 0 else "error",
            "doctor_runner_kick",
            f"doctor --fix ran `{' '.join(argv)}` because the heartbeat was "
            f"{int(report['runner']['heartbeat_age_seconds'] or 0)}s old "
            f"(exit {returncode}{': ' + stderr if stderr else ''})",
        )
        return {
            "action": "runner_kick",
            "status": "kicked" if returncode == 0 else "failed",
            "attempts": len(attempts) + 1,
            "returncode": returncode,
            "detail": f"ran {' '.join(argv)}",
        }

    # ----- output ------------------------------------------------------

    def run(
        self,
        *,
        fix: bool = False,
        quiet: bool = False,
        as_json: bool = False,
        watchdog: bool = False,
        stream: Optional[Any] = None,
    ) -> int:
        out = stream if stream is not None else sys.stdout
        report = self.report()
        if fix:
            self.fix(report)

        if watchdog:
            # Stamped only for a scheduled run, never for a human's --fix.
            # This marker is how a DEAD watchdog becomes visible: its only
            # failure mode is not running at all, and doctor reports the age of
            # this value. A manual --fix refreshing it would mask exactly that.
            self.state.set_meta(META_WATCHDOG_LAST_RUN, self._now().isoformat())

        acted = any(
            action.get("status") in ("kicked", "failed", "escalated", "reclaimed")
            for action in report["fixes"]
        )
        healthy = not report["problems"]
        if quiet and healthy and not acted:
            return 0

        if as_json:
            print(json.dumps(report, indent=2, sort_keys=True), file=out)
        else:
            print(render(report), file=out)
        return 0 if healthy else 1


def _format_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def render(report: Dict[str, Any]) -> str:
    lines = [f"WakeLite doctor — {report['generated_at']} (source: {report['source']})", ""]

    runner = report["runner"]
    lines.append(f"Runner:    {runner['status']} — {runner['detail']}")

    if report["daemons"]:
        lines.append("Daemons:")
        for daemon in report["daemons"]:
            if daemon["alive"]:
                parent = ""
                if daemon["parent_is_runner"] is True:
                    parent = f", parent pid {daemon['parent_pid']} is the runner"
                elif daemon["parent_is_runner"] is False:
                    parent = f", parent pid {daemon['parent_pid']} is NOT the runner"
                state = f"alive pid {daemon['pid']}{parent}"
            elif daemon["expected_running"]:
                state = "marked running but no live process"
            else:
                state = f"not running ({daemon['daemon_status']})"
            if daemon["duplicate"]:
                state += f" — DUPLICATE pids {daemon['marked_pids']}"
            elif daemon["duplicate"] is None:
                state += " — duplicate check unavailable (no WAKELITE_TIMER_ID marker)"
            lines.append(f"  {daemon['name']}: {state}")

    if report["ports"] or report["port_hints"]:
        lines.append("Ports:")
        for entry in report["ports"]:
            if not entry["holders"]:
                who = "nobody is listening"
            else:
                owner = "ours" if entry["ours"] else "NOT ours"
                who = f"held by pid {', '.join(str(p) for p in entry['holders'])} — {owner}"
            lines.append(f"  {entry['port']} ({entry['name']}): {who}")
        for hint in report["port_hints"]:
            lines.append(
                f"  {hint['name']}: resource '{hint['resource']}' names a port but no "
                f"number — add it to the resource description (e.g. \"TCP 17382\")"
            )

    if report["failing_timers"]:
        lines.append("Failing timers:")
        for timer in report["failing_timers"]:
            tail = timer["last_stderr_line"] or timer["last_error"] or "no stderr captured"
            lines.append(f"  {timer['name']}: {timer['streak']} failures in a row — {tail}")

    incidents = report["incidents"]
    breakdown = ", ".join(f"{key} {count}" for key, count in sorted(incidents["by_type"].items()))
    lines.append(
        f"Incidents: {incidents['unacked_total']} unacknowledged"
        + (f" ({breakdown})" if breakdown else "")
    )

    watchdog = report["watchdog"]
    lines.append(f"Watchdog:  {_format_age(watchdog['age_seconds'])} ({watchdog['last_run'] or 'no record'})")

    if report["fixes"]:
        lines.append("")
        lines.append("Fixes:")
        for action in report["fixes"]:
            lines.append(f"  {action['action']}: {action['status']} — {action.get('detail', '')}")

    lines.append("")
    if report["problems"]:
        lines.append("Problems:")
        lines.extend(f"  - {problem}" for problem in report["problems"])
    else:
        lines.append("No problems found — WakeLite is healthy.")

    return "\n".join(lines)


def run_doctor(
    *,
    fix: bool = False,
    quiet: bool = False,
    as_json: bool = False,
    watchdog: bool = False,
) -> int:
    # --watchdog is the scheduled entrypoint: it implies --fix and --quiet, so
    # the LaunchAgent and a human invoke the same code path with one flag.
    if watchdog:
        fix = True
        quiet = True
    return Doctor().run(fix=fix, quiet=quiet, as_json=as_json, watchdog=watchdog)
