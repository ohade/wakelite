from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Dict

from .config import OWNER

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "launchd"
USER_TEMPLATE = TEMPLATE_DIR / f"{OWNER}.runner.plist"
SYSTEM_TEMPLATE = TEMPLATE_DIR / f"{OWNER}.wakereconciler.plist"

USER_TARGET = Path.home() / "Library" / "LaunchAgents" / f"{OWNER}.runner.plist"
SYSTEM_TARGET = Path(f"/Library/LaunchDaemons/{OWNER}.wakereconciler.plist")


def _run(cmd: list[str]) -> Dict:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def install_user(load: bool = False) -> Dict:
    USER_TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(USER_TEMPLATE, USER_TARGET)
    result = {"installed": str(USER_TARGET)}
    if load:
        result["bootout"] = _run(["launchctl", "bootout", f"gui/{_uid()}", str(USER_TARGET)])
        result["bootstrap"] = _run(["launchctl", "bootstrap", f"gui/{_uid()}", str(USER_TARGET)])
    return result


def uninstall_user(unload: bool = False) -> Dict:
    result = {"removed": False, "path": str(USER_TARGET)}
    if unload:
        result["bootout"] = _run(["launchctl", "bootout", f"gui/{_uid()}", str(USER_TARGET)])
    if USER_TARGET.exists():
        USER_TARGET.unlink()
        result["removed"] = True
    return result


def install_system(load: bool = False) -> Dict:
    shutil.copy2(SYSTEM_TEMPLATE, SYSTEM_TARGET)
    result = {"installed": str(SYSTEM_TARGET)}
    if load:
        result["bootout"] = _run(["launchctl", "bootout", "system", str(SYSTEM_TARGET)])
        result["bootstrap"] = _run(["launchctl", "bootstrap", "system", str(SYSTEM_TARGET)])
    return result


def uninstall_system(unload: bool = False) -> Dict:
    result = {"removed": False, "path": str(SYSTEM_TARGET)}
    if unload:
        result["bootout"] = _run(["launchctl", "bootout", "system", str(SYSTEM_TARGET)])
    if SYSTEM_TARGET.exists():
        SYSTEM_TARGET.unlink()
        result["removed"] = True
    return result


def status() -> Dict:
    return {
        "user_plist_exists": USER_TARGET.exists(),
        "system_plist_exists": SYSTEM_TARGET.exists(),
        "user_list": _run(["launchctl", "list", f"{OWNER}.runner"]),
        "system_list": _run(["launchctl", "list", f"{OWNER}.wakereconciler"]),
    }


def _uid() -> str:
    return _run(["id", "-u"])["stdout"]
