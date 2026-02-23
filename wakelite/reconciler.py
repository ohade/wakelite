from __future__ import annotations

import argparse
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .config import OWNER, RECONCILER_LOG, WAKE_INTENTS_FILE, ensure_dirs
from .utils import read_json


SCHED_RE = re.compile(r"\[\d+\]\s+(\w+) at (\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}) by '([^']+)'")


@dataclass(frozen=True)
class ScheduledEvent:
    action: str
    dt: datetime
    owner: str

    @property
    def key(self) -> str:
        return f"{self.action}|{self.dt.strftime('%Y-%m-%d %H:%M:%S')}|{self.owner}"


def _to_pmset_date(dt: datetime) -> str:
    return dt.strftime("%m/%d/%y %H:%M:%S")


def _parse_pmset_date(value: str) -> datetime:
    # pmset -g sched prints with 4-digit year.
    return datetime.strptime(value, "%m/%d/%Y %H:%M:%S")


def _run_pmset(args: List[str], dry_run: bool = False) -> subprocess.CompletedProcess:
    cmd = ["pmset"] + args
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def list_current_events(owner: str = OWNER) -> List[ScheduledEvent]:
    proc = subprocess.run(["pmset", "-g", "sched"], check=False, capture_output=True, text=True)
    events: List[ScheduledEvent] = []
    for line in proc.stdout.splitlines():
        m = SCHED_RE.search(line)
        if not m:
            continue
        action, date_str, ev_owner = m.group(1), m.group(2), m.group(3)
        if ev_owner != owner:
            continue
        dt = _parse_pmset_date(date_str)
        events.append(ScheduledEvent(action=action, dt=dt, owner=ev_owner))
    return events


def load_desired_intents(path: Path = WAKE_INTENTS_FILE) -> List[ScheduledEvent]:
    raw = read_json(path, {"events": []})
    events: List[ScheduledEvent] = []
    for e in raw.get("events", []):
        try:
            dt = datetime.fromisoformat(e["wake_time"])
            events.append(ScheduledEvent(action=e.get("action", "wake"), dt=dt, owner=e.get("owner", OWNER)))
        except Exception:
            continue
    return events


def reconcile_once(dry_run: bool = False) -> Dict[str, int]:
    desired = [e for e in load_desired_intents() if e.owner == OWNER]
    current = list_current_events(owner=OWNER)

    now = datetime.now()
    desired_map = {e.key: e for e in desired if e.dt >= now}
    current_map = {e.key: e for e in current if e.dt >= now}

    to_add = [desired_map[k] for k in sorted(desired_map.keys()) if k not in current_map]
    to_remove = [current_map[k] for k in sorted(current_map.keys()) if k not in desired_map]

    added = 0
    removed = 0
    failures = 0

    for ev in to_add:
        proc = _run_pmset(["schedule", ev.action, _to_pmset_date(ev.dt), OWNER], dry_run=dry_run)
        if proc.returncode == 0:
            added += 1
        else:
            failures += 1

    for ev in to_remove:
        proc = _run_pmset(
            ["schedule", "cancel", ev.action, _to_pmset_date(ev.dt), OWNER],
            dry_run=dry_run,
        )
        if proc.returncode == 0:
            removed += 1
        else:
            failures += 1

    return {
        "desired": len(desired_map),
        "current": len(current_map),
        "added": added,
        "removed": removed,
        "failures": failures,
    }


def run_daemon(interval_seconds: int = 600, dry_run: bool = False) -> None:
    ensure_dirs()
    logging.basicConfig(
        filename=str(RECONCILER_LOG),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    while True:
        summary = reconcile_once(dry_run=dry_run)
        logging.info("reconcile summary: %s", summary)
        time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="WakeLite wake reconciler")
    parser.add_argument("--once", action="store_true", help="run a single reconcile pass")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--interval", type=int, default=600)
    args = parser.parse_args()

    if args.once:
        summary = reconcile_once(dry_run=args.dry_run)
        print(summary)
        return

    run_daemon(interval_seconds=args.interval, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
