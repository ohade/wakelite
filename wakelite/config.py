from __future__ import annotations

import logging
import os
from pathlib import Path

_LOG = logging.getLogger(__name__)

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

AMQ_BINARY_PATH = "/opt/homebrew/bin/amq"
AMQ_CALLBACK_SEND_TIMEOUT_SECONDS = 5
AMQ_KEEPALIVE_REGISTRY_FILE = Path.home() / ".amq-keepalive" / "registry.json"

CLAUDE_MCP_CONFIG = Path.home() / ".mcp.json"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"


def amq_callback_enabled() -> bool:
    """Return whether the opt-in AMQ callback route is enabled globally."""
    raw = os.environ.get("WAKELITE_AMQ_CALLBACK_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in ("false", "0", "no", "off", "disabled")


def ensure_dirs() -> None:
    for d in (HOME_DIR, RUN_DIR, LOG_DIR, MANIFEST_DIR):
        d.mkdir(parents=True, exist_ok=True)


def auto_capture_terminal(callback: dict) -> None:
    """Auto-detect terminal type and ID from environment, mutating callback in-place.

    Checks cmux first, then $GHOSTTY_TERMINAL_ID, then $WEZTERM_PANE unless
    callback.type is already set. Sets the appropriate ID field if not already set.
    """
    if callback is None or not isinstance(callback, dict):
        return

    cmux_workspace = os.environ.get("CMUX_WORKSPACE_ID")
    cmux_surface = os.environ.get("CMUX_SURFACE_ID") or os.environ.get("CMUX_PANEL_ID")
    ghostty_id = os.environ.get("GHOSTTY_TERMINAL_ID")
    wezterm_pane = os.environ.get("WEZTERM_PANE")

    # CC-95 HIGH#5: require BOTH CMUX_WORKSPACE_ID and CMUX_SURFACE_ID/CMUX_PANEL_ID
    # before auto-detecting cmux. Partial env (only one of the two) falls through to
    # ghostty/wezterm detection rather than producing a half-populated cmux callback
    # that fails timer_store validation at create time. Stale env carryover from a
    # parent process (e.g. CMUX_SURFACE_ID leaked into a non-cmux subshell) must not
    # misclassify the terminal.
    #
    # CC-95 MEDIUM (claude-nyx, claude-artemis): when partial cmux env is rejected,
    # emit a debug log so an operator debugging "why isn't my cmux callback being
    # captured?" has a signal that the partial-env was the cause. Without this,
    # the path looks identical to "no cmux env at all" — see audit finding F-CC95-MED-1.
    if (cmux_workspace or cmux_surface) and not (cmux_workspace and cmux_surface):
        _LOG.debug(
            "auto_capture: partial cmux env rejected (workspace=%r surface=%r); "
            "falling through to ghostty/wezterm detection",
            cmux_workspace,
            cmux_surface,
        )

    def capture_cmux() -> None:
        if not (cmux_workspace and cmux_surface):
            return
        if callback.get("workspace_id") is None:
            callback["workspace_id"] = cmux_workspace
        if callback.get("surface_id") is None:
            callback["surface_id"] = cmux_surface
        if callback.get("socket_path") is None and os.environ.get("CMUX_SOCKET_PATH"):
            callback["socket_path"] = os.environ["CMUX_SOCKET_PATH"]
        if callback.get("cli_path") is None:
            for candidate in (
                os.environ.get("CMUX_BUNDLED_CLI_PATH"),
                "/opt/homebrew/bin/cmux",
                "/usr/local/bin/cmux",
                "/Applications/cmux.app/Contents/Resources/bin/cmux",
            ):
                if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    callback["cli_path"] = candidate
                    break

    def capture_ghostty() -> None:
        if ghostty_id and callback.get("terminal_id") is None:
            callback["terminal_id"] = ghostty_id

    def capture_wezterm() -> None:
        if wezterm_pane and callback.get("pane_id") is None:
            try:
                callback["pane_id"] = int(wezterm_pane)
            except (ValueError, TypeError):
                pass

    requested_type = callback.get("type")
    if requested_type == "cmux":
        capture_cmux()
    elif requested_type == "ghostty":
        capture_ghostty()
    elif requested_type == "wezterm":
        capture_wezterm()
    elif cmux_workspace and cmux_surface:
        callback["type"] = "cmux"
        capture_cmux()
    elif ghostty_id:
        callback["type"] = "ghostty"
        capture_ghostty()
    elif wezterm_pane:
        callback["type"] = "wezterm"
        capture_wezterm()
