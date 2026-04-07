from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import HOME_DIR


BUNDLED_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "reminder": {
        "name": "reminder",
        "description": "One-time reminder with wake + notification. Auto-deletes after firing.",
        "defaults": {
            "recurrence": {"frequency": "once"},
            "wake": {"enabled": True, "action": "wake", "leadMinutes": 2},
            "notifications": {"onSuccess": True, "onFailure": True},
            "until": {"on_success": "delete", "on_failure": "continue"},
            "command": {"mode": "shell"},
        },
        "required_overrides": ["name", "recurrence.date", "recurrence.time", "command.shell"],
    },
    "callback": {
        "name": "callback",
        "description": "One-time timer that sends results back to the terminal. Auto-deletes on success.",
        "defaults": {
            "recurrence": {"frequency": "once"},
            "callback": {},
            "until": {"on_success": "delete", "on_failure": "continue"},
            "notifications": {"onSuccess": False, "onFailure": True},
            "command": {"mode": "shell"},
        },
        "required_overrides": ["name", "recurrence.date", "recurrence.time", "command.shell"],
    },
    "health-check": {
        "name": "health-check",
        "description": "Interval health check with active hours window. Notifies on failure only.",
        "defaults": {
            "recurrence": {
                "frequency": "interval",
                "every": "5m",
                "active_hours": {"start": "07:00", "end": "22:00"},
            },
            "execution": {"overlap": "skip"},
            "notifications": {"onSuccess": False, "onFailure": True},
            "command": {"mode": "shell"},
            "wake": {"enabled": False, "action": "wake", "leadMinutes": 0},
        },
        "required_overrides": ["name", "command.shell"],
    },
    "build-monitor": {
        "name": "build-monitor",
        "description": "Poll a build/deploy until success (exit 0) or give up after max_runs. Exit 75 = still waiting.",
        "defaults": {
            "recurrence": {"frequency": "interval", "every": "5m"},
            "max_runs": 20,
            "execution": {"overlap": "skip"},
            "until": {"on_success": "delete", "on_failure": "continue"},
            "notifications": {"onSuccess": True, "onFailure": True},
            "command": {"mode": "shell"},
        },
        "required_overrides": ["name", "command.shell"],
    },
    "daemon": {
        "name": "daemon",
        "description": "Long-lived process kept alive with restart and exponential backoff.",
        "defaults": {
            "timer_type": "daemon",
            "recurrence": {"frequency": "interval", "every": "0s"},
            "execution": {
                "overlap": "skip",
                "restart_on_failure": True,
                "restart_delay_seconds": 5,
                "restart_max_backoff_seconds": 300,
            },
            "notifications": {"onSuccess": False, "onFailure": True},
            "command": {"mode": "shell"},
        },
        "required_overrides": ["name", "command.shell"],
    },
    "recurring": {
        "name": "recurring",
        "description": "Daily recurring task at a fixed time.",
        "defaults": {
            "recurrence": {"frequency": "daily", "interval": 1},
            "wake": {"enabled": False, "action": "wake", "leadMinutes": 0},
            "notifications": {"onSuccess": False, "onFailure": True},
            "command": {"mode": "shell"},
        },
        "required_overrides": ["name", "recurrence.time", "command.shell"],
    },
}


def _user_templates_dir() -> Path:
    return HOME_DIR / "templates"


def _load_user_templates() -> Dict[str, Dict[str, Any]]:
    tpl_dir = _user_templates_dir()
    if not tpl_dir.is_dir():
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for f in sorted(tpl_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            name = data.get("name", f.stem)
            result[name] = data
        except (json.JSONDecodeError, OSError):
            continue
    return result


def list_templates() -> List[Dict[str, Any]]:
    merged = dict(BUNDLED_TEMPLATES)
    merged.update(_load_user_templates())
    return [
        {"name": t["name"], "description": t.get("description", ""), "builtin": t["name"] in BUNDLED_TEMPLATES}
        for t in merged.values()
    ]


def get_template(name: str) -> Optional[Dict[str, Any]]:
    user = _load_user_templates()
    if name in user:
        return user[name]
    return BUNDLED_TEMPLATES.get(name)


def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def resolve_template(name: str, overrides: Dict[str, Any]) -> Dict[str, Any]:
    template = get_template(name)
    if template is None:
        raise ValueError(f"Unknown template: '{name}'. Available: {', '.join(t['name'] for t in list_templates())}")
    defaults = copy.deepcopy(template.get("defaults", {}))
    return _deep_merge(defaults, overrides)
