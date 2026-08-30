from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DEFAULT_RETENTION_DAYS, STATE_DB_FILE


@dataclass
class RuntimeState:
    timer_id: str
    is_running: bool
    queued_once: bool
    queued_scheduled_at: Optional[str]
    running_run_id: Optional[str]
    last_processed_scheduled_at: Optional[str]
    running_count: int = 0


@dataclass
class DaemonState:
    timer_id: str
    status: str  # "stopped", "running", "restarting"
    current_run_id: Optional[str] = None
    restart_count: int = 0
    last_started_at: Optional[str] = None
    last_exited_at: Optional[str] = None
    last_exit_code: Optional[int] = None
    current_backoff_seconds: int = 0


class StateStore:
    def __init__(self, db_path: Path = STATE_DB_FILE) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Return the persistent connection, creating it if needed."""
        if self._conn is not None:
            return self._conn
        conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._conn = conn
        return conn

    def _reset_connection(self) -> None:
        """Close and discard the cached connection (e.g. after an error)."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _init_db(self) -> None:
        conn = self._connect()
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notification_mute_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    changed_at TEXT NOT NULL,
                    muted INTEGER NOT NULL,
                    source TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_notification_mute_audit_changed
                ON notification_mute_audit(changed_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS timer_runtime (
                    timer_id TEXT PRIMARY KEY,
                    is_running INTEGER NOT NULL DEFAULT 0,
                    queued_once INTEGER NOT NULL DEFAULT 0,
                    queued_scheduled_at TEXT,
                    running_run_id TEXT,
                    last_processed_scheduled_at TEXT
                );

                CREATE TABLE IF NOT EXISTS occurrences (
                    timer_id TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_run_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    is_catchup INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (timer_id, scheduled_at)
                );

                CREATE TABLE IF NOT EXISTS run_history (
                    run_id TEXT PRIMARY KEY,
                    timer_id TEXT NOT NULL,
                    timer_name TEXT,
                    scheduled_at TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    occurrence_key TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    is_catchup INTEGER NOT NULL DEFAULT 0,
                    queued_reason TEXT,
                    message TEXT,
                    stdout_path TEXT,
                    stderr_path TEXT,
                    retry_of_run_id TEXT,
                    idempotency_key TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_run_history_timer_created
                ON run_history(timer_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_run_history_status
                ON run_history(status);

                CREATE TABLE IF NOT EXISTS idempotency (
                    scope TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (scope, idem_key)
                );

                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    type TEXT NOT NULL,
                    timer_id TEXT,
                    message TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    acked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_incidents_acknowledged
                ON incidents(acknowledged);

                CREATE INDEX IF NOT EXISTS idx_incidents_ack_created
                ON incidents(acknowledged, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_incidents_timer
                ON incidents(timer_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_incidents_type
                ON incidents(type, created_at DESC);

                -- Ignore rules. A matching rule makes new incidents arrive
                -- already-acknowledged, so they stay in the report as evidence
                -- but never inflate the unacknowledged counter. '*' means "any";
                -- a sentinel rather than NULL because SQLite treats NULLs as
                -- distinct and would allow duplicate rules.
                CREATE TABLE IF NOT EXISTS incident_mutes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    timer_id TEXT NOT NULL DEFAULT '*',
                    type TEXT NOT NULL DEFAULT '*',
                    reason TEXT,
                    UNIQUE(timer_id, type)
                );

                CREATE TABLE IF NOT EXISTS active_runs (
                    run_id TEXT PRIMARY KEY,
                    timer_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    pid INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_active_runs_timer
                ON active_runs(timer_id);

                CREATE TABLE IF NOT EXISTS daemon_state (
                    timer_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'stopped',
                    current_run_id TEXT,
                    restart_count INTEGER NOT NULL DEFAULT 0,
                    last_started_at TEXT,
                    last_exited_at TEXT,
                    last_exit_code INTEGER,
                    current_backoff_seconds INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            # Migrations for existing DBs
            cols = [row["name"] for row in conn.execute("PRAGMA table_info(run_history)").fetchall()]
            if "timer_name" not in cols:
                conn.execute("ALTER TABLE run_history ADD COLUMN timer_name TEXT")
            if "timer_snapshot" not in cols:
                conn.execute("ALTER TABLE run_history ADD COLUMN timer_snapshot TEXT")
            rt_cols = [row["name"] for row in conn.execute("PRAGMA table_info(timer_runtime)").fetchall()]
            if "running_count" not in rt_cols:
                conn.execute("ALTER TABLE timer_runtime ADD COLUMN running_count INTEGER NOT NULL DEFAULT 0")
            inc_cols = [row["name"] for row in conn.execute("PRAGMA table_info(incidents)").fetchall()]
            if "ack_source" not in inc_cols:
                conn.execute("ALTER TABLE incidents ADD COLUMN ack_source TEXT")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def set_notification_mute(
        self, muted: bool, source: str, *, changed: bool
    ) -> Optional[Dict[str, Any]]:
        changed_at = self._now()
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('notifications.muted', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("true" if muted else "false",),
                )
                if not changed:
                    conn.commit()
                    return None
                cursor = conn.execute(
                    "INSERT INTO notification_mute_audit(changed_at, muted, source) VALUES(?, ?, ?)",
                    (changed_at, int(muted), source),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return {
                "id": int(cursor.lastrowid),
                "changed_at": changed_at,
                "muted": muted,
                "source": source,
            }

    def list_notification_mute_changes(self, limit: int = 20) -> List[Dict[str, Any]]:
        limit = min(max(limit, 1), 200)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT id, changed_at, muted, source "
                "FROM notification_mute_audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "id": int(row["id"]),
                    "changed_at": str(row["changed_at"]),
                    "muted": bool(row["muted"]),
                    "source": str(row["source"]),
                }
                for row in rows
            ]

    def get_runtime(self, timer_id: str) -> RuntimeState:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM timer_runtime WHERE timer_id = ?", (timer_id,)
            ).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO timer_runtime(timer_id, is_running, queued_once, running_count) VALUES(?, 0, 0, 0)",
                    (timer_id,),
                )
                return RuntimeState(timer_id, False, False, None, None, None, 0)
            return RuntimeState(
                timer_id=row["timer_id"],
                is_running=bool(row["is_running"]),
                queued_once=bool(row["queued_once"]),
                queued_scheduled_at=row["queued_scheduled_at"],
                running_run_id=row["running_run_id"],
                last_processed_scheduled_at=row["last_processed_scheduled_at"],
                running_count=int(row["running_count"]) if row["running_count"] else 0,
            )

    def set_runtime_running(self, timer_id: str, run_id: str, scheduled_at: str, pid: Optional[int] = None) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO timer_runtime(timer_id, is_running, running_run_id, running_count, last_processed_scheduled_at)
                VALUES(?, 1, ?, 1, ?)
                ON CONFLICT(timer_id) DO UPDATE SET
                    is_running = 1,
                    running_run_id = excluded.running_run_id,
                    running_count = running_count + 1,
                    last_processed_scheduled_at = excluded.last_processed_scheduled_at
                """,
                (timer_id, run_id, scheduled_at),
            )
            conn.execute(
                "INSERT OR REPLACE INTO active_runs(run_id, timer_id, started_at, pid) VALUES(?, ?, ?, ?)",
                (run_id, timer_id, self._now(), pid),
            )

    def set_runtime_idle(self, timer_id: str, run_id: Optional[str] = None) -> RuntimeState:
        with self._lock:
            conn = self._connect()
            if run_id:
                conn.execute("DELETE FROM active_runs WHERE run_id = ?", (run_id,))
                remaining = conn.execute(
                    "SELECT COUNT(*) as cnt FROM active_runs WHERE timer_id = ?", (timer_id,)
                ).fetchone()["cnt"]
                if remaining == 0:
                    conn.execute(
                        "UPDATE timer_runtime SET is_running = 0, running_run_id = NULL, running_count = 0 WHERE timer_id = ?",
                        (timer_id,),
                    )
                else:
                    conn.execute(
                        "UPDATE timer_runtime SET running_count = ? WHERE timer_id = ?",
                        (remaining, timer_id),
                    )
            else:
                conn.execute("DELETE FROM active_runs WHERE timer_id = ?", (timer_id,))
                conn.execute(
                    "UPDATE timer_runtime SET is_running = 0, running_run_id = NULL, running_count = 0 WHERE timer_id = ?",
                    (timer_id,),
                )
            row = conn.execute("SELECT * FROM timer_runtime WHERE timer_id = ?", (timer_id,)).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO timer_runtime(timer_id, is_running, queued_once, running_count) VALUES(?, 0, 0, 0)",
                    (timer_id,),
                )
                return RuntimeState(timer_id=timer_id, is_running=False, queued_once=False, queued_scheduled_at=None, running_run_id=None, last_processed_scheduled_at=None, running_count=0)
            return RuntimeState(
                timer_id=row["timer_id"],
                is_running=bool(row["is_running"]),
                queued_once=bool(row["queued_once"]),
                queued_scheduled_at=row["queued_scheduled_at"],
                running_run_id=row["running_run_id"],
                last_processed_scheduled_at=row["last_processed_scheduled_at"],
                running_count=int(row["running_count"]) if row["running_count"] else 0,
            )

    def update_active_run_pid(self, run_id: str, pid: int) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute("UPDATE active_runs SET pid = ? WHERE run_id = ?", (pid, run_id))

    def get_active_run_pid(self, run_id: str) -> Optional[int]:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT pid FROM active_runs WHERE run_id = ?", (run_id,)).fetchone()
            return int(row["pid"]) if row and row["pid"] is not None else None

    def get_active_runs_for_timer(self, timer_id: str) -> list:
        with self._lock:
            conn = self._connect()
            rows = conn.execute("SELECT * FROM active_runs WHERE timer_id = ?", (timer_id,)).fetchall()
            return [dict(r) for r in rows]

    def count_active_runs(self, timer_id: Optional[str] = None) -> int:
        with self._lock:
            conn = self._connect()
            if timer_id:
                row = conn.execute("SELECT COUNT(*) as cnt FROM active_runs WHERE timer_id = ?", (timer_id,)).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) as cnt FROM active_runs").fetchone()
            return int(row["cnt"])

    def get_daemon_state(self, timer_id: str) -> DaemonState:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT * FROM daemon_state WHERE timer_id = ?", (timer_id,)).fetchone()
            if not row:
                return DaemonState(timer_id=timer_id, status="stopped")
            return DaemonState(
                timer_id=row["timer_id"],
                status=row["status"],
                current_run_id=row["current_run_id"],
                restart_count=int(row["restart_count"]),
                last_started_at=row["last_started_at"],
                last_exited_at=row["last_exited_at"],
                last_exit_code=int(row["last_exit_code"]) if row["last_exit_code"] is not None else None,
                current_backoff_seconds=int(row["current_backoff_seconds"]),
            )

    def set_daemon_state(self, timer_id: str, **kwargs: Any) -> None:
        with self._lock:
            conn = self._connect()
            existing = conn.execute("SELECT 1 FROM daemon_state WHERE timer_id = ?", (timer_id,)).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO daemon_state(timer_id, status) VALUES(?, 'stopped')",
                    (timer_id,),
                )
            if kwargs:
                sets = ", ".join(f"{k} = ?" for k in kwargs)
                vals = list(kwargs.values()) + [timer_id]
                conn.execute(f"UPDATE daemon_state SET {sets} WHERE timer_id = ?", vals)

    def list_runtime(self) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute("SELECT * FROM timer_runtime").fetchall()
            return [dict(r) for r in rows]

    def set_queue_once(self, timer_id: str, scheduled_at: str) -> bool:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT queued_once, queued_scheduled_at FROM timer_runtime WHERE timer_id = ?",
                (timer_id,),
            ).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO timer_runtime(timer_id, queued_once, queued_scheduled_at) VALUES(?, 1, ?)",
                    (timer_id, scheduled_at),
                )
                return True

            if row["queued_once"]:
                existing = row["queued_scheduled_at"]
                if existing and existing >= scheduled_at:
                    return False
            conn.execute(
                "UPDATE timer_runtime SET queued_once = 1, queued_scheduled_at = ? WHERE timer_id = ?",
                (scheduled_at, timer_id),
            )
            return True

    def pop_queue_once(self, timer_id: str) -> Optional[str]:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT queued_once, queued_scheduled_at FROM timer_runtime WHERE timer_id = ?",
                (timer_id,),
            ).fetchone()
            if not row or not row["queued_once"]:
                return None
            scheduled = row["queued_scheduled_at"]
            conn.execute(
                "UPDATE timer_runtime SET queued_once = 0, queued_scheduled_at = NULL WHERE timer_id = ?",
                (timer_id,),
            )
            return scheduled

    def reserve_occurrence(self, timer_id: str, scheduled_at: str, is_catchup: bool) -> bool:
        now = self._now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO occurrences(timer_id, scheduled_at, status, is_catchup, created_at, updated_at)
                    VALUES(?, ?, 'pending', ?, ?, ?)
                    """,
                    (timer_id, scheduled_at, int(is_catchup), now, now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_occurrence_status(self, timer_id: str, scheduled_at: str, status: str, last_error: Optional[str] = None) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                UPDATE occurrences
                SET status = ?, last_error = ?, updated_at = ?
                WHERE timer_id = ? AND scheduled_at = ?
                """,
                (status, last_error, self._now(), timer_id, scheduled_at),
            )

    def create_run(
        self,
        timer_id: str,
        timer_name: str,
        scheduled_at: str,
        is_catchup: bool,
        queued_reason: Optional[str],
        retry_of_run_id: Optional[str] = None,
        timer_snapshot: Optional[str] = None,
    ) -> Dict[str, Any]:
        run_id = str(uuid.uuid4())
        now = self._now()
        occurrence_key = f"{timer_id}:{scheduled_at}"

        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT attempts FROM occurrences WHERE timer_id = ? AND scheduled_at = ?",
                (timer_id, scheduled_at),
            ).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 1
            conn.execute(
                """
                UPDATE occurrences
                SET status = 'running', attempts = ?, updated_at = ?, last_run_id = ?
                WHERE timer_id = ? AND scheduled_at = ?
                """,
                (attempts, now, run_id, timer_id, scheduled_at),
            )
            conn.execute(
                """
                INSERT INTO run_history(
                    run_id, timer_id, timer_name, scheduled_at, attempt, occurrence_key, started_at,
                    status, is_catchup, queued_reason, retry_of_run_id, created_at, timer_snapshot
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'started', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    timer_id,
                    timer_name,
                    scheduled_at,
                    attempts,
                    occurrence_key,
                    now,
                    int(is_catchup),
                    queued_reason,
                    retry_of_run_id,
                    now,
                    timer_snapshot,
                ),
            )
        return {
            "run_id": run_id,
            "timer_id": timer_id,
            "timer_name": timer_name,
            "scheduled_at": scheduled_at,
            "attempt": attempts,
        }

    def finish_run(
        self,
        run_id: str,
        timer_id: str,
        scheduled_at: str,
        status: str,
        exit_code: Optional[int],
        message: str,
        stdout_path: Optional[str],
        stderr_path: Optional[str],
    ) -> None:
        now = self._now()
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                UPDATE run_history
                SET finished_at = ?, status = ?, exit_code = ?, message = ?, stdout_path = ?, stderr_path = ?
                WHERE run_id = ?
                """,
                (now, status, exit_code, message, stdout_path, stderr_path, run_id),
            )
            if status == "success":
                occurrence_status = "success"
            elif status == "waiting":
                occurrence_status = "pending"
            else:
                occurrence_status = "failed"
            conn.execute(
                """
                UPDATE occurrences
                SET status = ?, updated_at = ?, last_error = ?
                WHERE timer_id = ? AND scheduled_at = ?
                """,
                (occurrence_status, now, None if occurrence_status == "success" else message, timer_id, scheduled_at),
            )

    def count_completed_runs(self, timer_id: str) -> int:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM run_history WHERE timer_id = ? AND status IN ('success', 'failed')",
                (timer_id,),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def recover_uncertain_runs(self) -> List[Dict[str, Any]]:
        now = self._now()
        recovered: List[Dict[str, Any]] = []
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT run_id, timer_id, scheduled_at
                FROM run_history
                WHERE status = 'started'
                """
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE run_history
                    SET status = 'uncertain_crash', finished_at = ?, message = 'Recovered after crash; retry queued'
                    WHERE run_id = ?
                    """,
                    (now, row["run_id"]),
                )
                conn.execute(
                    """
                    UPDATE occurrences
                    SET status = 'pending', updated_at = ?, last_error = 'Recovered after crash; retry pending'
                    WHERE timer_id = ? AND scheduled_at = ?
                    """,
                    (now, row["timer_id"], row["scheduled_at"]),
                )
                recovered.append(dict(row))
        return recovered

    def list_runs(self, limit: int = 100, timer_id: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        query = "SELECT * FROM run_history"
        params: List[Any] = []
        if timer_id:
            query += " WHERE timer_id = ?"
            params.append(timer_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            conn = self._connect()
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT * FROM run_history WHERE run_id = ?", (run_id,)).fetchone()
            return dict(row) if row else None

    def store_idempotent(self, scope: str, idem_key: str, fingerprint: str, response: Dict[str, Any]) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO idempotency(scope, idem_key, fingerprint, response_json, created_at) VALUES(?, ?, ?, ?, ?)",
                (scope, idem_key, fingerprint, json.dumps(response, sort_keys=True), self._now()),
            )

    def get_idempotent(self, scope: str, idem_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT fingerprint, response_json FROM idempotency WHERE scope = ? AND idem_key = ?",
                (scope, idem_key),
            ).fetchone()
            if not row:
                return None
            payload = json.loads(row["response_json"])
            return {
                "fingerprint": row["fingerprint"],
                "response": payload,
            }

    # ----- incidents -------------------------------------------------

    MUTE_ANY = "*"

    @staticmethod
    def _incident_where(
        include_acked: bool = True,
        incident_type: Optional[str] = None,
        severity: Optional[str] = None,
        timer_id: Optional[str] = None,
        since: Optional[str] = None,
        max_id: Optional[int] = None,
    ) -> tuple:
        """Build the shared WHERE clause for incident list/count/ack.

        One builder keeps the list the operator sees and the rows a bulk ack
        touches provably identical, so "resolve all" can never act on a wider
        set than what was displayed.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if not include_acked:
            clauses.append("acknowledged = 0")
        if incident_type:
            clauses.append("type = ?")
            params.append(incident_type)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if timer_id:
            clauses.append("timer_id = ?")
            params.append(timer_id)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if max_id is not None:
            clauses.append("id <= ?")
            params.append(int(max_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def matching_mute(self, incident_type: str, timer_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Return the ignore rule covering this incident, if any."""
        any_ = self.MUTE_ANY
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                """
                SELECT * FROM incident_mutes
                WHERE (timer_id = ? OR timer_id = ?)
                  AND (type = ? OR type = ?)
                ORDER BY (timer_id = ?) DESC, (type = ?) DESC
                LIMIT 1
                """,
                (timer_id or "", any_, incident_type, any_, any_, any_),
            ).fetchone()
            return dict(row) if row else None

    def add_incident(self, severity: str, incident_type: str, message: str, timer_id: Optional[str] = None) -> int:
        muted = self.matching_mute(incident_type, timer_id)
        with self._lock:
            conn = self._connect()
            now = self._now()
            if muted:
                # Still recorded, so the report keeps full history; pre-acked so
                # it never re-inflates the unacknowledged counter.
                cursor = conn.execute(
                    "INSERT INTO incidents(created_at, severity, type, timer_id, message,"
                    " acknowledged, acked_at, ack_source) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
                    (now, severity, incident_type, timer_id, message, now, f"mute:{muted['id']}"),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO incidents(created_at, severity, type, timer_id, message) VALUES(?, ?, ?, ?, ?)",
                    (now, severity, incident_type, timer_id, message),
                )
            return int(cursor.lastrowid)

    def list_incidents(
        self,
        limit: int = 200,
        include_acked: bool = True,
        incident_type: Optional[str] = None,
        severity: Optional[str] = None,
        timer_id: Optional[str] = None,
        since: Optional[str] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        limit = min(max(limit, 1), 1000)
        offset = max(int(offset), 0)
        where, params = self._incident_where(
            include_acked=include_acked,
            incident_type=incident_type,
            severity=severity,
            timer_id=timer_id,
            since=since,
        )
        query = "SELECT * FROM incidents" + where + " ORDER BY id DESC LIMIT ? OFFSET ?"

        with self._lock:
            conn = self._connect()
            rows = conn.execute(query, (*params, limit, offset)).fetchall()
            return [dict(r) for r in rows]

    def count_incidents(
        self,
        include_acked: bool = True,
        incident_type: Optional[str] = None,
        severity: Optional[str] = None,
        timer_id: Optional[str] = None,
        since: Optional[str] = None,
    ) -> int:
        where, params = self._incident_where(
            include_acked=include_acked,
            incident_type=incident_type,
            severity=severity,
            timer_id=timer_id,
            since=since,
        )
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM incidents" + where, tuple(params)
            ).fetchone()
            return int(row["n"])

    def count_unacked_incidents(self) -> int:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT COUNT(*) AS incident_count FROM incidents WHERE acknowledged = 0"
            ).fetchone()
            return int(row["incident_count"])

    def ack_incident(self, incident_id: int, source: Optional[str] = None) -> bool:
        with self._lock:
            conn = self._connect()
            cursor = conn.execute(
                "UPDATE incidents SET acknowledged = 1, acked_at = ?, ack_source = ?"
                " WHERE id = ? AND acknowledged = 0",
                (self._now(), source or "api", incident_id),
            )
            return cursor.rowcount > 0

    def ack_incidents(
        self,
        incident_type: Optional[str] = None,
        severity: Optional[str] = None,
        timer_id: Optional[str] = None,
        since: Optional[str] = None,
        max_id: Optional[int] = None,
        source: Optional[str] = None,
    ) -> int:
        """Acknowledge every unacknowledged incident matching the filter.

        `max_id` pins the operation to incidents that already existed when the
        operator looked, so a run failing mid-request is not silently resolved.
        """
        where, params = self._incident_where(
            include_acked=False,
            incident_type=incident_type,
            severity=severity,
            timer_id=timer_id,
            since=since,
            max_id=max_id,
        )
        with self._lock:
            conn = self._connect()
            cursor = conn.execute(
                "UPDATE incidents SET acknowledged = 1, acked_at = ?, ack_source = ?" + where,
                (self._now(), source or "bulk", *params),
            )
            return int(cursor.rowcount)

    def unack_incident(self, incident_id: int) -> bool:
        """Reopen a resolved incident (undo for a mis-click)."""
        with self._lock:
            conn = self._connect()
            cursor = conn.execute(
                "UPDATE incidents SET acknowledged = 0, acked_at = NULL, ack_source = NULL"
                " WHERE id = ? AND acknowledged = 1",
                (incident_id,),
            )
            return cursor.rowcount > 0

    def incident_summary(self, days: int = 30) -> Dict[str, Any]:
        """Aggregate report: totals, breakdowns, and a daily trend."""
        days = min(max(int(days), 1), 365)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._lock:
            conn = self._connect()

            totals = dict(
                conn.execute(
                    "SELECT COUNT(*) AS total,"
                    " COALESCE(SUM(acknowledged = 0), 0) AS unacked,"
                    " MIN(created_at) AS oldest, MAX(created_at) AS newest"
                    " FROM incidents"
                ).fetchone()
            )

            def group(column: str) -> List[Dict[str, Any]]:
                rows = conn.execute(
                    f"SELECT {column} AS key, COUNT(*) AS total,"
                    " COALESCE(SUM(acknowledged = 0), 0) AS unacked,"
                    " MAX(created_at) AS last_seen, MIN(created_at) AS first_seen"
                    f" FROM incidents GROUP BY {column} ORDER BY unacked DESC, total DESC LIMIT 50"
                ).fetchall()
                return [dict(r) for r in rows]

            trend = [
                dict(r)
                for r in conn.execute(
                    "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS total,"
                    " COALESCE(SUM(acknowledged = 0), 0) AS unacked"
                    " FROM incidents WHERE created_at >= ?"
                    " GROUP BY day ORDER BY day",
                    (cutoff,),
                ).fetchall()
            ]

            recent = dict(
                conn.execute(
                    "SELECT COUNT(*) AS total, COALESCE(SUM(acknowledged = 0), 0) AS unacked"
                    " FROM incidents WHERE created_at >= ?",
                    (cutoff,),
                ).fetchone()
            )

            return {
                "window_days": days,
                "totals": totals,
                "recent": recent,
                "by_type": group("type"),
                "by_severity": group("severity"),
                "by_timer": group("timer_id"),
                "trend": trend,
            }

    # ----- ignore rules ----------------------------------------------

    def add_incident_mute(
        self,
        timer_id: Optional[str] = None,
        incident_type: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        any_ = self.MUTE_ANY
        tid = timer_id or any_
        typ = incident_type or any_
        if tid == any_ and typ == any_:
            raise ValueError("an ignore rule must pin at least a timer or a type")
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR IGNORE INTO incident_mutes(created_at, timer_id, type, reason)"
                " VALUES(?, ?, ?, ?)",
                (self._now(), tid, typ, reason),
            )
            row = conn.execute(
                "SELECT * FROM incident_mutes WHERE timer_id = ? AND type = ?", (tid, typ)
            ).fetchone()
            return dict(row)

    def list_incident_mutes(self) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM incident_mutes ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_incident_mute(self, mute_id: int) -> bool:
        with self._lock:
            conn = self._connect()
            cursor = conn.execute("DELETE FROM incident_mutes WHERE id = ?", (int(mute_id),))
            return cursor.rowcount > 0

    def vacuum(self) -> None:
        """Reclaim file space after a large delete (auto_vacuum is off)."""
        with self._lock:
            conn = self._connect()
            conn.execute("VACUUM")

    def prune_old_data(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> Dict[str, int]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        cutoff_iso = cutoff.isoformat()
        with self._lock:
            conn = self._connect()
            run_deleted = conn.execute(
                "DELETE FROM run_history WHERE created_at < ?",
                (cutoff_iso,),
            ).rowcount
            occ_deleted = conn.execute(
                "DELETE FROM occurrences WHERE updated_at < ? AND status IN ('success', 'failed')",
                (cutoff_iso,),
            ).rowcount
            idem_deleted = conn.execute(
                "DELETE FROM idempotency WHERE created_at < ?",
                (cutoff_iso,),
            ).rowcount
            inc_deleted = conn.execute(
                "DELETE FROM incidents WHERE created_at < ? AND acknowledged = 1",
                (cutoff_iso,),
            ).rowcount
        return {
            "runs": run_deleted,
            "occurrences": occ_deleted,
            "idempotency": idem_deleted,
            "incidents": inc_deleted,
        }
