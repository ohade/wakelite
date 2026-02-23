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
