from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "wakelite"
OWNER = "com.wakelite"
API_PORT = 17341
MCP_HTTP_PORT = 17342
_DEFAULT_HOME = Path.home() / ".wakelite"
_HOME_ENV_VAR = "WAKELITE_HOME"

API_HOST = "127.0.0.1"
MCP_HTTP_HOST = "127.0.0.1"

HOME_DIR = Path(os.environ.get(_HOME_ENV_VAR, _DEFAULT_HOME)).expanduser()
RUN_DIR = HOME_DIR / "run"
LOG_DIR = HOME_DIR / "logs"
MANIFEST_DIR = HOME_DIR / "manifest"
TIMER_FILE = HOME_DIR / "timers.json"
WAKE_INTENTS_FILE = HOME_DIR / "wake-intents.json"
STATE_DB_FILE = HOME_DIR / "state.db"
RECONCILER_LOG = LOG_DIR / "reconciler.log"
RUNNER_LOG = LOG_DIR / "runner.log"
SOCKET_PATH = RUN_DIR / "api.sock"

MCP_MANIFEST_FILE = MANIFEST_DIR / "mcp.server.json"

DEFAULT_RETENTION_DAYS = 30
RUN_LOG_RETENTION_DAYS = 7
DEFAULT_HORIZON_DAYS = 21

MAX_WORKERS = 16

CLAUDE_MCP_CONFIG = Path.home() / ".mcp.json"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"


def ensure_dirs() -> None:
    for d in (HOME_DIR, RUN_DIR, LOG_DIR, MANIFEST_DIR):
        d.mkdir(parents=True, exist_ok=True)


def auto_capture_terminal(callback: dict) -> None:
    """Auto-detect terminal type and ID from environment, mutating callback in-place.

    Checks $GHOSTTY_TERMINAL_ID first (preferred), then $WEZTERM_PANE.
    Sets callback.type and the appropriate ID field if not already set.
    """
    if callback is None or not isinstance(callback, dict):
        return

    ghostty_id = os.environ.get("GHOSTTY_TERMINAL_ID")
    wezterm_pane = os.environ.get("WEZTERM_PANE")

    if ghostty_id:
        callback.setdefault("type", "ghostty")
        if callback.get("type") == "ghostty" and callback.get("terminal_id") is None:
            callback["terminal_id"] = ghostty_id
    elif wezterm_pane:
        callback.setdefault("type", "wezterm")
        if callback.get("type") == "wezterm" and callback.get("pane_id") is None:
            try:
                callback["pane_id"] = int(wezterm_pane)
            except (ValueError, TypeError):
                pass
