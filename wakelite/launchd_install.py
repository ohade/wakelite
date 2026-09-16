from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Dict

from .config import OWNER

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "launchd"
USER_TEMPLATE = TEMPLATE_DIR / f"{OWNER}.runner.plist"
SYSTEM_TEMPLATE = TEMPLATE_DIR / f"{OWNER}.wakereconciler.plist"

WATCHDOG_TEMPLATE = TEMPLATE_DIR / f"{OWNER}.watchdog.plist"

USER_TARGET = Path.home() / "Library" / "LaunchAgents" / f"{OWNER}.runner.plist"
WATCHDOG_TARGET = Path.home() / "Library" / "LaunchAgents" / f"{OWNER}.watchdog.plist"
SYSTEM_TARGET = Path(f"/Library/LaunchDaemons/{OWNER}.wakereconciler.plist")


def _run(cmd: list[str]) -> Dict:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


REPO_ROOT = Path(__file__).resolve().parents[1]


def _render(template: Path) -> str:
    """Substitute the template placeholders with this machine's real paths.

    The templates ship with `/path/to/...` so they are readable in the repo.
    install_user() used to copy them verbatim, which produced an installed plist
    launchd could not run — and silently, because launchd only reads the plist
    when it next starts the job, so the damage surfaced at the next restart
    rather than at install time.
    """
    return (
        template.read_text()
        .replace("/path/to/wakelite", str(REPO_ROOT))
        .replace("/path/to/home", str(Path.home()))
    )


def _write_agent(template: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render(template))


def _bootout(domain: str, target: Path) -> Dict:
    """Unload an agent, distinguishing "was not loaded" from a real failure.

    Install and uninstall both boot out before they bootstrap, so on a clean
    machine the agent is not loaded and launchctl fails. It reports that as
    EIO (5) with "Boot-out failed: 5: Input/output error" rather than as a
    distinct code, so matching on the code alone would also swallow genuine
    I/O errors -- hence the message check as well.

    Reported by a first-time user on 2026-09-16: the raw non-zero result read
    as a broken install even though the following bootstrap returned 0.

    Returns the usual `_run` dict plus a `status` of `unloaded`, `not_loaded`,
    or `failed`. A `not_loaded` result is normalised to returncode 0 because
    nothing went wrong; `failed` keeps its original code and stderr.
    """
    result = _run(["launchctl", "bootout", domain, str(target)])
    if result["returncode"] == 0:
        result["status"] = "unloaded"
        return result
    stderr = result.get("stderr", "")
    if result["returncode"] == 5 and "Input/output error" in stderr:
        result["status"] = "not_loaded"
        result["detail"] = "was not loaded; nothing to unload"
        result["launchctl_returncode"] = result["returncode"]
        result["returncode"] = 0
        return result
    result["status"] = "failed"
    return result


def install_user(load: bool = False) -> Dict:
    _write_agent(USER_TEMPLATE, USER_TARGET)
    _write_agent(WATCHDOG_TEMPLATE, WATCHDOG_TARGET)
    result = {"installed": str(USER_TARGET), "installed_watchdog": str(WATCHDOG_TARGET)}
    if load:
        result["bootout"] = _bootout(f"gui/{_uid()}", USER_TARGET)
        result["bootstrap"] = _run(["launchctl", "bootstrap", f"gui/{_uid()}", str(USER_TARGET)])
        result["watchdog_bootout"] = _bootout(f"gui/{_uid()}", WATCHDOG_TARGET)
        result["watchdog_bootstrap"] = _run(
            ["launchctl", "bootstrap", f"gui/{_uid()}", str(WATCHDOG_TARGET)]
        )
    return result


def uninstall_user(unload: bool = False) -> Dict:
    result = {"removed": False, "path": str(USER_TARGET)}
    if unload:
        result["bootout"] = _bootout(f"gui/{_uid()}", USER_TARGET)
        result["watchdog_bootout"] = _bootout(f"gui/{_uid()}", WATCHDOG_TARGET)
    if USER_TARGET.exists():
        USER_TARGET.unlink()
        result["removed"] = True
    if WATCHDOG_TARGET.exists():
        WATCHDOG_TARGET.unlink()
        result["removed_watchdog"] = True
    return result


def install_system(load: bool = False) -> Dict:
    shutil.copy2(SYSTEM_TEMPLATE, SYSTEM_TARGET)
    result = {"installed": str(SYSTEM_TARGET)}
    if load:
        result["bootout"] = _bootout("system", SYSTEM_TARGET)
        result["bootstrap"] = _run(["launchctl", "bootstrap", "system", str(SYSTEM_TARGET)])
    return result


def uninstall_system(unload: bool = False) -> Dict:
    result = {"removed": False, "path": str(SYSTEM_TARGET)}
    if unload:
        result["bootout"] = _bootout("system", SYSTEM_TARGET)
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
